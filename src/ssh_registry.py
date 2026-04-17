"""Process-wide registry of live SSH sessions.

Both :class:`integration_runner.IntegrationSSH` and
:class:`ssh_runner.MoshellSession` register themselves on connect and
unregister on disconnect. When the main window is closed, the app calls
:func:`kill_all` to force-close every live channel+transport so worker
threads blocked in ``shell.recv()`` unwind immediately and the process
can exit cleanly instead of lingering.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_sessions: set[Any] = set()


def register(session: Any) -> None:
    with _lock:
        _sessions.add(session)


def unregister(session: Any) -> None:
    with _lock:
        _sessions.discard(session)


def _force_close(session: Any) -> None:
    """Close shell + transport on a session, ignoring all errors."""
    shell = getattr(session, "shell", None)
    client = getattr(session, "client", None)
    if shell is not None:
        try:
            shell.close()
        except Exception:
            pass
    if client is not None:
        try:
            t = client.get_transport()
            if t is not None:
                t.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
    try:
        session._connected = False
    except Exception:
        pass


def kill_all() -> int:
    """Force-close every registered SSH session. Returns count killed."""
    with _lock:
        snap = list(_sessions)
        _sessions.clear()
    for s in snap:
        _force_close(s)
    if snap:
        logger.info("ssh_registry: killed %d live session(s)", len(snap))
    return len(snap)
