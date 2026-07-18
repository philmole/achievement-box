"""ctypes bridge to rcheevos rc_client (daemon/lib/rcheevos.dll).

rc_client drives everything; we supply two callbacks:
  read_memory  -> serves RA-address-space reads from the FPGA WRAM shadow
                  (RA Mega Drive address 0x0000-0xFFFF == 68K $FF0000+)
  server_call  -> synchronous HTTPS POST to retroachievements.org

Events (achievement unlocks etc.) arrive via the event handler during
rc_client_do_frame().
"""

from __future__ import annotations

import ctypes as C
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import sys

from achievementbox.version import USER_AGENT
from achievementbox.offline_queue import (DEFAULT_QUEUE_DIR,
                                          OfflineSubmissionQueue)
from achievementbox.ra_status import mode_from_native

# per-platform rc_client library: MSVC build on Windows, gcc build on
# the Pi (daemon/lib/build_rcheevos.sh)
_LIB_NAME = "rcheevos.dll" if sys.platform == "win32" else "librcheevos.so"
DLL_PATH = Path(__file__).parent.parent / "lib" / _LIB_NAME

# rc_client.h enums
EVENT_ACHIEVEMENT_TRIGGERED = 1
EVENT_GAME_COMPLETED = 15

WRAM_SIZE = 0x10000
RETRYABLE_CLIENT_ERROR = -2
SUBMISSION_OPERATIONS = frozenset({"awardachievement", "submitlbentry"})


class RcApiRequest(C.Structure):
    _fields_ = [
        ("url", C.c_char_p),
        ("post_data", C.c_char_p),
        ("content_type", C.c_char_p),
        # rc_buffer_t storage follows; opaque to us
    ]


class RcApiServerResponse(C.Structure):
    _fields_ = [
        ("body", C.c_char_p),
        ("body_length", C.c_size_t),
        ("http_status_code", C.c_int),
    ]


class RcClientAchievement(C.Structure):
    # rc_client.h:554-574 (rcheevos 12.3) -- full layout, verified.
    _fields_ = [
        ("title", C.c_char_p),
        ("description", C.c_char_p),
        ("badge_name", C.c_char * 8),
        ("measured_progress", C.c_char * 24),
        ("measured_percent", C.c_float),
        ("id", C.c_uint32),
        ("points", C.c_uint32),
        ("unlock_time", C.c_uint64),  # time_t on win64
        ("state", C.c_uint8),
        ("category", C.c_uint8),
        ("bucket", C.c_uint8),
        ("unlocked", C.c_uint8),
        ("rarity", C.c_float),
        ("rarity_hardcore", C.c_float),
        ("type", C.c_uint8),
        ("badge_url", C.c_char_p),
        ("badge_locked_url", C.c_char_p),
    ]


class RcClientAchievementBucket(C.Structure):
    # rc_client.h:592-599 -- verified.
    _fields_ = [
        ("achievements", C.POINTER(C.POINTER(RcClientAchievement))),
        ("num_achievements", C.c_uint32),
        ("label", C.c_char_p),
        ("subset_id", C.c_uint32),
        ("bucket_type", C.c_uint8),
    ]


class RcClientAchievementList(C.Structure):
    _fields_ = [
        ("buckets", C.POINTER(RcClientAchievementBucket)),
        ("num_buckets", C.c_uint32),
    ]


class RcClientEvent(C.Structure):
    _fields_ = [
        ("type", C.c_uint32),
        ("achievement", C.POINTER(RcClientAchievement)),
        ("leaderboard", C.c_void_p),
        ("leaderboard_tracker", C.c_void_p),
        ("leaderboard_scoreboard", C.c_void_p),
        ("server_error", C.c_void_p),
        ("subset", C.c_void_p),
    ]


READ_MEMORY_FUNC = C.CFUNCTYPE(C.c_uint32, C.c_uint32,
                               C.POINTER(C.c_uint8), C.c_uint32, C.c_void_p)
SERVER_CALLBACK = C.CFUNCTYPE(None, C.POINTER(RcApiServerResponse), C.c_void_p)
SERVER_CALL_FUNC = C.CFUNCTYPE(None, C.POINTER(RcApiRequest),
                               SERVER_CALLBACK, C.c_void_p, C.c_void_p)
GENERIC_CALLBACK = C.CFUNCTYPE(None, C.c_int, C.c_char_p, C.c_void_p, C.c_void_p)
EVENT_HANDLER = C.CFUNCTYPE(None, C.POINTER(RcClientEvent), C.c_void_p)
MESSAGE_CALLBACK = C.CFUNCTYPE(None, C.c_char_p, C.c_void_p)


