"""
baddel_bridge.py
================
Baddel Launcher — Steam Integration Bridge

Replaces GOG Galaxy's plugin.py entirely.
Communicates with Electron (Node.js) via JSON-RPC over stdin/stdout.

Protocol:
  Node → Python:  { "id": 1, "method": "authenticate", "params": {...} }
  Python → Node:  { "id": 1, "result": {...} }   (success)
                  { "id": 1, "error": "message" } (failure)

  Python can also push unsolicited events:
                  { "event": "games_update", "data": [...] }
                  { "event": "auth_step",    "data": {...} }
"""

import asyncio
import json
import logging
import ssl
import sys
import os
from typing import Optional, Dict, Any

import certifi

# ── Paths ─────────────────────────────────────────────────────────────────────
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="[STEAM-BRIDGE] %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,   # stderr → not mixed with JSON on stdout
)
logger = logging.getLogger("baddel_bridge")

# ── Internal imports (unchanged steam_network code) ───────────────────────────
from persistent_cache_state import PersistentCacheState
from steam_network.websocket_client import WebSocketClient
from steam_network.websocket_list import WebSocketList
from steam_network.steam_http_client import SteamHttpClient
from steam_network.friends_cache import FriendsCache
from steam_network.games_cache import GamesCache
from steam_network.stats_cache import StatsCache
from steam_network.times_cache import TimesCache
from steam_network.user_info_cache import UserInfoCache
from steam_network.authentication_cache import AuthenticationCache
from steam_network.local_machine_cache import LocalMachineCache
from steam_network.enums import (
    UserActionRequired, AuthCall, DisplayUriHelper, TwoFactorMethod
)
from steam_network.presence import presence_from_user_info
from steam_network.protocol.steam_types import ProtoUserInfo

# ── Replacements for galaxy.api types (simple dicts instead) ─────────────────
# We don't use GOG SDK at all — everything is plain Python dicts/JSON.

GAME_CACHE_IS_READY_TIMEOUT = 90
GAME_DOES_NOT_SUPPORT_LAST_PLAYED_VALUE = 86400

AVATAR_URL_TEMPLATE = "https://steamcdn-a.akamaihd.net/steamcommunity/public/images/avatars/{}/{}_full.jpg"
NO_AVATAR_SET = "0000000000000000000000000000000000000000"
DEFAULT_AVATAR_HASH = "fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb"

def avatar_url_from_hash(a_hash: str) -> str:
    if a_hash == NO_AVATAR_SET:
        a_hash = DEFAULT_AVATAR_HASH
    return AVATAR_URL_TEMPLATE.format(a_hash[:2], a_hash)


# ═════════════════════════════════════════════════════════════════════════════
# BaddelSteamBridge — Core logic
# ═════════════════════════════════════════════════════════════════════════════

