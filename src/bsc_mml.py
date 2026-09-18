"""
BSC MML access for Cut Over evidence (``rlcrp``).

The BSC is not reachable from the laptop directly. From the ENM scripting VM
(the same SSH gateway every other step uses) the operator runs::

    ssh -p 22 <enm_user>@<bsc_ip>      -> Password: (ENM password)
    >mml
    <rlcrp:cell=M12098S3;              -> ... END
    <

This module scripts exactly that on one shell channel. It never logs or tees
anything while logging in: ``IntegrationSSH.send`` does not record input, and
no step log / live sink is attached to this session, so the password stays out
of every log file. The BSC does not echo the password either.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, List, Tuple

from terminal_renderer import strip_ansi


class BscMmlError(RuntimeError):
    """The BSC session could not be established or a printout failed."""


def _read_until_any(ssh, markers: Iterable[str], timeout: float) -> Tuple[str, str]:
    """Read the shell until any marker appears. Returns (text, marker_hit);
    marker_hit is "" on timeout / closed channel."""
    markers = [m.lower() for m in markers]
    buf = ""
    start = time.time()
    while time.time() - start < timeout:
        if ssh._channel_dead():
            break
        if ssh.shell.recv_ready():
            buf += ssh.shell.recv(65536).decode("utf-8", errors="replace")
            low = strip_ansi(buf).lower()
            for m in markers:
                if m in low:
                    time.sleep(0.3)  # let the rest of the line arrive
                    while ssh.shell.recv_ready():
                        buf += ssh.shell.recv(65536).decode("utf-8", errors="replace")
                    return buf, m
        else:
            time.sleep(0.2)
    return buf, ""


def _ends_with_prompt(text: str, prompt: str) -> bool:
    return strip_ansi(text).rstrip().endswith(prompt)


def _read_until_prompt(ssh, prompt: str, timeout: float) -> str:
    """Read until the (stripped) output ends with ``prompt`` (e.g. ``<``)."""
    buf = ""
    start = time.time()
    while time.time() - start < timeout:
        if ssh._channel_dead():
            break
        if ssh.shell.recv_ready():
            buf += ssh.shell.recv(65536).decode("utf-8", errors="replace")
            if _ends_with_prompt(buf, prompt):
                time.sleep(0.3)
                if not ssh.shell.recv_ready():
                    return buf
        else:
            time.sleep(0.2)
    raise BscMmlError(f"timed out waiting for the '{prompt}' prompt")


def run_rlcrp(ssh_factory: Callable[[], object], bsc_ip: str, user: str,
              password: str, cells: List[str], log: Callable[[str], None],
              ssh_port: int = 22, login_timeout: float = 45,
              cell_timeout: float = 90) -> List[Tuple[str, str]]:
    """Open one BSC MML session and run ``rlcrp:cell=<cell>;`` per cell.

    ``ssh_factory`` returns an unconnected ``IntegrationSSH`` to the ENM
    scripting VM. Returns ``[(cell, printout), …]`` in the given order.
    Raises :class:`BscMmlError` on login / MML failures; the session is always
    closed.
    """
    if not cells:
        return []
    ssh = ssh_factory()
    results: List[Tuple[str, str]] = []
    try:
        log(f"[BSC {bsc_ip}] connecting via the ENM scripting VM…")
        ssh.connect(timeout=30)
        ssh.send(f"ssh -p {int(ssh_port)} {user}@{bsc_ip}")
        text, hit = _read_until_any(
            ssh, ["(yes/no", "password:", "connection refused",
                  "no route to host", "could not resolve", "timed out"],
            login_timeout)
        if hit == "(yes/no":
            ssh.send("yes")
            text, hit = _read_until_any(ssh, ["password:"], login_timeout)
        if hit != "password:":
            tail = " ".join(strip_ansi(text).split())[-160:]
            raise BscMmlError(f"no password prompt from BSC {bsc_ip} ({tail or 'timeout'})")

        ssh.shell.send(password + "\n")          # never through a logged path
        text, hit = _read_until_any(
            ssh, ["password:", "permission denied", "welcome", ">"],
            login_timeout)
        if hit in ("password:", "permission denied"):
            raise BscMmlError(f"BSC {bsc_ip} rejected the login (check the ENM "
                              f"password / account access to the BSC)")
        if not hit:
            raise BscMmlError(f"no prompt from BSC {bsc_ip} after login")
        if not _ends_with_prompt(text, ">"):
            _read_until_prompt(ssh, ">", login_timeout)

        log(f"[BSC {bsc_ip}] logged in — entering MML…")
        ssh.send("mml")
        _read_until_prompt(ssh, "<", login_timeout)

        for cell in cells:
            log(f"[BSC {bsc_ip}] rlcrp:cell={cell};")
            ssh.send(f"rlcrp:cell={cell};")
            out = _read_until_prompt(ssh, "<", cell_timeout)
            clean = strip_ansi(out).replace("\r\n", "\n").replace("\r", "\n")
            # Drop the trailing MML prompt; keep the echoed command + printout.
            clean = clean.rstrip()
            if clean.endswith("<"):
                clean = clean[:-1].rstrip()
            results.append((cell, clean))
        try:
            ssh.send("exit;")
            ssh.send("exit")
        except Exception:
            pass
        return results
    except BscMmlError:
        raise
    except Exception as exc:
        raise BscMmlError(f"BSC {bsc_ip}: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            ssh.disconnect()
        except Exception:
            pass
