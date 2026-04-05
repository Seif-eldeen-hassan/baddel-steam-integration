'use strict';

// ============================================================
// steamBridge.js — Baddel Launcher
// ============================================================
// Manages the baddel_bridge.py subprocess and exposes a clean
// async API to the rest of the Electron app.
//
// Usage:
//   const bridge = require('./steamBridge');
//   await bridge.start();
//   const result = await bridge.authenticate(storedCreds);
//   const { games } = await bridge.getOwnedGames();
// ============================================================

const path        = require('path');
const fs          = require('fs');
const { spawn }   = require('child_process');
const EventEmitter = require('events');

// ─── Paths ───────────────────────────────────────────────────
// app.getPath() only works inside Electron — use a lazy getter
function _getApp() { return require('electron').app; }
function _getCacheFile() {
    return path.join(_getApp().getPath('userData'), 'platform-sync', 'steam_bridge_cache.json');
}

const PYTHON_BIN    = _findPython();
const BRIDGE_SCRIPT = path.join(__dirname, 'baddel-steam-integration', 'src', 'baddel_bridge.py');

function _findPython() {
    const candidates = [
        // Bundled virtualenv (ship this with the app)
        path.join(__dirname, 'python_env', 'Scripts', 'python.exe'),   // Windows
        path.join(__dirname, 'python_env', 'bin', 'python3'),           // macOS/Linux
        // System fallbacks
        'C:\\Users\\seife\\AppData\\Local\\Programs\\Python\\Python311\\python.exe',
        'python',
    ];
    for (const p of candidates) {
        if (!p.includes(path.sep)) return p; // system PATH entry
        if (fs.existsSync(p)) return p;
    }
    return 'python3';
}

// ─── Bridge Class ─────────────────────────────────────────────
class SteamBridge extends EventEmitter {
    constructor() {
        super();
        this._proc       = null;
        this._ready      = false;
        this._pendingCalls = new Map();   // id → { resolve, reject }
        this._nextId     = 1;
        this._buffer     = '';
        this._shutdownRequested = false;
    }

    // ── Lifecycle ──────────────────────────────────────────────

    async start() {
        if (this._proc) return; // already running

        await fs.promises.mkdir(path.dirname(_getCacheFile()), { recursive: true });

        console.log('[SteamBridge] Starting Python bridge:', PYTHON_BIN, BRIDGE_SCRIPT);

        const BRIDGE_SRC_DIR = path.dirname(BRIDGE_SCRIPT);

        this._proc = spawn(PYTHON_BIN, [BRIDGE_SCRIPT], {
            stdio: ['pipe', 'pipe', 'pipe'],
            cwd: BRIDGE_SRC_DIR,
            env: {
                ...process.env,
                PYTHONUNBUFFERED: '1',
                PYTHONPATH: BRIDGE_SRC_DIR,
            },
        });

        this._proc.stdout.setEncoding('utf8');
        this._proc.stdout.on('data', (chunk) => this._onData(chunk));

        this._proc.stderr.setEncoding('utf8');
        this._proc.stderr.on('data', (chunk) => {
            // Python logs go to stderr — forward them for debugging
            chunk.split('\n').filter(Boolean).forEach(line =>
                console.log('[STEAM-PY]', line)
            );
        });

        this._proc.on('exit', (code, signal) => {
            console.warn(`[SteamBridge] Python process exited: code=${code} signal=${signal}`);
            this._proc  = null;
            this._ready = false;
            this._rejectAll('Steam bridge process exited unexpectedly');
            if (!this._shutdownRequested) {
                this.emit('disconnected', { code, signal });
            }
        });

        this._proc.on('error', (err) => {
            console.error('[SteamBridge] Spawn error:', err);
            this.emit('error', err);
        });

        // Tell the bridge to init and load persistent cache
        const cache = this._loadCache();
        const result = await this._call('start', { persistentCache: cache });
        this._ready = true;
        console.log('[SteamBridge] Bridge ready:', result);
        return result;
    }

    async stop() {
        this._shutdownRequested = true;
        if (this._proc) {
            try { await this._call('shutdown', {}); } catch {}
            this._proc.stdin.end();
            this._proc = null;
        }
        this._ready = false;
    }

    get isRunning() { return !!this._proc; }

    // ── API Methods ────────────────────────────────────────────

    /**
     * Authenticate with stored credentials or start fresh login.
     * Returns:
     *   { status: 'authenticated', steamId, personaName }
     *   { status: 'need_login',    loginUrl, endUriRegex }
     *   { status: 'need_2fa',      method, loginUrl, endUriRegex }
     *   { status: 'error',         message }
     */
    async authenticate(storedCredentials = null) {
        return this._call('authenticate', { storedCredentials });
    }

    /**
     * Call after the embedded WebView navigates to an end URI.
     * credentials: { end_uri, ...queryParams }
     */
    async passLoginCredentials(endUri, extraParams = {}) {
        return this._call('pass_login_credentials', { end_uri: endUri, ...extraParams });
    }

    /**
     * Returns full owned games list.
     * { status: 'success', games: [{ id, title, appid, platform }] }
     */
    async getOwnedGames() {
        return this._call('get_owned_games', {});
    }

    /**
     * { status: 'success', friends: [{ userId, username, avatarUrl, profileUrl }] }
     */
    async getFriends() {
        return this._call('get_friends', {});
    }

    /**
     * { status: 'success', achievements: { gameId: [{ name, unlockTime }] } }
     */
    async getAchievements(gameIds = []) {
        return this._call('get_achievements', { gameIds });
    }

    /**
     * { status: 'success', times: { appid: { timePlayed, lastPlayed } } }
     */
    async getGameTimes() {
        return this._call('get_game_times', {});
    }

    async ping() {
        return this._call('ping', {});
    }