class BaddelSteamBridge:
    def __init__(self, push_event_fn):
        """
        push_event_fn: async callable(event_name: str, data: Any)
            Used to push unsolicited events to Electron (e.g. new games found).
        """
        self._push_event = push_event_fn

        # ── SSL ───────────────────────────────────────────────────────────────
        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_context.load_verify_locations(certifi.where())

        # ── Caches ────────────────────────────────────────────────────────────
        self._persistent_storage_state = PersistentCacheState()
        self._persistent_cache: Dict[str, Any] = {}
        self._credentials: Dict[str, Any] = {}   # stored by Electron, passed back on restart

        self._authentication_cache = AuthenticationCache()
        self._user_info_cache = UserInfoCache()
        self._games_cache = GamesCache()
        self._stats_cache = StatsCache()
        self._times_cache = TimesCache()
        self._friends_cache = FriendsCache()
        self._translations_cache: Dict[int, str] = {}

        # ── Presence handler: push to Electron ────────────────────────────────
        async def _presence_handler(user_id: str, proto_user_info: ProtoUserInfo):
            presence = await presence_from_user_info(proto_user_info, self._translations_cache)
            await self._push_event("presence_update", {
                "userId": user_id,
                "status": presence.presence_state.value if presence.presence_state else None,
                "gameId": presence.game_id,
                "gameTitle": presence.game_title,
                "fullStatus": presence.full_status,
            })

        self._friends_cache.updated_handler = _presence_handler

        # ── HTTP client (aiohttp session) ─────────────────────────────────────
        import aiohttp
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None

        local_machine_cache = LocalMachineCache(self._persistent_cache, self._persistent_storage_state)

        # SteamHttpClient wraps aiohttp — we create it after session is ready
        self._local_machine_cache = local_machine_cache
        self._websocket_client: Optional[WebSocketClient] = None

        # Background tasks
        self._steam_run_task: Optional[asyncio.Task] = None
        self._update_games_task: asyncio.Task = asyncio.create_task(asyncio.sleep(0))
        self._owned_games_parsed = False

    # ── Session setup ─────────────────────────────────────────────────────────

    async def start(self):
        """Called once on startup to init the aiohttp session."""
        import aiohttp
        self._aiohttp_session = aiohttp.ClientSession()
        steam_http = _AiohttpSteamHttpClient(self._aiohttp_session)
        self._websocket_client = WebSocketClient(
            WebSocketList(steam_http),
            self._ssl_context,
            self._friends_cache,
            self._games_cache,
            self._translations_cache,
            self._stats_cache,
            self._times_cache,
            self._authentication_cache,
            self._user_info_cache,
            self._local_machine_cache,
        )
        logger.info("BaddelSteamBridge started")

    async def shutdown(self):
        if self._websocket_client:
            await self._websocket_client.close()
            await self._websocket_client.wait_closed()
        if self._aiohttp_session:
            await self._aiohttp_session.close()
        for task in [self._steam_run_task, self._update_games_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("BaddelSteamBridge shut down")

    # ── Tick (called periodically by event loop) ──────────────────────────────

    async def tick(self):
        """Push newly discovered games to Electron."""
        if self._update_games_task.done() and self._owned_games_parsed:
            self._update_games_task = asyncio.create_task(self._push_new_games())

        if self._user_info_cache.changed:
            creds = self._user_info_cache.to_dict()
            await self._push_event("store_credentials", creds)
            self._user_info_cache._changed = False

    async def _push_new_games(self):
        new_games = self._games_cache.consume_added_games()
        if not new_games:
            return
        games_list = [{"id": f"steam_{g.appid}", "title": g.title, "appid": str(g.appid)} for g in new_games]
        await self._push_event("games_update", games_list)

    # ── Authentication ────────────────────────────────────────────────────────

    async def authenticate(self, stored_credentials: Optional[Dict] = None) -> Dict:
        """
        Returns either:
          { "status": "need_login", "loginUrl": "...", "endUriRegex": "..." }
          { "status": "authenticated", "steamId": "...", "personaName": "..." }
        """
        # Load cache from disk if passed (Electron persists this between runs)
        if "games" in self._persistent_cache:
            self._games_cache.loads(self._persistent_cache["games"])

        # Start the WebSocket background task
        self._steam_run_task = asyncio.create_task(self._websocket_client.run())

        if stored_credentials:
            return await self._authenticate_with_token(stored_credentials)

        return self._need_login_response()

    async def _authenticate_with_token(self, creds: Dict) -> Dict:
        self._user_info_cache.from_dict(creds)
        if self._user_info_cache.is_initialized():
            await self._websocket_client.communication_queues["websocket"].put(
                {"mode": AuthCall.TOKEN}
            )
            result = await self._get_auth_step()
            if result == UserActionRequired.NoActionRequired:
                return self._authenticated_response()
            else:
                logger.info(f"Token login failed ({result}), falling back to password login")
                self._user_info_cache.Clear()

        return self._need_login_response()

    def _need_login_response(self) -> Dict:
        uri = DisplayUriHelper.LOGIN
        return {
            "status": "need_login",
            "loginUrl": uri.GetStartUri(),
            "endUriRegex": uri.GetEndUriRegex(),
        }

    def _authenticated_response(self) -> Dict:
        return {
            "status": "authenticated",
            "steamId": self._user_info_cache.steam_id,
            "personaName": self._user_info_cache.persona_name,
        }

    async def pass_login_credentials(self, end_uri: str, credentials: Dict) -> Dict:
        """
        Called by Electron when the embedded login page redirects.
        Returns same shape as authenticate().
        """
        if DisplayUriHelper.LOGIN.EndUri() in end_uri:
            return await self._handle_login_finished(credentials)
        elif DisplayUriHelper.TWO_FACTOR_MAIL.EndUri() in end_uri:
            return await self._handle_steam_guard(credentials, TwoFactorMethod.EmailCode, DisplayUriHelper.TWO_FACTOR_MAIL)
        elif DisplayUriHelper.TWO_FACTOR_MOBILE.EndUri() in end_uri:
            return await self._handle_steam_guard(credentials, TwoFactorMethod.PhoneCode, DisplayUriHelper.TWO_FACTOR_MOBILE)
        elif DisplayUriHelper.TWO_FACTOR_CONFIRM.EndUri() in end_uri:
            return await self._handle_steam_guard_check(DisplayUriHelper.TWO_FACTOR_CONFIRM, True)
        else:
            return {"status": "error", "message": f"Unknown end_uri: {end_uri}"}

    async def _get_auth_step(self) -> UserActionRequired:
        try:
            result = await asyncio.wait_for(
                self._websocket_client.communication_queues["plugin"].get(), 60
            )
            return result["auth_result"]
        except asyncio.TimeoutError:
            return UserActionRequired.InvalidAuthData

    @staticmethod
    def _sanitize(s: str) -> str:
        return (''.join(c for c in s if ord(c) < 128))[:64]

    async def _handle_login_finished(self, credentials: Dict) -> Dict:
        from urllib import parse
        parsed = parse.urlsplit(credentials.get("end_uri", ""))
        params = parse.parse_qs(parsed.query)

        if "password" not in params or "username" not in params:
            return {"status": "need_login", "loginUrl": DisplayUriHelper.LOGIN.GetStartUri(True), "endUriRegex": DisplayUriHelper.LOGIN.GetEndUriRegex()}

        user = params["username"][0]
        pwd = self._sanitize(params["password"][0])

        await self._websocket_client.communication_queues["websocket"].put(
            {"mode": AuthCall.RSA_AND_LOGIN, "username": user, "password": pwd}
        )
        result = await self._get_auth_step()

        if result == UserActionRequired.NoActionConfirmLogin:
            return await self._handle_steam_guard_none()
        elif result == UserActionRequired.TwoFactorRequired:
            allowed = self._authentication_cache.two_factor_allowed_methods
            method, msg = allowed[0]
            if method == TwoFactorMethod.Nothing:
                return await self._handle_steam_guard_none()
            elif method == TwoFactorMethod.PhoneCode:
                return {"status": "need_2fa", "method": "mobile", "loginUrl": DisplayUriHelper.TWO_FACTOR_MOBILE.GetStartUri(), "endUriRegex": DisplayUriHelper.TWO_FACTOR_MOBILE.GetEndUriRegex()}
            elif method == TwoFactorMethod.EmailCode:
                return {"status": "need_2fa", "method": "email", "loginUrl": DisplayUriHelper.TWO_FACTOR_MAIL.GetStartUri(), "endUriRegex": DisplayUriHelper.TWO_FACTOR_MAIL.GetEndUriRegex()}
            elif method == TwoFactorMethod.PhoneConfirm:
                return {"status": "need_2fa", "method": "confirm", "loginUrl": DisplayUriHelper.TWO_FACTOR_CONFIRM.GetStartUri(), "endUriRegex": DisplayUriHelper.TWO_FACTOR_CONFIRM.GetEndUriRegex()}
        return {"status": "need_login", "loginUrl": DisplayUriHelper.LOGIN.GetStartUri(True), "endUriRegex": DisplayUriHelper.LOGIN.GetEndUriRegex()}

    async def _handle_steam_guard(self, credentials: Dict, method: TwoFactorMethod, fallback: DisplayUriHelper) -> Dict:
        from urllib import parse
        parsed = parse.urlsplit(credentials.get("end_uri", ""))
        params = parse.parse_qs(parsed.query)

        if "code" not in params:
            return {"status": "need_2fa", "loginUrl": fallback.GetStartUri(True), "endUriRegex": fallback.GetEndUriRegex()}

        code = params["code"][0].strip()
        await self._websocket_client.communication_queues["websocket"].put(
            {"mode": AuthCall.UPDATE_TWO_FACTOR, "two-factor-code": code, "two-factor-method": method}
        )
        result = await self._get_auth_step()

        if result == UserActionRequired.NoActionConfirmLogin:
            return await self._handle_steam_guard_check(fallback, False)
        elif result == UserActionRequired.TwoFactorExpired:
            return {"status": "need_login", "loginUrl": DisplayUriHelper.LOGIN.GetStartUri(True, expired="true"), "endUriRegex": DisplayUriHelper.LOGIN.GetEndUriRegex()}
        return {"status": "need_2fa", "loginUrl": fallback.GetStartUri(True), "endUriRegex": fallback.GetEndUriRegex()}

    async def _handle_steam_guard_none(self) -> Dict:
        result = await self._poll_2fa()
        if result == UserActionRequired.NoActionRequired:
            return self._authenticated_response()
        elif result == UserActionRequired.NoActionConfirmToken:
            return await self._finish_auth()
        return {"status": "error", "message": "Unexpected auth state"}

    async def _handle_steam_guard_check(self, fallback: DisplayUriHelper, is_confirm: bool) -> Dict:
        result = await self._poll_2fa(is_confirm)
        if result == UserActionRequired.NoActionRequired:
            return self._authenticated_response()
        elif result == UserActionRequired.NoActionConfirmToken:
            return await self._finish_auth()
        elif result in (UserActionRequired.NoActionConfirmLogin, UserActionRequired.TwoFactorRequired):
            return {"status": "need_2fa", "loginUrl": fallback.GetStartUri(True), "endUriRegex": fallback.GetEndUriRegex()}
        elif result == UserActionRequired.TwoFactorExpired:
            return {"status": "need_login", "loginUrl": DisplayUriHelper.LOGIN.GetStartUri(True, expired="true"), "endUriRegex": DisplayUriHelper.LOGIN.GetEndUriRegex()}
        return {"status": "error", "message": "Unexpected auth state"}

    async def _poll_2fa(self, is_confirm: bool = False) -> UserActionRequired:
        await self._websocket_client.communication_queues["websocket"].put(
            {"mode": AuthCall.POLL_TWO_FACTOR, "is-confirm": is_confirm}
        )
        return await self._get_auth_step()

    async def _finish_auth(self) -> Dict:
        if self._user_info_cache.is_initialized():
            await self._websocket_client.communication_queues["websocket"].put({"mode": AuthCall.TOKEN})
            result = await self._get_auth_step()
            if result == UserActionRequired.NoActionRequired:
                return self._authenticated_response()
        return {"status": "error", "message": "Auth failed"}

    # ── Owned Games ───────────────────────────────────────────────────────────

    async def get_owned_games(self) -> Dict:
        """Returns full owned games list after cache is ready."""
        if not self._user_info_cache.steam_id:
            return {"status": "error", "message": "Not authenticated"}

        await self._games_cache.wait_ready(GAME_CACHE_IS_READY_TIMEOUT)
        self._games_cache.add_game_lever = True

        games = []
        async for app in self._games_cache.get_owned_games():
            games.append({"id": f"steam_{app.appid}", "title": app.title, "appid": str(app.appid), "platform": "steam"})

        self._owned_games_parsed = True
        self._persistent_cache["games"] = self._games_cache.dump()

        return {"status": "success", "games": games}

    # ── Friends ───────────────────────────────────────────────────────────────

    async def get_friends(self) -> Dict:
        if not self._user_info_cache.steam_id:
            return {"status": "error", "message": "Not authenticated"}

        friends_ids = await self._websocket_client.get_friends()
        friends_infos = await self._websocket_client.get_friends_info(friends_ids)

        friends = []
        for fid, info in friends_infos.items():
            friends.append({
                "userId": str(fid),
                "username": info.name,
                "avatarUrl": avatar_url_from_hash(info.avatar_hash.hex()),
                "profileUrl": f"https://steamcommunity.com/profiles/{fid}",
            })
        return {"status": "success", "friends": friends}

    # ── Achievements ──────────────────────────────────────────────────────────

    async def get_achievements(self, game_ids: list) -> Dict:
        if not self._user_info_cache.steam_id:
            return {"status": "error", "message": "Not authenticated"}

        await self._websocket_client.refresh_game_stats(game_ids)
        await self._stats_cache.wait_ready(10 * 60)

        result = {}
        for game_id in game_ids:
            raw = self._stats_cache.get(game_id, {})
            achievements = []
            for ach in raw.get("achievements", []):
                name = ach.get("name", "").strip() or ach.get("name", "")
                achievements.append({"name": name, "unlockTime": ach.get("unlock_time")})
            result[game_id] = achievements

        return {"status": "success", "achievements": result}

    # ── Game Time ─────────────────────────────────────────────────────────────

    async def get_game_times(self) -> Dict:
        if not self._user_info_cache.steam_id:
            return {"status": "error", "message": "Not authenticated"}

        await self._websocket_client.refresh_game_times()
        await self._times_cache.wait_ready(10 * 60)

        times = {}
        for game_id, data in self._times_cache._cache.items():
            last_played = data.get("last_played")
            if last_played == GAME_DOES_NOT_SUPPORT_LAST_PLAYED_VALUE:
                last_played = None
            times[str(game_id)] = {
                "timePlayed": data.get("time_played"),
                "lastPlayed": last_played,
            }
        return {"status": "success", "times": times}

    # ── Persistent cache (Electron passes this in on startup) ─────────────────

    def load_persistent_cache(self, cache: Dict):
        self._persistent_cache = cache
        if "games" in cache:
            self._games_cache.loads(cache["games"])
        logger.info("Persistent cache loaded")

    def get_persistent_cache(self) -> Dict:
        return self._persistent_cache


# ═════════════════════════════════════════════════════════════════════════════
# Minimal aiohttp-based SteamHttpClient (replaces galaxy.http dependency)
# ═════════════════════════════════════════════════════════════════════════════

class _AiohttpSteamHttpClient:
    """Wraps aiohttp.ClientSession to match SteamHttpClient's interface."""

    def __init__(self, session):
        self._session = session

    async def get_servers(self, cell_id: int):
        url = f"https://api.steampowered.com/ISteamDirectory/GetCMListForConnect/v1/?cellid={cell_id}&format=json"
        async with self._session.get(url) as resp:
            data = await resp.json()
            return [
                s["endpoint"]
                for s in data.get("response", {}).get("serverlist", [])
                if s.get("type") == "websockets"
            ]


# ═════════════════════════════════════════════════════════════════════════════
# JSON-RPC I/O loop
# ═════════════════════════════════════════════════════════════════════════════

class BaddelBridgeServer:
    """Reads JSON-RPC from stdin, dispatches to BaddelSteamBridge, writes results to stdout."""

    def __init__(self):
        self._bridge: Optional[BaddelSteamBridge] = None
        self._stdout_lock = asyncio.Lock()

    async def _send(self, obj: Dict):
        async with self._stdout_lock:
            line = json.dumps(obj) + "\n"
            sys.stdout.write(line)
            sys.stdout.flush()

    async def _push_event(self, event: str, data: Any):
        await self._send({"event": event, "data": data})

    async def _handle(self, req: Dict) -> Dict:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            result = await self._dispatch(method, params)
            return {"id": req_id, "result": result}
        except Exception as e:
            logger.exception(f"Error handling {method}")
            return {"id": req_id, "error": str(e)}

    async def _dispatch(self, method: str, params: Dict) -> Any:
        b = self._bridge

        if method == "ping":
            return "pong"

        elif method == "start":
            cache = params.get("persistentCache", {})
            await b.start()
            b.load_persistent_cache(cache)
            return {"status": "ok"}

        elif method == "authenticate":
            creds = params.get("storedCredentials")
            return await b.authenticate(creds)

        elif method == "pass_login_credentials":
            return await b.pass_login_credentials(
                params.get("end_uri", ""),
                params
            )

        elif method == "get_owned_games":
            return await b.get_owned_games()

        elif method == "get_friends":
            return await b.get_friends()

        elif method == "get_achievements":
            return await b.get_achievements(params.get("gameIds", []))

        elif method == "get_game_times":
            return await b.get_game_times()

        elif method == "get_persistent_cache":
            return b.get_persistent_cache()

        elif method == "shutdown":
            await b.shutdown()
            return {"status": "ok"}

        else:
            raise ValueError(f"Unknown method: {method}")

    async def run(self):
        self._bridge = BaddelSteamBridge(push_event_fn=self._push_event)

        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        logger.info("Baddel Steam Bridge ready — waiting for commands")

        tick_task = asyncio.create_task(self._tick_loop())

        try:
            while True:
                line = await reader.readline()
                if not line:
                    logger.info("stdin closed — shutting down")
                    break
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON: {e}")
                    continue

                # Handle each request in a separate task so we don't block
                asyncio.create_task(self._handle_and_send(req))

        finally:
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass
            if self._bridge:
                await self._bridge.shutdown()

    async def _handle_and_send(self, req: Dict):
        response = await self._handle(req)
        await self._send(response)

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(2)
            if self._bridge:
                try:
                    await self._bridge.tick()
                except Exception:
                    logger.exception("Error in tick")


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    server = BaddelBridgeServer()
    asyncio.run(server.run())