class RcClient:
    """Owns the rc_client_t and the Python callbacks (kept alive here)."""

    def __init__(self, read_wram: Callable[[int, int], bytes],
                 on_event: Callable[[str, dict], None],
                 log: Callable[[str], None] = print,
                 queue_dir: Path = DEFAULT_QUEUE_DIR):
        self._dll = C.CDLL(str(DLL_PATH))
        self._read_wram = read_wram
        self._on_event = on_event
        self._log = log
        self._async_results: dict[str, tuple[int, str]] = {}
        self.response_log: list[tuple[str, bytes]] | None = None  # debug tap
        self._submission_queue = OfflineSubmissionQueue(queue_dir)
        # rc_client retries new transport failures itself. Only records which
        # survived a previous process need independent replay here.
        self._startup_queue_ids = self._submission_queue.ids()

        d = self._dll
        d.rc_client_create.restype = C.c_void_p
        d.rc_client_create.argtypes = [READ_MEMORY_FUNC, SERVER_CALL_FUNC]

        # keep callback objects referenced for the client's lifetime
        self._cb_read = READ_MEMORY_FUNC(self._c_read_memory)
        self._cb_server = SERVER_CALL_FUNC(self._c_server_call)
        self._cb_event = EVENT_HANDLER(self._c_event)
        self._cb_log = MESSAGE_CALLBACK(self._c_log)

        self._client = d.rc_client_create(self._cb_read, self._cb_server)
        if not self._client:
            raise RuntimeError("rc_client_create failed")

        d.rc_client_set_event_handler.argtypes = [C.c_void_p, EVENT_HANDLER]
        d.rc_client_set_event_handler(self._client, self._cb_event)
        d.rc_client_enable_logging.argtypes = [C.c_void_p, C.c_int,
                                               MESSAGE_CALLBACK]
        d.rc_client_enable_logging(self._client, 3, self._cb_log)  # WARN
        d.rc_client_set_hardcore_enabled.argtypes = [C.c_void_p, C.c_uint32]
        d.rc_client_set_hardcore_enabled(self._client, 0)  # Casual, per plan

        # Query rather than infer so daemon/UI state reflects the native client.
        # If the ABI cannot report a trustworthy value, destroy the partially
        # constructed client and refuse to start an RA session.
        try:
            self._mode = self._read_native_mode()
        except Exception:
            d.rc_client_destroy.argtypes = [C.c_void_p]
            d.rc_client_destroy(self._client)
            self._client = None
            raise

    # -- C callbacks -------------------------------------------------
    def _c_read_memory(self, address, buffer, num_bytes, _client):
        if address >= WRAM_SIZE:
            return 0  # only 68K WRAM is shadowed
        n = min(num_bytes, WRAM_SIZE - address)
        try:
            data = self._read_wram(address, n)
        except Exception as e:  # a dropped USB read must not kill the frame
            self._log(f"read_memory 0x{address:x} failed: {e}")
            return 0
        # never write past the buffer rc_client gave us (heap corruption
        # from a native overrun surfaces as crashes ANYWHERE later)
        n = min(len(data), n)
        C.memmove(buffer, data, n)
        return n

    @staticmethod
    def _is_submission(url: str, post_data: bytes | None) -> bool:
        operations = parse_qs(urlsplit(url).query).get("r", [])
        if post_data:
            try:
                operations.extend(parse_qs(
                    post_data.decode("utf-8", "strict")).get("r", []))
            except UnicodeDecodeError:
                return False
        return any(operation.casefold() in SUBMISSION_OPERATIONS
                   for operation in operations)

    @staticmethod
    def _http_request(url: str, post_data: bytes | None,
                      content_type: str) -> tuple[bytes | None, int]:
        try:
            http = urllib.request.Request(
                url,
                data=post_data,
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": content_type},
            )
            with urllib.request.urlopen(http, timeout=20) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            return e.read(), e.code

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return status in (408, 429) or status >= 500

    def _flush_startup_queue(self) -> None:
        def corrupt(item_id):
            self._startup_queue_ids.discard(item_id)
            self._log(f"discarded tampered offline submission {item_id[:12]}")

        items = self._submission_queue.pending(
            self._startup_queue_ids, on_corrupt=corrupt)
        for item in items:
            try:
                _body, status = self._http_request(
                    item.url, item.post_data, item.content_type)
            except Exception as e:
                self._log(f"offline submission replay deferred: {e}")
                break
            if self._is_retryable_status(status):
                self._log(f"offline submission replay deferred ({status})")
                break
            # Any non-retryable HTTP response proves the request reached the
            # RA service. Authentication/client errors will not improve by
            # resending an old payload indefinitely.
            self._submission_queue.remove(item.id)
            self._startup_queue_ids.discard(item.id)
            self._log(f"replayed offline submission ({status})")

    def _c_server_call(self, request, callback, callback_data, _client):
        req = request.contents
        url = req.url.decode("utf-8", "strict")
        post_data = req.post_data if req.post_data else None
        content_type = (req.content_type or
                        b"application/x-www-form-urlencoded").decode()
        queue_id = None
        if self._is_submission(url, post_data):
            queue_id = self._submission_queue.enqueue(
                url, post_data, content_type)

        try:
            body, status = self._http_request(url, post_data, content_type)
        except Exception as e:
            self._log(f"server_call failed: {e}")
            body, status = None, RETRYABLE_CLIENT_ERROR
        else:
            if (queue_id is not None
                    and not self._is_retryable_status(status)):
                self._submission_queue.remove(queue_id)
                self._startup_queue_ids.discard(queue_id)
            if 200 <= status < 300 and self._startup_queue_ids:
                self._flush_startup_queue()

        if self.response_log is not None:
            self.response_log.append((url, body or b""))
        resp = RcApiServerResponse(body=body, body_length=len(body or b""),
                                   http_status_code=status)
        callback(C.byref(resp), callback_data)

    def _c_event(self, event, _client):
        ev = event.contents
        if ev.type == EVENT_ACHIEVEMENT_TRIGGERED and ev.achievement:
            a = ev.achievement.contents
            self._on_event("unlock", {
                "id": a.id,
                "title": (a.title or b"?").decode("utf-8", "replace"),
                "description": (a.description or b"").decode("utf-8", "replace"),
                "points": a.points,
                "badge": bytes(a.badge_name).split(b"\0")[0].decode(),
            })
        elif ev.type == EVENT_GAME_COMPLETED:
            self._on_event("mastered", {})
        else:
            self._on_event(f"event_{ev.type}", {})

    def _c_log(self, message, _client):
        self._log(f"[rc] {(message or b'').decode('utf-8', 'replace')}")

    # -- blocking wrappers over the async API ------------------------
    def _wait_async(self, key, begin_fn, *args):
        done = {}

        @GENERIC_CALLBACK
        def cb(result, error_message, _client, _userdata):
            done["result"] = result
            done["error"] = (error_message or b"").decode("utf-8", "replace")

        begin_fn(self._client, *args, cb, None)
        # server_call is synchronous, so the callback has fired already
        if "result" not in done:
            raise RuntimeError(f"{key}: callback did not fire")
        if done["result"] != 0:
            raise RuntimeError(f"{key} failed: {done['error']}")

    def login(self, username: str, password: str):
        fn = self._dll.rc_client_begin_login_with_password
        fn.restype = C.c_void_p
        fn.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p,
                       GENERIC_CALLBACK, C.c_void_p]
        self._wait_async("login", fn, username.encode(), password.encode())

    def load_game(self, md5: str):
        fn = self._dll.rc_client_begin_load_game
        fn.restype = C.c_void_p
        fn.argtypes = [C.c_void_p, C.c_char_p, GENERIC_CALLBACK, C.c_void_p]
        self._wait_async("load_game", fn, md5.encode())

    def game_info(self) -> dict:
        fn = self._dll.rc_client_get_game_info
        fn.restype = C.c_void_p
        fn.argtypes = [C.c_void_p]
        ptr = fn(self._client)
        if not ptr:
            return {}

        class GameInfo(C.Structure):
            # rc_client_game_t, rc_client.h:309-317 -- verified. The
            # `hash` member matters: omitting it shifted every later
            # field (icon became the badge NAME, not a URL).
            _fields_ = [("id", C.c_uint32), ("console_id", C.c_uint32),
                        ("title", C.c_char_p), ("hash", C.c_char_p),
                        ("badge_name", C.c_char_p),
                        ("badge_url", C.c_char_p)]

        g = C.cast(ptr, C.POINTER(GameInfo)).contents
        badge = (g.badge_name or b"").decode("utf-8", "replace")
        icon = (g.badge_url or b"").decode("utf-8", "replace")
        if not icon.startswith("http") and badge:
            icon = f"https://media.retroachievements.org/Images/{badge}.png"
        return {"id": g.id, "console_id": g.console_id,
                "title": (g.title or b"?").decode("utf-8", "replace"),
                "badge": badge, "icon": icon}

    def achievement_list(self) -> list[dict]:
        """The core achievement set straight from rc_client (not scraped
        from HTTP): id, title, description, points, badge, unlocked."""
        CORE = 1              # RC_CLIENT_ACHIEVEMENT_CATEGORY_CORE
        BY_LOCK = 0           # RC_CLIENT_ACHIEVEMENT_LIST_GROUPING_LOCK_STATE
        fn = self._dll.rc_client_create_achievement_list
        fn.restype = C.POINTER(RcClientAchievementList)
        fn.argtypes = [C.c_void_p, C.c_int, C.c_int]
        ptr = fn(self._client, CORE, BY_LOCK)
        if not ptr:
            return []
        out = []
        try:
            lst = ptr.contents
            for bi in range(lst.num_buckets):
                bucket = lst.buckets[bi]
                for ai in range(bucket.num_achievements):
                    a = bucket.achievements[ai].contents
                    out.append({
                        "id": a.id,
                        "title": (a.title or b"").decode("utf-8", "replace"),
                        "description": (a.description or b"").decode(
                            "utf-8", "replace"),
                        "points": a.points,
                        "badge": bytes(a.badge_name).split(b"\0")[0]
                            .decode("ascii", "replace"),
                        "badge_url": (a.badge_url or b"").decode(
                            "utf-8", "replace"),
                        "badge_locked_url": (a.badge_locked_url or b"")
                            .decode("utf-8", "replace"),
                        "unlocked": bool(a.unlocked),
                    })
        finally:
            self._dll.rc_client_destroy_achievement_list.argtypes = [
                C.POINTER(RcClientAchievementList)]
            self._dll.rc_client_destroy_achievement_list(ptr)
        return out

    def summary(self) -> dict:
        class Summary(C.Structure):
            _fields_ = [("num_core_achievements", C.c_uint32),
                        ("num_unofficial_achievements", C.c_uint32),
                        ("num_unlocked_achievements", C.c_uint32),
                        ("num_unsupported_achievements", C.c_uint32),
                        ("points_core", C.c_uint32),
                        ("points_unlocked", C.c_uint32),
                        # rcheevos 12.1+: omitting these lets the native
                        # function write beyond the ctypes allocation.
                        ("beaten_time", C.c_int64),
                        ("completed_time", C.c_int64)]

        s = Summary()
        fn = self._dll.rc_client_get_user_game_summary
        fn.argtypes = [C.c_void_p, C.POINTER(Summary)]
        fn(self._client, C.byref(s))
        return {"total": s.num_core_achievements,
                "unlocked": s.num_unlocked_achievements,
                "unsupported": s.num_unsupported_achievements,
                "points": s.points_core}

    def unload_game(self):
        """Stop evaluating the current set (console back at menu)."""
        self._dll.rc_client_unload_game.argtypes = [C.c_void_p]
        self._dll.rc_client_unload_game(self._client)

    def do_frame(self):
        self._dll.rc_client_do_frame.argtypes = [C.c_void_p]
        self._dll.rc_client_do_frame(self._client)

    def rich_presence(self) -> str:
        """Return the Rich Presence text evaluated by rc_client.

        rc_client owns parsing the set's Rich Presence script and sending
        periodic pings to RetroAchievements.  This is only a read-only view
        of the current evaluated message for local presentation.
        """
        fn = self._dll.rc_client_get_rich_presence_message
        fn.restype = C.c_size_t
        fn.argtypes = [C.c_void_p, C.POINTER(C.c_char), C.c_size_t]
        buffer = C.create_string_buffer(256)
        fn(self._client, buffer, len(buffer))
        return buffer.value.decode("utf-8", "replace")

    def idle(self):
        self._dll.rc_client_idle.argtypes = [C.c_void_p]
        self._dll.rc_client_idle(self._client)

    def _read_native_mode(self) -> str:
        fn = self._dll.rc_client_get_hardcore_enabled
        fn.restype = C.c_int
        fn.argtypes = [C.c_void_p]
        return mode_from_native(int(fn(self._client)))

    @property
    def mode(self) -> str:
        """Mode confirmed by the native rc_client after configuration."""
        current = self._read_native_mode()
        if current != self._mode:
            raise RuntimeError("native RA mode changed outside the mode flow")
        return current

    def close(self):
        if self._client:
            self._dll.rc_client_destroy.argtypes = [C.c_void_p]
            self._dll.rc_client_destroy(self._client)
            self._client = None