    // ── Internal: JSON-RPC ─────────────────────────────────────

    _call(method, params, timeoutMs = 90_000) {
        return new Promise((resolve, reject) => {
            if (!this._proc) {
                return reject(new Error('Steam bridge is not running'));
            }

            const id = this._nextId++;
            let timer = null;

            timer = setTimeout(() => {
                this._pendingCalls.delete(id);
                reject(new Error(`[SteamBridge] Timeout waiting for "${method}" (${timeoutMs}ms)`));
            }, timeoutMs);

            this._pendingCalls.set(id, {
                resolve: (val) => { clearTimeout(timer); resolve(val); },
                reject:  (err) => { clearTimeout(timer); reject(err); },
            });

            const msg = JSON.stringify({ id, method, params }) + '\n';
            this._proc.stdin.write(msg);
        });
    }

    _onData(chunk) {
        this._buffer += chunk;
        const lines = this._buffer.split('\n');
        this._buffer = lines.pop(); // keep incomplete last line

        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            try {
                const msg = JSON.parse(trimmed);
                this._dispatch(msg);
            } catch (e) {
                console.warn('[SteamBridge] Could not parse line:', trimmed.slice(0, 120));
            }
        }
    }

    _dispatch(msg) {
        // Unsolicited event from Python
        if (msg.event) {
            this._onEvent(msg.event, msg.data);
            return;
        }

        // Response to a pending call
        const pending = this._pendingCalls.get(msg.id);
        if (!pending) {
            console.warn('[SteamBridge] No pending call for id:', msg.id);
            return;
        }
        this._pendingCalls.delete(msg.id);

        if (msg.error) {
            pending.reject(new Error(msg.error));
        } else {
            pending.resolve(msg.result);
        }
    }

    _onEvent(event, data) {
        switch (event) {
            case 'games_update':
                // New games found in background — emit so UI can update
                this.emit('gamesUpdate', data);
                break;

            case 'presence_update':
                this.emit('presenceUpdate', data);
                break;

            case 'store_credentials':
                // Python says credentials changed — persist them keyed by steamId
                // data should contain { steamId, ...credFields }
                if (data?.steamId) {
                    this._saveCredentialsForAccount(data.steamId, data);
                } else {
                    // fallback: old behaviour — save under legacy key so nothing breaks
                    this._saveCredentialsLegacy(data);
                }
                this.emit('credentialsChanged', data);
                break;

            default:
                this.emit(event, data);
        }
    }

    _rejectAll(reason) {
        for (const [id, pending] of this._pendingCalls) {
            pending.reject(new Error(reason));
        }
        this._pendingCalls.clear();
    }

    // ── Persistent Cache ───────────────────────────────────────

    _loadCache() {
        try {
            return JSON.parse(fs.readFileSync(_getCacheFile(), 'utf8'));
        } catch {
            return {};
        }
    }

    _writeCache(cache) {
        try {
            fs.writeFileSync(_getCacheFile(), JSON.stringify(cache, null, 2), 'utf8');
        } catch (e) {
            console.error('[SteamBridge] Failed to write cache:', e.message);
        }
    }

    // ── Multi-account credentials ──────────────────────────────

    /**
     * Save credentials for a specific Steam account.
     * Stored under cache._steamCredentialsByAccount[steamId]
     */
    _saveCredentialsForAccount(steamId, creds) {
        try {
            const cache = this._loadCache();
            if (!cache._steamCredentialsByAccount) cache._steamCredentialsByAccount = {};
            cache._steamCredentialsByAccount[String(steamId)] = creds;
            this._writeCache(cache);
        } catch (e) {
            console.error('[SteamBridge] Failed to save credentials for account:', e.message);
        }
    }

    /** Legacy single-account save — kept for backward compatibility */
    _saveCredentialsLegacy(creds) {
        try {
            const cache = this._loadCache();
            cache._steamCredentials = creds;
            this._writeCache(cache);
        } catch (e) {
            console.error('[SteamBridge] Failed to save credentials (legacy):', e.message);
        }
    }

    /**
     * Retrieve credentials for a specific steamId.
     * Falls back to the legacy single-account field if nothing is found per-account.
     */
    getCredentialsForAccount(steamId) {
        try {
            const cache = this._loadCache();
            const byAccount = cache._steamCredentialsByAccount || {};
            if (byAccount[String(steamId)]) return byAccount[String(steamId)];
            // fallback: legacy single-credential blob
            return cache._steamCredentials || null;
        } catch {
            return null;
        }
    }

    /**
     * Returns ALL stored per-account credentials as a plain object { steamId: creds }.
     */
    getAllSavedCredentials() {
        try {
            const cache = this._loadCache();
            return cache._steamCredentialsByAccount || {};
        } catch {
            return {};
        }
    }

    /**
     * @deprecated Use getCredentialsForAccount(steamId) instead.
     * Kept for any callers that haven't been updated yet.
     */
    getSavedCredentials() {
        try {
            const cache = this._loadCache();
            // Prefer the first per-account entry if it exists
            const byAccount = cache._steamCredentialsByAccount || {};
            const first = Object.values(byAccount)[0];
            return first || cache._steamCredentials || null;
        } catch {
            return null;
        }
    }

    /**
     * Remove stored credentials for a specific account (called on unlink).
     */
    deleteCredentialsForAccount(steamId) {
        try {
            const cache = this._loadCache();
            if (cache._steamCredentialsByAccount) {
                delete cache._steamCredentialsByAccount[String(steamId)];
            }
            this._writeCache(cache);
        } catch (e) {
            console.error('[SteamBridge] Failed to delete credentials:', e.message);
        }
    }
}

// ─── Singleton ────────────────────────────────────────────────
const bridge = new SteamBridge();

module.exports = bridge;
