"""Log handlers for the two entrypoints. Called from __main__, never on import.

Neither program writes a log file. Each one hands its records to the journal
and lets journald own storage, rotation and retention.
"""

import logging
import logging.handlers

APP = "dictation"

# The journal records the timestamp, the identifier and the priority itself,
# so the line only has to carry the message.
FORMAT = "%(message)s"

# Both keep to their own namespace, so their level is set there. At DEBUG they
# bury the daemon's own records under per-segment decoder output.
LIBRARY_LOGGERS = ("faster_whisper", "huggingface_hub")

# journald owns this socket; syslog is the way in for a process systemd did
# not start.
JOURNAL_SOCKET = "/dev/log"


def _install(handler: logging.Handler) -> None:
    handler.setFormatter(logging.Formatter(FORMAT))
    app = logging.getLogger(APP)
    # The handler belongs to the application logger rather than the root, so
    # nothing a library does to the root can redirect or duplicate these
    # records, and library records do not ride our handler on the way up.
    app.propagate = False
    app.addHandler(handler)
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_daemon_logging() -> None:
    """systemd starts the daemon, so its stderr is already the journal."""
    _install(logging.StreamHandler())


def setup_client_logging(ident: str) -> None:
    """The hotkey starts the client, outside systemd, so its stderr goes to
    the compositor's log or nowhere. Reach the journal directly instead."""
    handler = logging.handlers.SysLogHandler(address=JOURNAL_SOCKET)
    # journald reads the identifier off this prefix: journalctl -t <ident>.
    handler.ident = f"{ident}: "
    _install(handler)


def set_level(level: int) -> None:
    """Children inherit it, so this is the one level the config controls."""
    logging.getLogger(APP).setLevel(level)
