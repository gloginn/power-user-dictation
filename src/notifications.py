"""Desktop notifications through busctl, which ships with systemd and, unlike
notify-send, can close a notification by id."""

from concurrent.futures import ThreadPoolExecutor

import json
import logging
import subprocess
import time

log = logging.getLogger("dictation.notifications")
_silent_mode = False


def set_silent_mode(enabled: bool) -> None:
    global _silent_mode
    _silent_mode = enabled


SERVICE = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"

# Notify(app_name s, replaces_id u, app_icon s, summary s, body s, actions as,
#        hints a{sv}, expire_timeout i) -> id u. busctl spells an array as its
# length followed by the elements, so "0" is no actions and the hint below is
# one entry: key, variant type, value.
NOTIFY_SIGNATURE = "susssasa{sv}i"
NO_ACTIONS = "0"

URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2

ERROR_TIMEOUT_MS = 5000
STATUS_TIMEOUT_MS = 2000

# Some notification servers drop a notification whose app, summary, body
# and urgency match one received in the last few seconds. Two toggles in a row
# send the exact same "Recording..." payload, so the second banner would be
# dismissed before it ever renders. A zero-width suffix keeps the text identical
# on screen while making each payload distinct.
# The suffix spells the low 16 bits of the monotonic clock in milliseconds, so
# the client and the daemon stay distinct without sharing state.
NONCE_CHARS = ("\u200b", "\u200c")
NONCE_BITS = 16


def _nonce() -> str:
    """Return an invisible suffix that differs from any recent call's."""
    ticks = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1_000_000
    return "".join(NONCE_CHARS[(ticks >> i) & 1] for i in range(NONCE_BITS))


def _call(method: str, signature: str, *args: str) -> str | None:
    """None on any failure: notifications must never break recording."""
    if _silent_mode:
        return None
    try:
        result = subprocess.run(
            [
                "busctl", "--user", "--json=short", "call",
                SERVICE, OBJECT_PATH, SERVICE, method, signature, *args,
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notification %s unavailable: %s", method, e)
        return None
    if result.returncode != 0:
        log.warning("notification %s failed: %s", method, result.stderr.strip())
        return None
    return result.stdout


def _send(
    message: str,
    urgency: int = URGENCY_NORMAL,
    expire_timeout: int = 0,
) -> int | None:
    out = _call(
        "Notify",
        NOTIFY_SIGNATURE,
        "Dictation", "0", "", "Dictation", message + _nonce(),
        NO_ACTIONS,
        "1", "urgency", "y", str(urgency),
        str(expire_timeout),
    )
    if not out:
        return None
    try:
        return json.loads(out)["data"][0]
    except (ValueError, KeyError, IndexError):
        log.warning("could not read a notification id from %r", out)
        return None


def notify_recording_start(language: str | None = None) -> int | None:
    message = "Recording..."
    if language:
        message = f"Recording... {language.upper()}"
    return _send(message, urgency=URGENCY_CRITICAL)


def notify_recording_stop(notify_id: int | None):
    if notify_id:
        _call("CloseNotification", "u", str(notify_id))


def notify_error(message: str):
    _send(message, urgency=URGENCY_CRITICAL, expire_timeout=ERROR_TIMEOUT_MS)


def notify_status(message: str):
    _send(message, urgency=URGENCY_NORMAL, expire_timeout=STATUS_TIMEOUT_MS)


class NotificationQueue:
    """Serialize daemon banners off the event loop, including their close IDs."""

    def __init__(self):
        self._worker = ThreadPoolExecutor(max_workers=1)
        self._recording_id: int | None = None

    def recording_start(self, language: str):
        self._worker.submit(self._start, language)

    def _start(self, language: str):
        self._recording_id = notify_recording_start(language)

    def recording_stop(self):
        self._worker.submit(self._stop)

    def _stop(self):
        notify_recording_stop(self._recording_id)
        self._recording_id = None

    def error(self, message: str):
        self._worker.submit(notify_error, message)

    async def close(self):
        # Imported here so the client, which never reaches this, skips the
        # asyncio import on every keypress.
        import asyncio

        await asyncio.to_thread(self._worker.shutdown, wait=True)
