"""
Integration workflow — SSH session for running integration scripts.
Provides an interactive shell that can handle prompted inputs and
verification commands.
"""
import json
import logging
import os
import re
import time
import uuid
from typing import Callable, Optional

import paramiko
import relation_journal

logger = logging.getLogger(__name__)

# ── Load dynamic config ─────────────────────────────────────────
# Lookup order for ``config.json``:
#   1. ``<exe_dir>/config.json``  — user's editable copy next to the
#      exe (or project root in dev mode). This is the file operators
#      actually edit to point at different ENM script paths.
#   2. ``<this_file_dir>/config.json`` — bundled default that ships
#      inside the PyInstaller _MEIPASS. Used as fallback when the
#      user-editable file is missing (typical on a fresh exe install,
#      until ``ensure_assets_in_app_dir`` seeds a copy beside the exe).
def _resolve_config_path() -> str:
    candidates = []
    try:
        from app_path import get_app_dir
        candidates.append(os.path.join(get_app_dir(), "config.json"))
    except Exception:
        pass
    candidates.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    )
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]  # for the warning log


_CONFIG_PATH = _resolve_config_path()


def _load_config() -> dict:
    """Load config.json. Returns defaults if file is missing."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            logger.info(f"Loaded integration config from {_CONFIG_PATH}")
            return cfg
    except Exception:
        logger.warning(
            f"config.json not found at {_CONFIG_PATH} — using built-in defaults."
        )
        return {}


_CFG = _load_config()

# ── Script path resolution ──────────────────────────────────────
# Every script path in config.json should be a FULL absolute path
# (starts with ``/``). Operators can point each script at any
# directory — they don't all need to live under the same root.
#
# For backward compatibility, if a value does NOT start with ``/``
# we treat it as relative to ``scripts_path`` (the legacy behaviour).
# Old config.json files with bare names like ``"ES/create_arne.py"``
# keep working, but the recommended form is the explicit full path.
SCRIPTS_PATH = _CFG.get("scripts_path", "/home/shared/ESETARI/INOC/SCRIPTS")


def _resolve_script_path(value: str) -> str:
    """Return ``value`` unchanged if it's already an absolute Unix
    path; otherwise prepend ``SCRIPTS_PATH`` (legacy behaviour)."""
    if not value:
        return value
    if value.startswith("/"):
        return value
    return f"{SCRIPTS_PATH}/{value}"


CLI_PY = _resolve_script_path(
    _CFG.get("cli_py", "/home/shared/ESETARI/INOC/SCRIPTS/cli.py")
)

# Enrollment / ARNE scripts
_CREATE_ARNE = _resolve_script_path(
    _CFG.get("create_arne_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/ES/create_arne_2.py")
)
_ENTITY_MAKER = _resolve_script_path(
    _CFG.get("entity_maker_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/ES/entity_maker.sh")
)
_EXE_ENTITY = _resolve_script_path(
    _CFG.get("exe_entity_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/ES/exe_entity.py")
)
_ENROLLMENT_MOS = _resolve_script_path(
    _CFG.get("enrollment_mos",
             "/home/shared/ESETARI/INOC/SCRIPTS/ES/enroll/lhgenm1.mos")
)

# SGW reachability check
_SGW_CHECK_MOS = _resolve_script_path(
    _CFG.get("sgw_check_mos",
             "/home/shared/ESETARI/INOC/SCRIPTS/SGW_Check.mos")
)

# New comprehensive ping tests. Two distinct scripts now:
#   * LTE/NR — backhaul + MME + SGW reachability
#   * GSM    — BSC broker IP reachability
# When a single node hosts BOTH (co-located LTE+GSM / NR+GSM, no
# separate GSM DN) we run BOTH scripts back-to-back on that one
# node; the parser merges results so the 4-level status still
# reflects the combined outcome.
_PING_TEST_LTE_NR = _resolve_script_path(
    _CFG.get("ping_test_lte_nr",
             "/home/shared/ESETARI/INOC/SCRIPTS/DM/ping.txt")
)
_PING_TEST_GSM = _resolve_script_path(
    _CFG.get("ping_test_gsm",
             "/home/shared/common/INTEGRATION_TEAM/script/"
             "Ping_Test_BSC_brokerIP.txt")
)

# LKF management scripts (typically at SCRIPTS_PATH root)
_LKF_IMPORT = _resolve_script_path(
    _CFG.get("lkf_import_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/lkfimport.py")
)
_LKF_INSTALL = _resolve_script_path(
    _CFG.get("lkf_install_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/lkfinstall.py")
)
_LKF_STATUS = _resolve_script_path(
    _CFG.get("lkf_status_script",
             "/home/shared/ESETARI/INOC/SCRIPTS/lkfstatus.py")
)

# Where ENM's SMRS service stores per-node license files. Configurable
# in case a deployment uses a non-default path.
_SMRS_LICENCE_DIR = _CFG.get(
    "smrs_licence_dir",
    "/ericsson/tor/smrs/smrsroot/licence",
)


def _find_node_lkf_in_zip(zip_path: str, node_name: str) -> Optional[str]:
    """Open ``zip_path`` locally and find the per-node LKF XML inside.

    Convention: the zip contains one file per node, named like
    ``<NODE_NAME>_<fingerprint>.xml``. A sibling
    ``<NODE_NAME>_<fingerprint>_info.xml`` may also be present —
    that's the metadata file, NOT the license, and we explicitly
    skip it.

    Match is case-insensitive on the leading node name so casing
    differences between the zip and the node DN don't cause a miss.

    Returns the bare filename (no path) if found, else ``None``.
    """
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except Exception as exc:
        logger.warning(f"[lkf] cannot open zip {zip_path}: {exc}")
        return None

    node_lower = node_name.lower()
    candidates: list[str] = []
    for name in names:
        base = os.path.basename(name)
        low = base.lower()
        if not low.endswith(".xml"):
            continue
        if low.endswith("_info.xml"):
            continue
        # Either starts with <node>_ or contains the node name as a
        # leading token. Strict prefix match avoids picking another
        # node's file when this node's name is a substring.
        if low.startswith(node_lower + "_") or low == node_lower + ".xml":
            candidates.append(base)
    if not candidates:
        return None
    # If multiple non-info XMLs match, prefer the lexicographically
    # last one (typically the newest fingerprint).
    return sorted(candidates)[-1]


def _lkf_already_on_smrs(
    ssh: "IntegrationSSH",
    node_name: str,
    lkf_filename: str,
    log_cb: Callable[[str], None],
) -> bool:
    """Check whether ``<smrs_dir>/<node_name>/<lkf_filename>`` exists
    on the gateway. Returns True only if an exact-name match is
    present (so a stale file with a different fingerprint won't be
    treated as 'already installed')."""
    remote_path = f"{_SMRS_LICENCE_DIR}/{node_name}/{lkf_filename}"
    log_cb(f"Checking if LKF already on SMRS: {remote_path}")
    # ``! ls -1 <path> 2>/dev/null`` prints the path on success,
    # nothing on failure. Then we just look for the filename in
    # the output.
    out = ssh.run_amos_command_safe(
        f"!ls -1 '{remote_path}' 2>/dev/null",
        node_name, timeout=15,
    )
    return lkf_filename in out

_BASELINE_SCRIPT = _resolve_script_path(_CFG.get(
    "baseline_script_path",
    "/home/shared/common/INTEGRATION_TEAM/script/Baseline_script.mos",
))

_BASELINE_FILES = _CFG.get("baseline_files", {
    "L":  "Globe_Baseline_L_NonModular_Rev_15042026.mos",
    "LN": "Globe_Baseline_LN_NonModular_Rev_15042026.mos",
    "N":  "Globe_Baseline_LN_NonModular_Rev_15042026.mos",
})

_URI_CFG = _CFG.get("uri_setting", {})
ENM_LOGIN_URL = _URI_CFG.get(
    "enm_login_url", "https://lhgenm1.globetel.com/login")
ENM_URI_UPDATE_URL = _URI_CFG.get(
    "enm_uri_update_url",
    "https://lhgenm1.globetel.com/oss/shm/rest/softwarePackage/"
    "updateUpMoFtpServerDetails")
_UPGRADE_PKG_ID = _URI_CFG.get("upgrade_package_id", "CXP2010174/2-R42J13")

# BSC name → expected broker IP (config.json ``bsc_broker_map``). The GSM
# SGW check verifies the node's own AbisIp bscBrokerIpAddress against this,
# so a node configured with the WRONG BSC's broker (ping still succeeds!)
# gets flagged instead of passing silently. Keys upper-cased for lookup.
_BSC_BROKER_MAP = {
    str(k).upper(): str(v).strip()
    for k, v in _CFG.get("bsc_broker_map", {
        "MINBS00": "10.14.194.131",
        "MINBS01": "10.14.204.3",
    }).items()
}

ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]|\x1b\(B"
)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _is_shell_prompt(line: str) -> bool:
    """Check if line looks like a bash prompt (ends with $ or # or ])."""
    s = line.rstrip()
    return bool(s) and (s.endswith("$") or s.endswith("#") or s.endswith("]"))


class IntegrationSSH:
    """Lightweight SSH session for integration scripts.

    Unlike MoshellSession, this stays in a normal bash shell and can
    handle interactive prompts (e.g. create_arne.py asking for nodename).
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._log = log_callback or (lambda msg: None)
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self._connected = False
        # Live session-log tee — when set, every byte read from the shell
        # is appended to this file (full moshell-style terminal capture).
        self._step_log_fp = None
        self._step_log_path: Optional[str] = None
        # Optional live sink — when set, every decoded recv chunk is ALSO
        # handed to this callback (in addition to the file tee) so the
        # GUI can mirror the real moshell terminal stream per node. Kept
        # deliberately dumb (raw chunk in, no parsing) so the SSH read
        # hot-path stays cheap; the GUI side does any cleanup at render.
        self._live_sink: Optional[Callable[[str], None]] = None
        # Server-side log files produced during a step; downloaded to
        # LOG/{SHORTCODE}/MOSHELL/ after the step completes.
        # Each entry: (remote_path, subfolder_or_None)
        self._remote_logs: list[tuple[str, Optional[str]]] = []

    def register_remote_log(
        self, remote_path: str, subfolder: Optional[str] = None
    ) -> None:
        """Queue a server-side log for download after the current step.

        If `subfolder` is given, the file is placed in moshell_dir/subfolder/.
        """
        if remote_path:
            self._remote_logs.append((remote_path, subfolder))

    def drain_remote_logs(self, moshell_dir: str) -> list[str]:
        """Download all queued remote logs into `moshell_dir` (or a subfolder)
        and clear the queue.

        After a successful download we also DELETE the file from the
        server so ``/home/shared/<user>/`` doesn't grow indefinitely
        across runs. The deletion is best-effort — if it fails we just
        log a warning, the local copy is the one that matters.

        Returns list of successfully-downloaded local paths.
        """
        downloaded: list[str] = []
        if not self._remote_logs:
            return downloaded
        for remote, subfolder in list(self._remote_logs):
            target_dir = (
                os.path.join(moshell_dir, subfolder) if subfolder else moshell_dir
            )
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception:
                pass
            local = os.path.join(target_dir, os.path.basename(remote))
            try:
                self.sftp_download(remote, local)
                downloaded.append(local)
                # Successfully copied locally → delete from server.
                # ``sftp_remove`` is best-effort and never raises.
                self.sftp_remove(remote)
            except Exception as exc:
                self._log(f"Could not download {remote}: {exc}")
        self._remote_logs.clear()
        return downloaded

    # ── Live step logging ────────────────────────────────────────
    def start_step_log(self, path: str) -> None:
        """Open a file that every byte read from the shell is teed to.

        Any currently-open step log is closed first.
        """
        self.stop_step_log()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        try:
            # Block buffering (not line-buffered): the tee writes every
            # SSH byte here, so for multi-MB baseline/relation output a
            # line-buffered file (``buffering=1``) flushes thousands of
            # times — heavy disk I/O, especially with 3 nodes streaming
            # at once. 64 KB block buffering cuts that to a handful of
            # writes. The file is flushed + closed by stop_step_log
            # before anything reads it back (e.g. baseline fallback).
            self._step_log_fp = open(
                path, "w", encoding="utf-8", buffering=65536,
            )
            self._step_log_path = path
            header = (
                f"# Live moshell session log\n"
                f"# File: {os.path.basename(path)}\n"
                f"# Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# Host: {self.host}:{self.port} as {self.username}\n"
                + "=" * 72 + "\n\n"
            )
            self._step_log_fp.write(header)
        except Exception as exc:
            self._log(f"Could not open step log {path}: {exc}")
            self._step_log_fp = None
            self._step_log_path = None

    def stop_step_log(self) -> None:
        """Close the current step log file, if any."""
        if self._step_log_fp is not None:
            try:
                self._step_log_fp.write(
                    f"\n\n# Closed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                self._step_log_fp.close()
            except Exception:
                pass
        self._step_log_fp = None
        self._step_log_path = None

    def set_live_sink(self, sink: Optional[Callable[[str], None]]) -> None:
        """Register a callback that mirrors every recv chunk (for the GUI
        live terminal). Pass ``None`` to detach."""
        self._live_sink = sink

    def _tee(self, chunk: str) -> None:
        """Write a decoded recv chunk to the step log, if open, and
        mirror it to the live sink (GUI terminal), if set."""
        if not chunk:
            return
        if self._step_log_fp is not None:
            try:
                self._step_log_fp.write(chunk)
            except Exception:
                pass
        if self._live_sink is not None:
            try:
                self._live_sink(chunk)
            except Exception:
                pass

    # ── Connection ───────────────────────────────────────────────
    def connect(self, timeout: int = 30):
        self._log(f"Connecting to {self.host}:{self.port} as {self.username}...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        self.shell = self.client.invoke_shell(term="xterm", width=200, height=50)
        self.shell.settimeout(timeout)
        self._connected = True
        try:
            import ssh_registry
            ssh_registry.register(self)
        except Exception:
            pass
        # Consume initial banner / motd
        self._read_until_prompt(timeout=15)
        self._log("SSH connection established.")

    def disconnect(self):
        if self.shell:
            try:
                self.shell.close()
            except Exception:
                pass
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self._connected = False
        try:
            import ssh_registry
            ssh_registry.unregister(self)
        except Exception:
            pass
        self._log("SSH disconnected.")

    def sftp_upload(self, local_path: str, remote_dir: str) -> str:
        """Upload a local file to the server via SFTP.

        Creates the remote directory if it doesn't exist.
        Returns the full remote path of the uploaded file.
        """
        filename = os.path.basename(local_path)
        remote_path = f"{remote_dir}/{filename}"
        self._log(f"SFTP uploading {filename} → {remote_path}...")

        sftp = self.client.open_sftp()
        try:
            # Ensure remote directory exists
            try:
                sftp.stat(remote_dir)
            except FileNotFoundError:
                self._log(f"Creating remote directory {remote_dir}...")
                # mkdir -p equivalent: create each component
                parts = remote_dir.replace("\\", "/").split("/")
                current = ""
                for part in parts:
                    if not part:
                        current = "/"
                        continue
                    current = f"{current}/{part}" if current != "/" else f"/{part}"
                    if current == "/home" or current == "/":
                        continue
                    try:
                        sftp.stat(current)
                    except FileNotFoundError:
                        sftp.mkdir(current)

            sftp.put(local_path, remote_path)
            self._log(f"SFTP upload complete: {remote_path}")
        finally:
            sftp.close()

        return remote_path

    def sftp_download(self, remote_path: str, local_path: str) -> str:
        """Download a file from the server via SFTP.

        Creates the local directory if it doesn't exist.
        Returns the local path of the downloaded file.
        """
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.exists(local_dir):
            os.makedirs(local_dir, exist_ok=True)

        self._log(f"SFTP downloading {remote_path} → {local_path}...")
        sftp = self.client.open_sftp()
        try:
            sftp.get(remote_path, local_path)
            self._log(f"SFTP download complete: {local_path}")
        finally:
            sftp.close()

        return local_path

    def sftp_remove(self, remote_path: str) -> bool:
        """Delete ``remote_path`` from the server via SFTP. Best-effort:
        returns ``True`` on success, ``False`` (no exception raised) on
        any failure — clean-up never blocks a step. Used after a log
        has been safely downloaded locally to keep the operator's
        ``/home/shared/<user>/`` from growing without bound."""
        if not remote_path:
            return False
        try:
            sftp = self.client.open_sftp()
            try:
                sftp.remove(remote_path)
            finally:
                sftp.close()
            self._log(f"SFTP removed remote {remote_path}")
            return True
        except FileNotFoundError:
            # Already gone — count as success.
            return True
        except Exception as exc:
            self._log(f"SFTP remove failed for {remote_path}: {exc}")
            return False

    def reconnect(self, timeout: int = 30):
        """Disconnect and re-establish the SSH session."""
        self._log("Reconnecting SSH session...")
        self.disconnect()
        time.sleep(2)
        self.connect(timeout=timeout)

    def is_session_expired(self, output: str) -> bool:
        """Check if output contains the ENM SessionTimeoutException."""
        return "SessionTimeoutException" in output or "session timeout has expired" in output.lower()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ── Fast direct exec (no AMOS shell needed) ──────────────────
    def exec_ssh(self, command: str, timeout: int = 300) -> str:
        """Run a command via a fresh SSH channel (bypasses the interactive AMOS shell).

        Much faster than ``run_amos_command_safe`` because there's no prompt
        detection, no ANSI scrubbing, and no blocking on AMOS readiness.
        """
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            out += "\n[stderr]\n" + err
        return out

    # ── Core I/O ─────────────────────────────────────────────────
    def send(self, text: str):
        """Send text (with newline) to the shell."""
        self.shell.send(text + "\n")
        time.sleep(0.3)

    def run_amos_set_with_confirm(self, command: str, node_name: str,
                                  answer: str = "y", timeout: int = 60) -> str:
        """Run an AMOS ``set`` command that prompts ``Are you Sure [y/n] ?``.

        Sends the command, waits for the y/n prompt (up to 15s), sends the
        answer, then reads until the AMOS prompt. Works whether or not a
        confirmation prompt actually appears.
        """
        import re as _re
        self.send(command)
        buf = ""
        start = time.time()
        hit_confirm = False
        prompt_re = _re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")
        # Phase 1: wait up to 15s for a confirm prompt OR the real amos prompt
        while time.time() - start < 15:
            if self.shell.recv_ready():
                buf += (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                clean = strip_ansi(buf).lower()
                if "[y/n]" in clean or "are you sure" in clean:
                    hit_confirm = True
                    break
                last = strip_ansi(buf).strip().split("\n")[-1].strip()
                if (prompt_re.match(last)
                        and "<" not in last and "/" not in last):
                    return strip_ansi(buf)
            else:
                time.sleep(0.2)
        if hit_confirm:
            self.send(answer)
        # Phase 2: read until amos prompt
        tail = self._read_until_amos(timeout=timeout)
        return strip_ansi(buf + tail)

    def _read_until_prompt(self, timeout: int = 60) -> str:
        """Read output until a shell prompt is detected."""
        # O(n²) avoidance: rolling tail for last-line prompt detection
        # (see run_amos_blocking_with_sentinel for the rationale).
        SCAN_MAX = 4096
        buf_parts: list[str] = []
        scan = ""
        start = time.time()
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for shell prompt")
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for shell prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf_parts.append(chunk)
                scan = (scan + strip_ansi(chunk))[-SCAN_MAX:]
                last = scan.strip().split("\n")[-1].strip() if scan.strip() else ""
                if _is_shell_prompt(last):
                    # Drain any trailing bytes
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        extra = self.shell.recv(65536).decode("utf-8", errors="replace")
                        self._tee(extra)
                        buf_parts.append(extra)
                    break
            else:
                time.sleep(0.3)
        return "".join(buf_parts)

    def _channel_dead(self) -> bool:
        """True if the shell channel has been closed/EOF'd — used so wait
        loops bail out immediately when the session is force-disconnected
        (cancel / back) instead of idling until their timeout expires."""
        sh = self.shell
        if sh is None:
            return True
        try:
            if getattr(sh, "closed", False):
                return True
            if sh.eof_received:
                return True
            if not sh.get_transport() or not sh.get_transport().is_active():
                return True
        except Exception:
            return True
        return False

    def _read_until(self, marker: str, timeout: int = 60) -> str:
        """Read output until a specific marker string appears in the output."""
        buf = ""
        start = time.time()
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for '%s'", marker)
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for '%s'", timeout, marker)
                break
            if self.shell.recv_ready():
                chunk = (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                buf += chunk
                if marker.lower() in strip_ansi(buf).lower():
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        buf += (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                    break
            else:
                time.sleep(0.3)
        return buf

    def run_amos_command_autoyes(self, command: str, timeout: int = 180) -> str:
        """Run an AMOS command and auto-answer any ``[y/n]`` confirmation
        prompts with ``y``. Used for commands like ``mcc ... ping`` that
        emit a "Run COMCLI command(s) on N MOs. Are you Sure [y/n] ?" prompt.
        """
        import re
        self.send(command)
        prompt_re = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")
        # Rolling-tail detection (O(1)/chunk). The ``[y/n]`` prompt
        # appears right after the command (before any large output), so
        # an 8 KB tail always catches it; the final AMOS prompt is one
        # short line at the end.
        SCAN_MAX = 8192
        buf_parts: list[str] = []
        scan = ""
        start = time.time()
        answered = False
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for AMOS prompt")
                break
            if time.time() - start > timeout:
                logger.warning(
                    "Timeout (%ds) waiting for AMOS prompt (autoyes)", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf_parts.append(chunk)
                scan = (scan + strip_ansi(chunk))[-SCAN_MAX:]
                # Auto-answer y/n confirmation
                if not answered and "[y/n]" in scan.lower():
                    self._log("  auto-answering [y/n] prompt with 'y'")
                    self.send("y")
                    answered = True
                    continue
                last = scan.strip().split("\n")[-1].strip() if scan.strip() else ""
                if (prompt_re.match(last)
                        and "<" not in last
                        and "/" not in last):
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        extra = self.shell.recv(65536).decode("utf-8", errors="replace")
                        self._tee(extra)
                        buf_parts.append(extra)
                    break
            else:
                time.sleep(0.3)
        return strip_ansi("".join(buf_parts))

    def drain_after_command(self, wait: float = 5.0, read_timeout: int = 300) -> str:
        """After a long-running command, drain any remaining output.

        Call this after ``run_amos_command_safe()`` for commands that take a
        long time (e.g. baseline, relation).  ``_read_until_amos`` may detect
        a false prompt early; this method waits a few seconds, drains stale
        data, and reads until the real AMOS prompt confirms the command is
        truly finished.

        Returns any additional output received.
        """
        import time as _t
        self._log("Draining remaining output after long-running command...")
        extra = ""

        _t.sleep(wait)

        stale = False
        while self.shell and self.shell.recv_ready():
            try:
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                extra += chunk
                stale = True
            except Exception:
                break

        if stale:
            self._log("Additional output found — reading until AMOS prompt...")
            remaining = self._read_until_amos(timeout=read_timeout)
            extra += remaining
            self._log(f"Drained {len(extra)} additional bytes.")
        else:
            self._log("No remaining output — command appears complete.")

        return extra

    def run_amos_blocking_with_sentinel(
        self,
        command: str,
        node_name: str,
        timeout: int = 3600,
        quiet_after: float = 10.0,
        idle_timeout: float = 300.0,
        on_activity: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Run a long-running AMOS command and wait for a sentinel echo
        AND a quiescence window to prove it *really* finished.

        This models the operator's mental check for baseline:
          * run <baseline.mos>
          * baseline fires many set/crn commands
          * each one fills in the next time the AMOS prompt is ready
          * we decide baseline is done when the prompt is idle for N seconds
          * then cvls verifies a ``Post_...`` entry appears

        Implementation uses a sentinel token AND a quiet window (both must
        hold). Either check alone is insufficient:
          * sentinel alone → moshell may still print trailing noise after
            the nonce (ANSI resets, final prompt redraw)
          * quiescence alone → a natural mid-script pause can fire too
            early (baseline has seconds-long gaps while each set commits)

        Flow:
            1. Build a unique nonce → ``__TRFS_DONE_<8hex>__``
            2. Send ``<command>`` (e.g. ``run <baseline.mos>``)
            3. Send ``!echo <nonce>`` on a fresh line. Moshell *queues* it
               behind the current ``run``, so it only executes once the
               script exits and the AMOS prompt is free — that's the
               "prompt idle, no more commands to feed" signal.
            4. After seeing the nonce, wait for an AMOS prompt *and* for
               ``quiet_after`` seconds of no new bytes on the channel.

        Args:
            command:      The AMOS command to run (most often ``run <path>``).
            node_name:    For logging / reconnect (unused in fast path).
            timeout:      Hard upper bound in seconds.
            quiet_after:  How long the channel must be silent after the
                          AMOS prompt appears before we declare "done".
                          10 s matches the operator's rule-of-thumb for
                          baseline; pass a smaller value for fast scripts.

        Returns: the full accumulated output (ANSI-stripped).
        """
        nonce = uuid.uuid4().hex[:8]
        sentinel = f"__TRFS_DONE_{nonce}__"
        self._log(
            f"[sentinel] '{command}' → waiting for {sentinel} "
            f"(quiet={quiet_after:.0f}s, idle-timeout={idle_timeout:.0f}s, "
            f"hard-timeout={timeout}s)"
        )
        self.send(command)
        # Small gap so moshell reads the command line before the echo.
        time.sleep(0.4)
        self.send(f"!echo {sentinel}")

        # PERFORMANCE: relation / baseline output can be many MB. We must
        # NOT re-strip + re-split the whole accumulated buffer on every
        # 64 KB recv — that's O(n²) and pegs a CPU core, starving the UI
        # thread. We keep a small rolling ``scan`` window for detection.
        SCAN_MAX = 8192
        buf_parts: list[str] = []
        scan = ""

        start = time.time()
        saw_sentinel = False
        sentinel_time = 0.0
        last_byte_time = start
        last_progress = start
        done_reason = "hard-timeout"
        while True:
            if self._channel_dead():
                self._log("[sentinel] channel dead — aborting wait.")
                done_reason = "channel-dead"
                break

            now = time.time()
            # Hard upper bound.
            if now - start > timeout:
                self._log(
                    f"[sentinel] HARD TIMEOUT after {timeout}s — "
                    f"sentinel {'seen' if saw_sentinel else 'NOT seen'}. "
                    "Returning what we have."
                )
                done_reason = "hard-timeout"
                break

            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf_parts.append(chunk)
                now = time.time()
                last_byte_time = now
                last_progress = now
                # Only strip ANSI when something actually needs it: the
                # activity callback, or the still-running sentinel scan.
                # After the sentinel is found (and with no on_activity),
                # we skip the per-chunk strip entirely — the trailing
                # settling bytes don't need scanning. Saves a full
                # strip_ansi() per 64 KB chunk during the quiet window.
                if on_activity or not saw_sentinel:
                    stripped_chunk = strip_ansi(chunk)
                    # Live activity callback (e.g. relation step detecting
                    # "run <script>" lines for per-script UI progress).
                    if on_activity:
                        try:
                            on_activity(stripped_chunk)
                        except Exception:
                            pass
                    if not saw_sentinel:
                        # Roll the small detection window (O(1) per chunk).
                        scan = (scan + stripped_chunk)[-SCAN_MAX:]
                        # Only the bare ``__TRFS_DONE_xxx__`` on its own
                        # line counts (not the PTY echo of "!echo …").
                        for line in scan.splitlines():
                            if line.strip() == sentinel:
                                saw_sentinel = True
                                sentinel_time = now
                                self._log(
                                    f"[sentinel] nonce seen after "
                                    f"{int(now - start)}s — script finished; "
                                    f"settling {quiet_after:.0f}s for trailing "
                                    "output."
                                )
                                break
                # Yield the GIL so the asyncio UI event loop gets to run
                # between chunks. Without this, 3 nodes streaming MB-large
                # relation/baseline output keep the GIL busy back-to-back
                # and the Flet window stops receiving render frames (goes
                # black). sleep(0) is a near-zero-cost scheduler yield.
                time.sleep(0)
            else:
                now = time.time()
                # ── Completion gates (checked while idle) ──────────
                # 1) Sentinel seen → the run genuinely returned to the
                #    AMOS prompt and executed !echo. Once it's been
                #    quiet for ``quiet_after`` seconds, return. We do
                #    NOT additionally require a prompt-regex match — that
                #    extra gate was fragile and caused hangs when the
                #    final prompt line didn't match exactly.
                if saw_sentinel and (now - last_byte_time) >= quiet_after:
                    self._log(
                        f"[sentinel] complete — sentinel + "
                        f"{quiet_after:.0f}s quiet."
                    )
                    done_reason = "sentinel"
                    break
                # 2) Sentinel NOT seen but the channel has been totally
                #    silent for ``idle_timeout`` — the command is stuck
                #    (moshell waiting on input, or hung). Give up rather
                #    than block until the hours-long hard timeout. This
                #    is the key fix for "one node stuck forever on
                #    relation while others progress".
                if (not saw_sentinel
                        and (now - last_byte_time) >= idle_timeout):
                    self._log(
                        f"[sentinel] NO OUTPUT for {idle_timeout:.0f}s and "
                        "no sentinel — assuming the command is stuck or "
                        "finished without echoing. Proceeding with "
                        "whatever output was captured."
                    )
                    done_reason = "idle-timeout"
                    break
                # Heartbeat so the SESSION log shows it's alive + where.
                if now - last_progress > 60:
                    idle_for = int(now - last_byte_time)
                    if saw_sentinel:
                        phase = (
                            f"sentinel seen, settling "
                            f"(quiet {idle_for}/{int(quiet_after)}s)"
                        )
                    else:
                        phase = (
                            f"running, last output {idle_for}s ago "
                            f"(stuck if reaches {int(idle_timeout)}s)"
                        )
                    self._log(
                        f"[sentinel] still alive — {phase}, "
                        f"elapsed={int(now - start)}s"
                    )
                    last_progress = now
                time.sleep(0.5)

        self._log(f"[sentinel] returning (reason={done_reason}).")
        return strip_ansi("".join(buf_parts))

    def _read_until_amos(self, timeout: int = 120) -> str:
        """Read output until an AMOS/moshell prompt (``NODENAME>``) appears.

        Must NOT match XML closing tags like ``</hello>`` or ``</rpc-reply>``
        when a command like ``netconf`` emits XML — only the actual shell
        prompt (alphanumeric+underscore only, ending with ``>``).
        """
        import re
        prompt_re = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")
        # Same O(n²) avoidance as run_amos_blocking_with_sentinel: keep a
        # small rolling tail for the last-line prompt check instead of
        # re-stripping the whole buffer each chunk. Only the LAST line
        # matters for prompt detection, so a few KB tail is plenty.
        SCAN_MAX = 4096
        buf_parts: list[str] = []
        scan = ""
        start = time.time()
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for AMOS prompt")
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for AMOS prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf_parts.append(chunk)
                scan = (scan + strip_ansi(chunk))[-SCAN_MAX:]
                last = scan.strip().split("\n")[-1].strip() if scan.strip() else ""
                # Real AMOS prompt: word-only token + '>', no XML chars
                if (prompt_re.match(last)
                        and "<" not in last
                        and "/" not in last):
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        extra = self.shell.recv(65536).decode("utf-8", errors="replace")
                        self._tee(extra)
                        buf_parts.append(extra)
                    break
            else:
                time.sleep(0.3)
        return "".join(buf_parts)

    def run_command(self, command: str, timeout: int = 60) -> str:
        """Run a command and wait for the shell prompt to return."""
        self.send(command)
        output = self._read_until_prompt(timeout=timeout)
        return strip_ansi(output)

    def run_amos_command(self, command: str, timeout: int = 120) -> str:
        """Run a command inside an AMOS session and wait for the AMOS prompt."""
        self.send(command)
        output = self._read_until_amos(timeout=timeout)
        return strip_ansi(output)

    def enter_amos(self, node_name: str, timeout: int = 90) -> str:
        """Start AMOS session: ``amos <nodename>`` then ``lt all``."""
        self._log(f"Entering AMOS for {node_name}...")
        self.send(f"amos {node_name}")
        output = self._read_until_amos(timeout=timeout)
        self._log("AMOS prompt ready, loading MO tree (lt all)...")
        self.send("lt all")
        output += self._read_until_amos_or_prompt(timeout=120)
        self._log("AMOS ready.")
        return strip_ansi(output)

    def _read_until_amos_or_prompt(self, timeout: int = 120) -> str:
        """Read until AMOS prompt, handling username/password prompts with 'rbs'/'rbs'.

        ``lt all`` loads tens of thousands of MOs — huge output — so we
        use the rolling-tail technique (O(1) per chunk) instead of
        re-stripping the whole buffer each time.
        """
        import re as _re
        SCAN_MAX = 4096
        buf_parts: list[str] = []
        scan = ""
        start = time.time()
        sent_user = False
        sent_pass = False
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for AMOS prompt")
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for AMOS prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf_parts.append(chunk)
                scan = (scan + strip_ansi(chunk))[-SCAN_MAX:]
                tail = scan[-500:].lower()
                if not sent_user and "enter username" in tail:
                    self._log("lt all: sending username 'rbs'")
                    self.send("rbs")
                    sent_user = True
                    time.sleep(0.3)
                    continue
                if sent_user and not sent_pass and "password" in tail:
                    self._log("lt all: sending password")
                    self.send("rbs")
                    sent_pass = True
                    time.sleep(0.3)
                    continue
                last = scan.strip().split("\n")[-1].strip() if scan.strip() else ""
                if (_re.match(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$", last)
                        and "<" not in last and "/" not in last):
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        extra = self.shell.recv(65536).decode("utf-8", errors="replace")
                        self._tee(extra)
                        buf_parts.append(extra)
                    break
            else:
                time.sleep(0.3)
        return "".join(buf_parts)

    def exit_amos(self) -> str:
        """Exit AMOS session back to bash."""
        self.send("exit")
        time.sleep(2)
        output = self._read_until_prompt(timeout=15)
        return strip_ansi(output)

    def run_amos_command_safe(
        self,
        command: str,
        node_name: str,
        timeout: int = 120,
        in_amos: bool = True,
    ) -> str:
        """Run an AMOS command; if session expired, reconnect + re-enter AMOS and retry.

        Args:
            command:   The command to run (e.g. ``!python ... cli.py ...``)
            node_name: Node name (needed to re-enter AMOS after reconnect)
            timeout:   Command timeout
            in_amos:   If True, we're inside AMOS and should re-enter after reconnect.

        Resilience: if the SSH socket has DIED (e.g. the gateway dropped
        the session right after a long baseline run), we reconnect and
        re-enter AMOS BEFORE running the command, then run it — so the
        pending verification (cvls, st mme, etc.) resumes on a fresh
        session instead of failing on a dead socket.
        """
        # Pre-flight: dead socket → reconnect + re-enter AMOS first.
        if self._channel_dead():
            self._log(
                "⚠ SSH socket is closed before command — reconnecting "
                "and re-entering AMOS to resume..."
            )
            try:
                self.reconnect()
                if in_amos:
                    self.enter_amos(node_name, timeout=90)
            except Exception as exc:
                self._log(f"✗ Reconnect failed: {exc}")
                return ""

        output = self.run_amos_command(command, timeout=timeout)

        # Post-flight: session-expired string OR the channel died mid /
        # right-after the command → reconnect, re-enter AMOS, retry once.
        if not self.is_session_expired(output) and not self._channel_dead():
            return output

        reason = (
            "session expired"
            if self.is_session_expired(output)
            else "SSH socket closed"
        )
        self._log(
            f"⚠ {reason} detected — reconnecting SSH and re-entering "
            "AMOS, then retrying the command..."
        )
        try:
            self.reconnect()
            if in_amos:
                self.enter_amos(node_name, timeout=90)
            output2 = self.run_amos_command(command, timeout=timeout)
            return output + "\n[SESSION RECONNECTED]\n" + output2
        except Exception as exc:
            self._log(f"✗ Reconnect/retry failed: {exc}")
            return output

    def run_command_safe(
        self,
        command: str,
        node_name: str = "",
        timeout: int = 60,
    ) -> str:
        """Run a bash command; if session expired, reconnect and retry."""
        output = self.run_command(command, timeout=timeout)
        if not self.is_session_expired(output):
            return output

        self._log("⚠ Session expired detected — reconnecting SSH...")
        self.reconnect()
        output2 = self.run_command(command, timeout=timeout)
        return output + "\n[SESSION RECONNECTED]\n" + output2

    def run_interactive(
        self,
        command: str,
        prompts: list[tuple[str, str]],
        final_timeout: int = 120,
    ) -> str:
        """Run a command that asks interactive prompts.

        Args:
            command: The command to execute.
            prompts: List of (marker_to_wait_for, value_to_send) pairs.
                     Each marker is a substring expected in the prompt text.
            final_timeout: Timeout for the final shell prompt after all
                          prompts are answered.

        Returns:
            Full cleaned output.
        """
        all_output = ""
        self.send(command)

        for marker, value in prompts:
            out = self._read_until(marker, timeout=60)
            all_output += out
            self._log(f"  Prompt '{marker}' → answering '{value}'")
            self.send(value)

        # Wait for the command to complete (shell prompt returns)
        out = self._read_until_prompt(timeout=final_timeout)
        all_output += out
        return strip_ansi(all_output)


# ── ARNE step ────────────────────────────────────────────────────
def verify_arne(ssh: IntegrationSSH, node_name: str, log_cb: Callable[[str], None]) -> tuple[bool, str]:
    """Run cli.py to verify the node exists. Returns (success, output)."""
    log_cb(f"Verifying ARNE for {node_name}...")
    verify_cmd = (
        f'python {CLI_PY} '
        f'"cmedit get {node_name}"'
    )
    verify_output = ssh.run_command(verify_cmd, timeout=60)
    log_cb(f"Verify output:\n{verify_output}")
    success = "1 instance(s)" in verify_output
    return success, verify_output


def run_create_arne(
    ssh: IntegrationSSH,
    node_name: str,
    node_ip: str,
    subnetwork: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    *,
    bsc_name: Optional[str] = None,
    log_dir: Optional[str] = None,
) -> tuple[bool, str]:
    """Run create_arne.py, fill the 3 prompts, then verify with cli.py.

    If verification fails (0 instances), calls ``wait_for_user(message)``
    which should block until the user clicks Retry (returns True) or
    Cancel (returns False).  Re-verifies in a loop until success or
    cancellation.

    For GSM nodes, if ``bsc_name`` is provided, additionally sets the
    ``controllingBsc`` attribute on the NetworkElement to point at the
    BSC after the ARNE entry has been verified::

        cmedit set NetworkElement=<node> controllingBsc="NetworkElement=<bsc>"

    Uses the same ``python cli.py "<cmedit ...>"`` invocation pattern as
    ``verify_arne``, including a ``cmedit get`` read-back to confirm the
    attribute landed.

    Returns:
        (success: bool, full_output: str)
    """
    log_cb(f"Running create_arne.py for {node_name}...")
    log_cb(f"  Node: {node_name}  IP: {node_ip}  Subnetwork: {subnetwork}")

    # Step 0: Ping the node IP to verify reachability
    log_cb(f"Pinging {node_ip}...")
    ping_out = ssh.run_command(f"ping -c 3 -W 5 {node_ip}", timeout=30)
    log_cb(f"Ping output:\n{ping_out}")

    if "0 received" in ping_out or "100% packet loss" in ping_out \
            or "unreachable" in ping_out.lower():
        msg = (
            f"Node IP {node_ip} is not reachable (ping failed).\n\n"
            f"Please check the IP address and network connectivity."
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(msg)
            if not retry:
                return False, ping_out
            # Retry ping
            ping_out2 = ssh.run_command(f"ping -c 3 -W 5 {node_ip}", timeout=30)
            log_cb(f"Retry ping:\n{ping_out2}")
            if "0 received" in ping_out2 or "100% packet loss" in ping_out2 \
                    or "unreachable" in ping_out2.lower():
                log_cb("✗ Ping still failed after retry.")
                return False, ping_out + "\n" + ping_out2
        else:
            return False, ping_out

    log_cb(f"✓ Node IP {node_ip} is reachable.")

    # Step 1: Run the ARNE creation script (interactive prompts)
    output = ssh.run_interactive(
        command=f"python {_CREATE_ARNE}",
        prompts=[
            ("nodename",   node_name),
            ("ip address", node_ip),
            ("subnetwork", subnetwork),
        ],
        final_timeout=120,
    )
    log_cb(f"create_arne.py output:\n{output}")

    # Step 2: Verify
    all_output = output
    success, verify_output = verify_arne(ssh, node_name, log_cb)
    all_output += "\n" + verify_output

    if success:
        log_cb(f"✓ ARNE verified — {node_name} found (1 instance)")
        return True, all_output

    # Verification failed — loop: ask user to fix, then re-verify
    while not success:
        log_cb(f"✗ ARNE verification failed — {node_name}: 0 instance(s) found")
        log_cb("Waiting for user to check and fix the issue...")

        if wait_for_user is None:
            # No callback — just fail
            return False, all_output

        retry = wait_for_user(
            f"ARNE creation for '{node_name}' failed — 0 instance(s) found.\n\n"
            f"Please check the error and fix the issue manually.\n"
            f"Click 'Retry' to re-verify, or 'Stop' to abort remaining steps."
        )
        if not retry:
            log_cb("User chose to stop. Aborting remaining steps.")
            return False, all_output

        log_cb("User clicked Retry — re-verifying...")
        success, verify_output = verify_arne(ssh, node_name, log_cb)
        all_output += "\n" + verify_output

        if success:
            log_cb(f"✓ ARNE verified — {node_name} found (1 instance)")

    # ── GSM: set controllingBsc → NetworkElement=<BSC> ──────────
    # Triggered only when the caller passed a non-empty bsc_name
    # (separate GSM node, or co-located GSM on the primary LTE node).
    # We ALWAYS log the decision here — silent skips were impossible
    # to trace from the field.
    bsc = (bsc_name or "").strip()
    if not bsc:
        log_cb(
            f"[controllingBsc] SKIPPED for {node_name} — no BSC name "
            f"was passed to run_create_arne (bsc_name={bsc_name!r}). "
            "This is expected for pure LTE/NR nodes. If this IS a GSM "
            "or co-located node, check that the BSC Name field is "
            "filled in the form."
        )
    else:
        log_cb(
            f"[controllingBsc] BSC name = '{bsc}' → proceeding to set "
            f"controllingBsc on {node_name}."
        )
        set_ok, set_out = _set_controlling_bsc(
            ssh, node_name, bsc, log_cb,
            wait_for_user=wait_for_user,
            log_dir=log_dir,
        )
        all_output += "\n" + set_out
        if not set_ok:
            # The controllingBsc set failed — but ARNE itself is
            # already verified, so we report this as a partial
            # failure. Caller decides whether to continue.
            log_cb(
                f"✗ controllingBsc could not be set on {node_name} "
                f"→ NetworkElement={bsc}. ARNE entry exists but the "
                "BSC link is missing."
            )
            return False, all_output

    return True, all_output


def _set_controlling_bsc(
    ssh: IntegrationSSH,
    node_name: str,
    bsc_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    log_dir: Optional[str] = None,
    in_amos: bool = False,
) -> tuple[bool, str]:
    """Set ``controllingBsc`` on a GSM NetworkElement and verify.

    Sends the cmedit set/get through cli.py. The invocation style
    depends on the SSH context:

      * ``in_amos=False`` (bash shell — e.g. inside ``run_create_arne``
        before AMOS is entered): ``python cli.py "<cmd>"`` via
        ``ssh.run_command``.
      * ``in_amos=True`` (inside an AMOS session — e.g. the GSM cell
        define step): ``!python cli.py "<cmd>"`` via
        ``ssh.run_amos_command_safe`` (the ``!`` shells out from AMOS).

    Commands::

        cmedit set NetworkElement=<node> controllingBsc="NetworkElement=<bsc>"
        cmedit get <node> NetworkElement.controllingBsc

    On mismatch, prompts the operator to retry (re-runs SET + GET).

    If ``log_dir`` is given, a dedicated trace file is written to
    ``<log_dir>/MOSHELL/CONTROLLING_BSC_<node>.log`` containing the
    exact SET / GET commands and their raw output — so a failed BSC
    link can be diagnosed from the field without re-running.
    """
    import os as _os
    import time as _time

    all_output = ""
    expected_value = f"NetworkElement={bsc_name}"

    # cli.py invocation prefix + runner differ by context.
    _prefix = "!python" if in_amos else "python"

    def _exec(cmd: str) -> str:
        if in_amos:
            return ssh.run_amos_command_safe(cmd, node_name, timeout=60)
        return ssh.run_command(cmd, timeout=60)

    # Collect a structured trace for the dedicated log file.
    _trace: list[str] = [
        "=" * 72,
        "controllingBsc SET + VERIFY trace",
        f"Node: {node_name}",
        f"Target BSC: {bsc_name}  (expected value: {expected_value})",
        f"Started: {_time.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 72,
        "",
    ]

    def _trace_log(msg: str) -> None:
        """Forward to the normal UI log AND accumulate for the file."""
        log_cb(msg)
        _trace.append(msg)

    def _flush_trace(final_status: str) -> None:
        _trace.append("")
        _trace.append("-" * 72)
        _trace.append(f"Result: {final_status}")
        _trace.append(f"Finished: {_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if not log_dir:
            return
        try:
            moshell_dir = _os.path.join(log_dir, "MOSHELL")
            _os.makedirs(moshell_dir, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", node_name)
            path = _os.path.join(
                moshell_dir, f"CONTROLLING_BSC_{safe}.log"
            )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(_trace) + "\n")
            log_cb(f"[controllingBsc] trace saved → {path}")
        except Exception as exc:
            log_cb(f"[controllingBsc] could not save trace: {exc}")

    # ── SET ──────────────────────────────────────────────────
    # cmedit set syntax accepted by ENM:
    #   cmedit set NetworkElement=<node> controllingBsc="NetworkElement=<bsc>"
    # The value MUST be wrapped in double quotes because it's an FDN
    # reference (contains ``=``) — without quotes cmedit would parse
    # ``controllingBsc=NetworkElement=BSC`` as two separate key=value
    # pairs and silently set nothing.
    #
    # Shell-quoting: we wrap the WHOLE cmedit string in OUTER double
    # quotes and ESCAPE the inner doubles, matching the established
    # pattern used by ``verify_arne`` / ``cmedit action`` elsewhere.
    # The previous single-quote-outer approach let the literal quotes
    # bleed into cli.py's argument, which depending on cli.py's
    # parsing could be why the set never took effect.
    set_cmd = (
        f'{_prefix} {CLI_PY} '
        f'"cmedit set NetworkElement={node_name} '
        f'controllingBsc=\\"NetworkElement={bsc_name}\\""'
    )

    def _run_set() -> str:
        _trace_log(f"Setting controllingBsc on {node_name} → {expected_value}")
        _trace_log(f"  $ {set_cmd}")
        out = _exec(set_cmd)
        _trace_log(f"cmedit set output:\n{out}")
        return out

    set_out = _run_set()
    all_output += set_out

    # Give ENM ~3 s to commit the attribute before reading it back —
    # cmedit set is fire-and-forget; the change isn't always visible
    # to an immediate get.
    _trace_log("Waiting 3 s for ENM to propagate the controllingBsc change…")
    time.sleep(3)

    # ── VERIFY ───────────────────────────────────────────────
    # Correct cmedit syntax for reading a single MO-class attribute
    # off a NetworkElement is to qualify with ``<MoClass>.<attr>``:
    #     cmedit get <node> NetworkElement.controllingBsc
    # Plain ``controllingBsc`` without the ``NetworkElement.`` prefix
    # makes cmedit search every MO under the node — which is why the
    # previous verify always saw ``null`` even when the set worked.
    get_cmd = (
        f'{_prefix} {CLI_PY} '
        f'"cmedit get {node_name} NetworkElement.controllingBsc"'
    )

    # Match the controllingBsc line specifically — anchored to its
    # own line so the FDN line (``NetworkElement=<NODE>``) above it
    # can't false-positive the substring search. ``null`` is the
    # explicit "not set" sentinel.
    _SUCCESS_RE = re.compile(
        r"^\s*controllingBsc\s*:\s*NetworkElement\s*=\s*"
        + re.escape(bsc_name) + r"\s*$",
        re.MULTILINE,
    )
    _NULL_RE = re.compile(
        r"^\s*controllingBsc\s*:\s*null\s*$", re.MULTILINE,
    )

    def _verify_once() -> tuple[bool, str, bool]:
        """Returns (ok, output, is_explicit_null)."""
        _trace_log(f"  $ {get_cmd}")
        out = _exec(get_cmd)
        _trace_log(f"cmedit get output:\n{out}")
        ok = bool(_SUCCESS_RE.search(out))
        is_null = bool(_NULL_RE.search(out)) and not ok
        return ok, out, is_null

    ok, get_out, is_null = _verify_once()
    all_output += "\n" + get_out
    if ok:
        _trace_log(
            f"✓ controllingBsc verified on {node_name} → {expected_value}"
        )
        _flush_trace("SUCCESS")
        return True, all_output

    # ── Retry loop ───────────────────────────────────────────
    # When verify shows ``null`` (set didn't take effect), re-RUN the
    # SET command then re-verify — operator's manual retry usually
    # works the second time. When verify shows some OTHER value (rare:
    # set went through but to a different BSC), just re-verify.
    while not ok:
        if is_null:
            _trace_log(
                f"✗ controllingBsc still null on {node_name} — set "
                "did not take effect."
            )
        else:
            _trace_log(
                f"✗ controllingBsc on {node_name} is not "
                f"{expected_value} (see output above)."
            )
        if wait_for_user is None:
            _flush_trace("FAILED (no user prompt available)")
            return False, all_output
        retry = wait_for_user(
            f"controllingBsc on '{node_name}' did not verify as "
            f"'{expected_value}'.\n\n"
            "Click Retry — we'll re-run the SET and re-verify. "
            "Click Stop to skip the BSC link step (ARNE itself is "
            "already verified)."
        )
        if not retry:
            _trace_log("User chose to stop the controllingBsc verification.")
            _flush_trace("STOPPED by user")
            return False, all_output
        _trace_log("User clicked Retry — re-running set + verify…")
        # Re-do the SET on retry (not just re-read), since the
        # previous SET clearly didn't stick.
        set_out2 = _run_set()
        all_output += "\n" + set_out2
        time.sleep(3)
        ok, get_out, is_null = _verify_once()
        all_output += "\n" + get_out

    _trace_log(
        f"✓ controllingBsc verified on {node_name} → {expected_value}"
    )
    _flush_trace("SUCCESS (after retry)")
    return True, all_output



# ── Enrollment step ──────────────────────────────────────────────
def run_enrollment(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Run the full enrollment sequence.

    Stays inside AMOS for the entire flow (cli.py calls use ``!python``).
    If a SessionTimeoutException is detected, automatically reconnects
    SSH and re-enters AMOS before retrying.

    Sub-steps:
      1. Enter AMOS (amos <nodename> + lt all)
      2. Ensure ~/INOC/SCRIPTS/NS/ exists
      3. Create entity XML via entity_maker.sh
      4. Upload entity via exe_entity.py
      5. Run enrollment MOS script
      6. Validate: get NodeCredential enrollmentProgress → SUCCESS
      7. Force sync via cli.py  (stays in AMOS with ``!python``)
      8. Wait for syncStatus → SYNCHRONIZED  (stays in AMOS with ``!python``)

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # ── 1. AMOS assumed to be already entered by caller ────────

    # ── 2. Ensure ~/INOC/SCRIPTS/NS/ folder exists ──────────────
    log_cb("Ensuring ~/INOC/SCRIPTS/NS/ directory exists...")
    out = ssh.run_amos_command("!mkdir -p ~/INOC/SCRIPTS/NS/", timeout=15)
    all_output += out
    log_cb("Directory ensured.")

    # ── 3. Create entity XML ─────────────────────────────────────
    log_cb(f"Creating entity XML for {node_name}...")
    out = ssh.run_amos_command_safe(
        f"!bash {_ENTITY_MAKER} {node_name}",
        node_name, timeout=60,
    )
    all_output += out
    log_cb(f"entity_maker.sh output:\n{out}")

    # Verify XML was created
    log_cb(f"Checking {node_name}.xml exists...")
    check_out = ssh.run_amos_command(
        f"!ls ~/INOC/SCRIPTS/NS/{node_name}.xml",
        timeout=15,
    )
    all_output += check_out
    if f"{node_name}.xml" not in check_out or "No such file" in check_out:
        msg = f"Entity XML file ~/INOC/SCRIPTS/NS/{node_name}.xml was NOT created."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nPlease check the entity_maker.sh output above for errors.\n"
                f"Fix the issue, then click Retry to continue."
            )
            if not retry:
                log_cb("User chose to stop.")
                return False, all_output
            check_out2 = ssh.run_amos_command(
                f"!ls ~/INOC/SCRIPTS/NS/{node_name}.xml", timeout=15,
            )
            all_output += check_out2
            if f"{node_name}.xml" not in check_out2 or "No such file" in check_out2:
                log_cb("✗ XML still not found after retry.")
                return False, all_output
        else:
            return False, all_output
    log_cb(f"✓ {node_name}.xml confirmed.")

    # ── 4. Upload entity file ────────────────────────────────────
    log_cb(f"Uploading entity file for {node_name}...")
    out = ssh.run_amos_command_safe(
        f"!python {_EXE_ENTITY} "
        f"~/INOC/SCRIPTS/NS/{node_name}.xml",
        node_name, timeout=300,
    )
    all_output += out
    log_cb(f"exe_entity.py output:\n{out}")

    entity_ok = (
        "Creation of entity successful" in out
        or "Entity already exists" in out
        or "11203" in out
    )
    if not entity_ok:
        msg = f"Entity upload for {node_name} did not return expected success message."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nExpected 'Creation of entity successful' or "
                f"'Entity already exists'.\n"
                f"Check the output, fix the issue, then click Retry."
            )
            if not retry:
                log_cb("User chose to stop.")
                return False, all_output
        else:
            return False, all_output
    log_cb("✓ Entity upload confirmed.")

    # ── 5. Run enrollment MOS script ─────────────────────────────
    # Use the sentinel-based runner (same as baseline/relation): the
    # enrollment .mos contains many ``set NodeCredential``,
    # ``set TrustedCertificate``, etc. — each prints a transient
    # ``NODE>`` prompt that ``_read_until_amos`` would match wrongly,
    # returning before the script is actually done. That premature
    # return is what makes the next step (``get NodeCredential
    # enrollmentProgress``) see IDLE/ONGOING and fire a false error
    # popup; the operator's Retry then succeeds because the enrollment
    # really did finish in the background by then.
    #
    # Sentinel + 10s idle quiescence guarantees we only proceed to the
    # progress check after the enrollment script has truly returned to
    # the AMOS prompt and stayed quiet — so the very first
    # ``enrollmentProgress`` check has a real chance of seeing SUCCESS.
    log_cb("Running enrollment script (lhgenm1.mos)...")
    log_cb("(sentinel + 10s idle quiescence; can take several minutes)")
    out = ssh.run_amos_blocking_with_sentinel(
        f"run {_ENROLLMENT_MOS}",
        node_name, timeout=600, quiet_after=10.0,
    )
    all_output += out
    log_cb(f"Enrollment script output:\n{out}")

    # ── 6. Validate enrollment — poll NodeCredential progress ───
    # The enrollmentProgress can transition IDLE → ONGOING → SUCCESS.
    # Per spec: retry 2 times for credential status, ~2 minutes per
    # retry (= 1 initial check + 1 retry, 120 s apart). Slow nodes
    # that are still transitioning get one full 2-minute window before
    # we bother the user.
    log_cb("Validating enrollment (NodeCredential enrollmentProgress)...")
    enroll_success = False
    max_auto_polls = 2
    poll_interval = 120
    out = ""
    for attempt in range(1, max_auto_polls + 1):
        out = ssh.run_amos_command_safe(
            "get NodeCredential enrollmentProgress result",
            node_name, timeout=60,
        )
        all_output += out
        log_cb(f"Validation poll #{attempt}/{max_auto_polls}:\n{out}")

        if "SUCCESS" in out:
            enroll_success = True
            break
        # Hard failure — don't keep polling if the node says ERROR/FAILURE
        if "FAILURE" in out or "ERROR" in out.upper().replace("IDLE/ERROR", ""):
            log_cb("Hard enrollment error detected — stopping auto-poll.")
            break
        if attempt < max_auto_polls:
            log_cb(f"Still not SUCCESS — waiting {poll_interval}s before next check...")
            time.sleep(poll_interval)

    if not enroll_success:
        advice = (
            f"Enrollment validation for {node_name} is not yet SUCCESS after "
            f"{max_auto_polls} auto-checks.\n\n"
            f"Recommended manual checks (in another AMOS window):\n"
            f"  1. get NodeCredential enrollmentProgress       ← should be SUCCESS\n"
            f"  2. get NodeCredential enrollmentAuthorityType  ← should be set\n"
            f"  3. get NodeCredential trustedCertificateAuthority\n"
            f"  4. alt                                          ← check for open alarms\n\n"
            f"If the node shows ONGOING, just wait ~30s and click Retry.\n"
            f"If it already shows SUCCESS manually but this dialog says not, click Retry — "
            f"we'll re-check once (fast) instead of polling."
        )
        log_cb(f"✗ Enrollment not yet validated; asking user.")
        while not enroll_success:
            if not wait_for_user:
                return False, all_output
            retry = wait_for_user(advice)
            if not retry:
                log_cb("User chose to stop.")
                return False, all_output
            log_cb("Re-checking enrollment status (single fast check)...")
            out = ssh.run_amos_command_safe(
                "get NodeCredential enrollmentProgress result",
                node_name, timeout=60,
            )
            all_output += out
            log_cb(f"Re-check output:\n{out}")
            enroll_success = "SUCCESS" in out
    log_cb("✓ Enrollment validated — SUCCESS.")

    # ── 7-8. Force sync and wait for SYNCHRONIZED ────────────────
    # Per spec: 2 attempts at 2-minute intervals for the sync check.
    synced, sync_output = _ensure_node_synchronized(
        ssh,
        node_name,
        log_cb,
        wait_for_user=wait_for_user,
        force_first=True,
        poll_interval=120,
        max_attempts=2,
    )
    all_output += sync_output
    if not synced:
        return False, all_output

    log_cb(f"✓ {node_name} is SYNCHRONIZED. Enrollment complete.")
    return True, all_output


def _parse_sync_state(output: str) -> str:
    """Extract the node sync state from CLI output.

    Check UNSYNCHRONIZED before SYNCHRONIZED because the former contains the
    latter as a substring.
    """
    upper = output.upper()
    if "UNSYNCHRONIZED" in upper:
        return "UNSYNCHRONIZED"
    if "SYNCHRONIZED" in upper:
        return "SYNCHRONIZED"
    if "PENDING" in upper:
        return "PENDING"
    if "TOPOLOGY" in upper:
        return "TOPOLOGY"
    return "UNKNOWN"


def _ensure_node_synchronized(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    *,
    force_first: bool = False,
    poll_interval: int = 5,
    max_attempts: int = 60,
) -> tuple[bool, str]:
    """Ensure the node reaches SYNCHRONIZED using the standard CLI commands."""
    all_output = ""
    sync_cmd = f'!python {CLI_PY} "cmedit get {node_name} cmfunction.syncstatus -t"'
    force_sync_cmd = f'!python {CLI_PY} "cmedit action {node_name} cmfunction=1 SYNC"'

    def _run_force_sync(prefix: str) -> None:
        nonlocal all_output
        log_cb(prefix)
        out = ssh.run_amos_command_safe(force_sync_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Sync command output:\n{out}")

    def _poll_cycle(cycle_label: str) -> bool:
        nonlocal all_output
        forced_sync = False

        if force_first:
            _run_force_sync(f"Forcing sync for {node_name}...")
            forced_sync = True

        log_cb(cycle_label)
        for attempt in range(1, max_attempts + 1):
            out = ssh.run_amos_command_safe(sync_cmd, node_name, timeout=60)
            all_output += out
            state = _parse_sync_state(out)
            log_cb(f"Sync check #{attempt}/{max_attempts} [{state}]:\n{out}")

            if state == "SYNCHRONIZED":
                log_cb(f"✓ {node_name} is SYNCHRONIZED.")
                return True

            if state == "UNSYNCHRONIZED" and not forced_sync:
                _run_force_sync(
                    f"{node_name} is UNSYNCHRONIZED — forcing sync before continuing..."
                )
                forced_sync = True

            if attempt < max_attempts:
                log_cb(
                    f"Status is {state}; waiting {poll_interval}s before next check..."
                )
                time.sleep(poll_interval)

        return False

    if _poll_cycle(f"Waiting for {node_name} to reach SYNCHRONIZED status..."):
        return True, all_output

    msg = (
        f"Sync for {node_name} did not reach SYNCHRONIZED after "
        f"{max_attempts} checks."
    )
    log_cb(f"✗ {msg}")

    while wait_for_user:
        retry = wait_for_user(
            f"{msg}\n\nClick Retry to keep checking, or Stop to abort."
        )
        if not retry:
            log_cb("User chose to stop.")
            return False, all_output
        if _poll_cycle(f"Re-checking sync status for {node_name}..."):
            return True, all_output

    return False, all_output


# ── LKF step ─────────────────────────────────────────────────────
def run_install_lkf(
    ssh: IntegrationSSH,
    node_name: str,
    lkf_local_path: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Install LKF and poll status. Works with or without a zip file.

    Two modes:

      A) Zip provided (``lkf_local_path`` non-empty):
         0. Fast-path: skip upload+import if the per-node XML is
            already on SMRS.
         1. SFTP upload LKF zip to ~/LKF/
         2. lkfimport.py <zipfile>.zip
         3. lkfinstall.py <nodename>  → extract job name
         4. lkfstatus.py <jobname>    → poll until COMPLETED

      B) No zip (``lkf_local_path`` empty/None):
         Skip steps 0/1/2 entirely. Go straight to step 3
         (lkfinstall.py) — the license may already be imported in ENM
         from a previous batch. Then poll status. If install/status
         fails, return False WITHOUT prompting (caller continues to
         the next step and just logs "LKF not available").

    Must be called while inside an AMOS session (or will enter one).

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
    has_zip = bool(lkf_local_path and str(lkf_local_path).strip())
    # In no-zip mode we never prompt the operator — failures are
    # non-blocking (caller continues). This overrides any
    # wait_for_user passed in.
    if not has_zip:
        wait_for_user = None
        log_cb(
            f"No LKF zip provided — running lkfinstall.py for "
            f"{node_name} directly (license may already be imported "
            "in ENM). Will not prompt on failure; just report and "
            "continue."
        )

    if has_zip:
        zip_filename = os.path.basename(lkf_local_path)

        # ── 0. Fast-path check: is this node's LKF already on SMRS? ─
        # We open the zip locally (no upload yet) and look for the
        # per-node XML inside. If a file with the same name is already in
        # ``<smrs_dir>/<node>/`` on the gateway, the import was done in a
        # previous run and we can skip straight to ``lkfinstall.py`` —
        # saves an SFTP upload + a 10-minute lkfimport.py run.
        target_xml = _find_node_lkf_in_zip(lkf_local_path, node_name)
        skip_upload_and_import = False
        if target_xml:
            log_cb(
                f"Found LKF XML for {node_name} in zip: {target_xml} "
                f"(matched non-_info.xml entry)"
            )
            try:
                if _lkf_already_on_smrs(ssh, node_name, target_xml, log_cb):
                    log_cb(
                        f"✓ LKF already on SMRS for {node_name} — "
                        "skipping SFTP upload and lkfimport.py."
                    )
                    skip_upload_and_import = True
                    all_output += (
                        f"[fast-path] LKF {target_xml} already present at "
                        f"{_SMRS_LICENCE_DIR}/{node_name}/ — skipping import.\n"
                    )
                else:
                    log_cb(
                        f"LKF not present at "
                        f"{_SMRS_LICENCE_DIR}/{node_name}/ — will do full "
                        "upload + import flow."
                    )
            except Exception as exc:
                log_cb(
                    f"(SMRS pre-check failed: {exc}; will do full flow)"
                )
        else:
            log_cb(
                f"No matching <{node_name}>_*.xml found in {zip_filename} — "
                "will do full upload + import flow."
            )

        if skip_upload_and_import:
            log_cb("Skipping steps 1 & 2 (upload + import) — fast-path.")
        else:
            # ── 1. Upload LKF file via SFTP ──────────────────────────
            log_cb(f"Uploading LKF file: {zip_filename}...")
            try:
                # Upload to /home/shared/<username>/LKF
                lkf_remote_dir = f"/home/shared/{ssh.username}/LKF"
                remote_path = ssh.sftp_upload(lkf_local_path, lkf_remote_dir)
                # Resolve ~ for display (actual path handled by SFTP)
                all_output += f"[SFTP] Uploaded {zip_filename} → ~/LKF/{zip_filename}\n"
                log_cb(f"✓ LKF file uploaded to ~/LKF/{zip_filename}")
            except Exception as exc:
                msg = f"SFTP upload failed: {exc}"
                log_cb(f"✗ {msg}")
                all_output += f"[SFTP] {msg}\n"
                if wait_for_user:
                    retry = wait_for_user(
                        f"LKF file upload failed: {exc}\n\n"
                        f"Please upload the file manually to ~/LKF/ and click Retry."
                    )
                    if not retry:
                        return False, all_output
                else:
                    return False, all_output

            # ── 2. Import LKF (direct SSH exec — faster than !python) ──
            log_cb(f"Importing LKF: {zip_filename}...")
            out = ssh.exec_ssh(
                f"python {_LKF_IMPORT} /home/shared/{ssh.username}/LKF/{zip_filename}",
                timeout=600,
            )
            all_output += out
            log_cb(f"lkfimport.py output:\n{out}")

            # Validate the import succeeded BEFORE proceeding to install.
            # Previously we plowed straight into ``lkfinstall.py`` regardless,
            # so an import error (corrupt zip, ENM down) showed up as a
            # confusing "no job name" later instead of a clear import failure.
            upper_imp = out.upper()
            import_ok = (
                "IMPORTED SUCCESSFULLY" in upper_imp
                or "IMPORT SUCCESSFUL" in upper_imp
                or "ALREADY EXISTS" in upper_imp     # already imported earlier
                or "ALREADY IMPORTED" in upper_imp
            )
            # Explicit failure markers — but be careful not to false-positive
            # on harmless lines like "Error description: None".
            import_failed = bool(
                re.search(r"\bFAIL(?:ED|URE)?\b", upper_imp)
                or re.search(r"\bEXCEPTION\b", upper_imp)
                or "TRACEBACK" in upper_imp
            )
            if import_failed and not import_ok:
                msg = (
                    f"LKF import failed for {zip_filename}.\n"
                    f"Output tail:\n{out[-500:]}"
                )
                log_cb(f"✗ {msg}")
                if wait_for_user:
                    retry = wait_for_user(
                        f"{msg}\n\nFix the issue (e.g. re-export the LKF, check ENM) "
                        f"and click Retry, or Stop to abort."
                    )
                    if not retry:
                        return False, all_output
                    # One re-attempt
                    out = ssh.exec_ssh(
                        f"python {_LKF_IMPORT} "
                        f"/home/shared/{ssh.username}/LKF/{zip_filename}",
                        timeout=600,
                    )
                    all_output += out
                    log_cb(f"Retry import output:\n{out}")
                    upper_imp = out.upper()
                    if (re.search(r"\bFAIL(?:ED|URE)?\b", upper_imp) and
                            "IMPORTED" not in upper_imp and
                            "ALREADY" not in upper_imp):
                        log_cb("✗ Import still failed.")
                        return False, all_output
                else:
                    return False, all_output
            log_cb("✓ LKF imported (or already present in ENM).")

    # ── 3. Install LKF — extract job name (direct SSH exec) ──────
    log_cb(f"Installing LKF for {node_name}...")
    out = ssh.exec_ssh(
        f"python {_LKF_INSTALL} {node_name}",
        timeout=600,
    )
    all_output += out
    log_cb(f"lkfinstall.py output:\n{out}")

    # Extract job name with a regex that tolerates case + spacing +
    # trailing punctuation, e.g.
    #   "Job started with Job Name : Shm_Cli_InstallLicense_USER_..."
    #   "with job name:Shm_Cli_..."
    # The job-name token is a typical Shm_* identifier — we anchor on
    # that pattern instead of just the label, so a stray "job name:"
    # label without a value can't capture garbage.
    JOB_NAME_RE = re.compile(
        r"job\s*name\s*[:=]?\s*([A-Za-z][A-Za-z0-9_.\-]*)",
        re.IGNORECASE,
    )
    SHM_JOB_RE = re.compile(r"\b(Shm_Cli_InstallLicense_[A-Za-z0-9_.\-]+)\b")

    def _extract_job_name(text: str) -> Optional[str]:
        # Prefer a labelled match; fall back to a bare Shm_* identifier
        m = JOB_NAME_RE.search(text)
        if m:
            cand = m.group(1).rstrip(".,;:")
            # Reject obviously bad captures (label echoed without value)
            if len(cand) >= 4 and cand.lower() != "name":
                return cand
        m = SHM_JOB_RE.search(text)
        if m:
            return m.group(1)
        return None

    def _find_existing_install_jobs(text: str) -> list[str]:
        """Scan ``text`` for ALL ``Shm_Cli_InstallLicense_*`` job IDs.

        ENM job names embed the username + timestamp, so the same node
        can have multiple historical jobs. We sort lexicographically:
        the timestamp suffix (``YYYYMMDDHHMMSS`` form) makes sorting
        equivalent to "most recent last".
        """
        ids = SHM_JOB_RE.findall(text)
        # De-dup while preserving order, then sort to put most-recent last
        seen: dict[str, None] = {}
        for j in ids:
            seen[j] = None
        return sorted(seen.keys())

    job_name = _extract_job_name(out)

    if not job_name:
        # Before bothering the operator (and risking a duplicate
        # submission), scan EVERYTHING we've collected so far for a
        # Shm_Cli_InstallLicense_* token. ``lkfinstall.py`` sometimes
        # prints the job name on a delayed line that the strict
        # extractor misses — or it appears in a "queued" status line
        # that doesn't include the literal "job name:" label. If we
        # find one, we can poll its status directly without re-submit.
        recovered = _find_existing_install_jobs(all_output)
        if recovered:
            # Take the most recent (last after sort) — that's the one
            # this very lkfinstall.py call just submitted.
            job_name = recovered[-1]
            log_cb(
                f"✓ Recovered job name '{job_name}' from accumulated "
                "output without re-submitting (avoids duplicate ENM job)."
            )
            if len(recovered) > 1:
                log_cb(
                    f"  (also saw {len(recovered) - 1} older job(s) in "
                    f"output; using the most recent)"
                )

    if not job_name:
        # Last-resort: still need to re-submit. Warn the operator
        # explicitly that this MAY create a duplicate if the first
        # attempt actually did kick off a job in ENM that we just
        # couldn't see in the output.
        msg = (
            f"Could not extract or recover any "
            f"Shm_Cli_InstallLicense_* job ID for {node_name}.\n\n"
            f"Last output tail:\n{out[-400:]}"
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nClicking Retry will RE-SUBMIT the install. "
                f"Before clicking Retry, please verify in ENM that no "
                f"existing 'Shm_Cli_InstallLicense_*' job for "
                f"{node_name} is still running — otherwise you'll end "
                f"up with two parallel install jobs.\n\n"
                f"Click Retry to re-submit, or Stop to abort."
            )
            if not retry:
                return False, all_output
            # Operator confirmed re-submit
            out2 = ssh.exec_ssh(
                f"python {_LKF_INSTALL} {node_name}",
                timeout=300,
            )
            all_output += out2
            log_cb(f"Re-submit output:\n{out2}")
            job_name = _extract_job_name(out2)
            if not job_name:
                # Even after re-submit, last-chance recovery scan
                recovered2 = _find_existing_install_jobs(all_output)
                if recovered2:
                    job_name = recovered2[-1]
                    log_cb(
                        f"✓ Recovered job '{job_name}' from re-submit output."
                    )
            if not job_name:
                log_cb("✗ Still no job name found after re-submit.")
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ Job name: {job_name}")

    # ── 4. Poll status until COMPLETED (direct SSH exec) ─────────
    # Look for the *job's status field* explicitly, not just the
    # substring "ERROR" anywhere in output. Lines like
    #     "Error description: None"
    # used to early-exit the poll with a false fatal-error.
    log_cb(f"Checking LKF installation status for job: {job_name}...")
    status_cmd = f"python {_LKF_STATUS} {job_name}"
    # lkfinstall can take a while to settle; give the job more polling time
    # before we fall back to the node-side alt check.
    max_attempts = 30
    completed = False

    # Real fatal states the LKF status tool prints — anchored to a
    # status field, not free-text.
    FATAL_RE = re.compile(
        r"(?:^|\s)(?:status|state)\s*[:=]\s*"
        r"(failed|failure|error|cancelled|canceled|terminated)\b",
        re.IGNORECASE,
    )
    # ``Status : COMPLETED`` means the JOB finished — but NOT that the
    # license was actually installed. The real outcome is the RESULT
    # field. A job can finish COMPLETED with ``Result : SKIPPED`` when
    # the Licence Key File isn't found — that is a FAILURE for us.
    STATUS_COMPLETED_RE = re.compile(
        r"(?:^|\s)status\s*[:=]\s*completed\b", re.IGNORECASE,
    )
    # Markers that the install did NOT actually happen even though the
    # job "completed":
    SKIPPED_RE = re.compile(
        r"(?:^|\s)result\s*[:=]\s*skipped\b", re.IGNORECASE,
    )
    NODES_SKIPPED_RE = re.compile(
        r"No\s+of\s+Nodes\s+Skipped\s*[:=]\s*([1-9]\d*)", re.IGNORECASE,
    )
    NOT_FOUND_RE = re.compile(
        r"licen[cs]e\s*key\s*file\s*not\s*found"
        r"|activity\s+is\s+skipped"
        r"|\bnot\s+found\b",
        re.IGNORECASE,
    )
    # Positive confirmation the install really succeeded.
    RESULT_SUCCESS_RE = re.compile(
        r"(?:^|\s)result\s*[:=]\s*success\b", re.IGNORECASE,
    )
    NODES_COMPLETED_RE = re.compile(
        r"No\s+of\s+Nodes\s+Completed\s*[:=]\s*([1-9]\d*)", re.IGNORECASE,
    )

    skipped = False
    for attempt in range(1, max_attempts + 1):
        out = ssh.exec_ssh(status_cmd, timeout=120)
        all_output += out
        log_cb(f"Status check #{attempt}:\n{out}")

        status_completed = (
            STATUS_COMPLETED_RE.search(out)
            or "COMPLETED" in out.split("\n")[-5:].__str__().upper()
        )
        if status_completed:
            # Job reached a terminal state. Now decide the REAL result.
            install_skipped = bool(
                SKIPPED_RE.search(out)
                or NODES_SKIPPED_RE.search(out)
                or NOT_FOUND_RE.search(out)
            )
            install_ok = bool(
                RESULT_SUCCESS_RE.search(out)
                or NODES_COMPLETED_RE.search(out)
            )
            if install_skipped and not (
                install_ok and not SKIPPED_RE.search(out)
            ):
                # Completed-but-skipped → license NOT installed.
                skipped = True
                log_cb(
                    "LKF job COMPLETED but Result=SKIPPED — license was "
                    "NOT installed (Licence Key File not found)."
                )
                break
            completed = True
            break
        if FATAL_RE.search(out):
            log_cb("Terminal failure detected in LKF status field — stopping poll.")
            break

        # Back off: 10s for first 5 attempts, then 20s after
        delay = 10 if attempt <= 5 else 20
        if attempt < max_attempts:
            log_cb(f"Status not yet COMPLETED ({attempt}/{max_attempts}), "
                   f"waiting {delay}s...")
            time.sleep(delay)

    # ── 5. Second-side verification via alt (node-side ground truth) ──
    # The install job status can lag or report SKIPPED while the license is
    # in fact present on the node. The node's own alarm list is authoritative:
    # if there is NO "License Key File Fault" alarm in ``alt``, the LKF is
    # installed. Only used to RESCUE a skipped/timeout verdict (never to
    # downgrade a job that already reported success), and we re-read a few
    # times so a slow-finishing install has a chance to clear the alarm.
    _LKF_FAULT_RE = re.compile(r"licen[cs]e\s*key\s*file\s*fault", re.IGNORECASE)
    if skipped or not completed:
        for a in range(1, 4):
            try:
                log_cb(f"Verifying via alt (#{a}/3): checking the node's alarm "
                       "list for 'License Key File Fault'...")
                alt_out = ssh.run_amos_command_safe("alt", node_name, timeout=120)
                all_output += alt_out
                log_cb(f"alt output:\n{alt_out}")
            except Exception as exc:
                log_cb(f"(alt check failed: {exc})")
                break
            if not _LKF_FAULT_RE.search(alt_out):
                log_cb(f"✓ No 'License Key File Fault' alarm on {node_name} — "
                       "LKF confirmed installed via alt (job status was "
                       "SKIPPED/incomplete but the node is fine).")
                return True, all_output
            if a < 3:
                log_cb("'License Key File Fault' still present — the install "
                       "may still be finishing; waiting 20s before re-check...")
                time.sleep(20)
        log_cb("alt still shows 'License Key File Fault' (or alt unavailable).")

    if skipped:
        msg = (
            f"LKF NOT installed for {node_name} — the job completed but "
            f"the install was SKIPPED (Licence Key File not found), and the "
            f"node still raises a 'License Key File Fault' alarm. Make sure "
            f"the LKF for this node is imported in ENM / present in SMRS, or "
            f"provide the LKF zip."
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    if not completed:
        msg = (
            f"LKF installation is failed, need manual check.\n"
            f"Job '{job_name}' did not reach COMPLETED after {max_attempts} "
            f"attempts, and the node still raises a 'License Key File Fault' "
            f"alarm."
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"✓ LKF installation COMPLETED + installed for {node_name} (job: {job_name}).")
    return True, all_output


# ── Baseline step ────────────────────────────────────────────────
BASELINE_MAP = _BASELINE_FILES


def run_baseline(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    confirm_baseline: Optional[Callable[[str, str], bool]] = None,
) -> tuple[bool, str]:
    """Run baseline script based on node RAT type.

    Sub-steps:
      1. Check $rats with ``pv $rats``
      2. Determine baseline MOS file, confirm with user
      3. List available baseline files for user verification
      4. Run the baseline MOS
      5. Verify with ``cvls`` — look for POST_<baseline>_execution entry

    Args:
        confirm_baseline: Callable(title, message) -> bool, shown before
            running the baseline to let user verify the correct file.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # Baseline path is configurable so teams can switch script locations
    # without editing code. Relative values resolve under scripts_path.
    # ``_BASELINE_SCRIPT`` is already a full absolute path
    # (``_resolve_script_path`` normalized it at module load).
    baseline_path = _BASELINE_SCRIPT
    log_cb(f"Baseline file: {baseline_path}")

    # ── Run baseline ─────────────────────────────────────────────
    # Operator's mental model for "baseline done":
    #   1. We issue ``run <baseline.mos>``.
    #   2. Baseline fires many set/crn commands; each fills when the
    #      AMOS prompt is ready for the next one.
    #   3. The prompt going idle (no more commands being pushed into it)
    #      for ~10 s = the script has finished its last command.
    #   4. Then cvls must show a ``Post_Globe_Baseline...`` backup entry.
    #
    # ``run_amos_blocking_with_sentinel`` is the deterministic equivalent
    # of step 3: we queue ``!echo __TRFS_DONE_<nonce>__`` right after
    # ``run``, so the nonce only prints when moshell is actually back at
    # an AMOS prompt with nothing queued. We then additionally require
    # 10 s of channel silence (``quiet_after=10.0``) — matching the
    # operator's manual rule and guarding against any trailing prompt
    # redraw or ANSI noise.
    log_cb(f"Running baseline: run {baseline_path}")
    log_cb(
        "(sentinel + 10s idle quiescence; baseline can run 30-60 min — "
        "heartbeat every 60s shows it's still alive)"
    )
    out = ssh.run_amos_blocking_with_sentinel(
        f"run {baseline_path}",
        node_name, timeout=3600, quiet_after=10.0,
    )
    all_output += out
    log_cb(f"Baseline output:\n{out}")

    # ── 6. Verify with cvls ──────────────────────────────────────
    log_cb("Verifying baseline with cvls...")
    out = ssh.run_amos_command_safe("cvls", node_name, timeout=60)
    all_output += out
    log_cb(f"cvls output:\n{out}")

    # Look for any "Post_Globe_Baseline" backup entry in cvls output.
    # Real cvls shows entries like:
    #   "Post_Globe_Baseline_All_Bands_execution_260414_1832"
    #   "POST_Globe_Baseline_L_NonModular_Rev_15042026_execution_260415_2055"
    # Accept both capitalizations. Match anywhere in the cvls backup list.
    def _baseline_done(text: str) -> bool:
        low = text.lower()
        return "post_globe_baseline" in low

    if _baseline_done(out):
        log_cb(f"✓ Baseline verified — 'Post_Globe_Baseline' backup found in cvls")
        return True, all_output

    # Not found — retry loop
    msg = (
        f"Baseline verification failed for {node_name}.\n"
        f"Expected a 'Post_Globe_Baseline...' backup entry in cvls output."
    )
    log_cb(f"✗ {msg}")
    while True:
        if not wait_for_user:
            return False, all_output
        retry = wait_for_user(
            f"{msg}\n\nClick Retry to re-check cvls, or Stop to abort."
        )
        if not retry:
            log_cb("User chose to stop.")
            return False, all_output
        out = ssh.run_amos_command_safe("cvls", node_name, timeout=60)
        all_output += out
        log_cb(f"Re-check cvls:\n{out}")
        if _baseline_done(out):
            log_cb(f"✓ Baseline verified — 'Post_Globe_Baseline' backup found in cvls")
            return True, all_output


# ── URI Setting step ─────────────────────────────────────────────
def run_uri_setting(
    ssh: IntegrationSSH,
    node_name: str,
    enm_username: str,
    enm_password: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Configure URI on the node and update FTP server details via ENM REST API.

    AMOS commands:
      1. set SwM=1 defaultUri
      2. set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID} uri
      3. set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID}
         password cleartext=true,password=

    Bash commands (via ! prefix in AMOS):
      4. Ensure node is SYNCHRONIZED
      5. curl login to get cookie
      6. curl POST to updateUpMoFtpServerDetails with node name

    Both curl commands must return "SUCCESS" to pass.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # ── 1. UpgradePackage ID (from config.json) ─────────────────
    # Use the configured target version directly
    # (uri_setting.upgrade_package_id) — NO auto-detect. This keeps URI
    # Setting and SW Level Check on the SAME source of truth. Change the
    # target by editing config.json next to the exe (no rebuild).
    upgrade_pkg_id = _UPGRADE_PKG_ID
    log_cb(f"Using UpgradePackage from config.json: {upgrade_pkg_id}")

    # ── 2-4. AMOS set commands (each requires y/n confirmation) ─
    amos_cmds = [
        "set SwM=1 defaultUri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={upgrade_pkg_id} uri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={upgrade_pkg_id} "
        "password cleartext=true,password=",
    ]

    for cmd in amos_cmds:
        log_cb(f"Running (with y/n confirm): {cmd}")
        out = ssh.run_amos_set_with_confirm(cmd, node_name, answer="y", timeout=60)
        all_output += out
        log_cb(f"Output:\n{out}")

    # ENM needs a few seconds to commit set-changes into its internal
    # model before ``updateUpMoFtpServerDetails`` will see them. Without
    # this small wait the curl POST sometimes runs against a stale view
    # and returns failure even though the AMOS sets succeeded.
    log_cb("Pausing 3 s for ENM to commit URI/SwM/UpgradePackage changes…")
    time.sleep(3)

    # ── 4. Ensure node is SYNCHRONIZED before ENM curl calls ────
    sync_ok, sync_output = _ensure_node_synchronized(
        ssh,
        node_name,
        log_cb,
        wait_for_user=wait_for_user,
        poll_interval=5,
        max_attempts=60,
    )
    all_output += sync_output
    if not sync_ok:
        return False, all_output

    # ── 5. curl login to get cookie ─────────────────────────────
    # Cookie filename includes the node name so concurrent multi-node
    # runs don't clobber each other's session. Previously every node
    # wrote to ``./cookie.txt`` and the last login won, causing random
    # "session timeout" failures on the other nodes' curl POST.
    safe_node = re.sub(r"[^A-Za-z0-9._-]", "_", node_name)
    cookie_file = f"./cookie_{safe_node}.txt"

    # Track the cookie path for end-of-step cleanup so each run
    # doesn't leave a stale cookie behind on the gateway. The bare
    # ``cookie_<NODE>.txt`` in the user's home folder is what we want
    # to remove. Path is resolved at cleanup time, not here.
    def _cleanup_cookie():
        # ``cookie_file`` starts with ``./`` — strip and prefix the
        # SSH user's home so SFTP can find the absolute path.
        try:
            bare = cookie_file.lstrip("./").lstrip("/")
            abs_path = f"/home/shared/{ssh.username}/{bare}"
            ssh.sftp_remove(abs_path)
        except Exception:
            pass
    login_cmd = (
        f'!curl --insecure --silent --show-error --max-time 30 '
        f'--request POST '
        f'--data "IDToken1={enm_username}" '
        f'--data "IDToken2={enm_password}" '
        f'--cookie-jar {cookie_file} {ENM_LOGIN_URL}'
    )

    def _is_login_ok(login_output: str) -> bool:
        """Accept any of the success indicators ENM has used over time
        (string varies between releases / locales)."""
        upper = login_output.upper()
        return (
            "AUTHENTICATION SUCCESSFUL" in upper
            or '"SUCCESS"' in upper
            or '"STATUS":"OK"' in upper
            or '"CODE":200' in upper
            or "AUTHENTICATED" in upper
        )

    log_cb("Logging in to ENM (curl)...")
    out = ssh.run_amos_command_safe(login_cmd, node_name, timeout=60)
    all_output += out
    log_cb(f"Login output:\n{out}")

    if not _is_login_ok(out):
        # One automatic retry before bothering the operator — ENM
        # SSO occasionally returns 503 on first request after idle.
        log_cb("Login response unclear; auto-retrying once…")
        time.sleep(2)
        out = ssh.run_amos_command_safe(login_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Login retry output:\n{out}")

    if not _is_login_ok(out):
        msg = f"ENM login failed for URI setting.\nOutput: {out[:300]}"
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck credentials and click Retry."
            )
            if not retry:
                return False, all_output
            # Retry login
            out = ssh.run_amos_command_safe(login_cmd, node_name, timeout=60)
            all_output += out
            if not _is_login_ok(out):
                log_cb("✗ Login still failed after retry.")
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ ENM login successful (cookie: {cookie_file}).")

    # ── 6. curl POST to update URI FTP server details ───────────
    update_cmd = (
        f"!curl --insecure --silent --show-error --max-time 60 "
        f"--request POST "
        f"'{ENM_URI_UPDATE_URL}' "
        f"--cookie {cookie_file} "
        f'-H "Content-Type: application/json" '
        f"-d '[\"{{nodeName}}\"]'"
    ).replace("{nodeName}", node_name)

    log_cb(f"Updating URI FTP server details for {node_name}...")
    out = ssh.run_amos_command_safe(update_cmd, node_name, timeout=60)
    all_output += out
    log_cb(f"Update output:\n{out}")

    api_ok = "SUCCESS" in out.upper()
    if not api_ok:
        log_cb(f"✗ updateUpMoFtpServerDetails did not return SUCCESS.")

    # ── 6. Verify via `get depack` — uri must start with sftp://mm-software@
    log_cb("Verifying URI via: get depack")
    vout = ssh.run_amos_command_safe("get depack", node_name, timeout=60)
    all_output += vout
    log_cb(f"get depack output:\n{vout}")

    uri_value = ""
    for line in vout.splitlines():
        s = line.strip()
        if s.startswith("uri") and len(s) > 3:
            parts = s.split(None, 1)
            if len(parts) > 1:
                uri_value = parts[1].strip()
            break
    uri_ok = uri_value.startswith("sftp://mm-software@")

    if api_ok and uri_ok:
        log_cb(f"✓ URI setting completed for {node_name}.")
        log_cb(f"  uri = {uri_value}")
        _cleanup_cookie()
        return True, all_output

    msg = (
        f"URI setting verification failed for {node_name}.\n"
        f"  updateUpMoFtpServerDetails SUCCESS? {api_ok}\n"
        f"  uri starts with 'sftp://mm-software@'? {uri_ok}\n"
        f"  current uri = {uri_value or '(empty)'}"
    )
    log_cb(f"✗ {msg}")
    if wait_for_user:
        retry = wait_for_user(f"{msg}\n\nCheck manually and click Retry.")
        if not retry:
            _cleanup_cookie()
            return False, all_output
        # Retry: re-run curl update + re-verify
        out = ssh.run_amos_command_safe(update_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Retry update output:\n{out}")
        retry_api_ok = "SUCCESS" in out.upper()
        vout = ssh.run_amos_command_safe("get depack", node_name, timeout=60)
        all_output += vout
        log_cb(f"Retry get depack output:\n{vout}")
        uri_value = ""
        for line in vout.splitlines():
            s = line.strip()
            if s.startswith("uri") and len(s) > 3:
                parts = s.split(None, 1)
                if len(parts) > 1:
                    uri_value = parts[1].strip()
                break
        if retry_api_ok and uri_value.startswith("sftp://mm-software@"):
            log_cb(f"✓ URI verified on retry: {uri_value}")
            _cleanup_cookie()
            return True, all_output
        log_cb(
            "✗ URI still not valid on retry: "
            f"update SUCCESS? {retry_api_ok}, "
            f"uri = {uri_value or '(empty)'}"
        )
    _cleanup_cookie()
    return False, all_output


# ── Relation step ────────────────────────────────────────────────
def _parse_relation_output(output: str, filename: str) -> dict:
    """Parse AMOS relation script output and extract summary metrics.

    Replicates the logic of the bash validation script:
      - For Relation/Definition files: MO_TOTAL, MO_EXIST, MO_CRE, FAILED, TOTALERROR
      - For other (set) files: CMD_SET, SUCCEED, FAILED, WRONG_MO

    Returns a dict with all metrics and a formatted summary string.
    """
    import re

    lines = output.split("\n")

    # Count metrics by grepping output lines (same patterns as the bash script)
    failed = 0          # "Total: 1 MOs attempted, 0 MOs set"
    no_att = 0          # "Total: 0 MOs attempted, 0 MOs set"
    l_set = 0           # lines containing "MOs set"
    mo_total = 0        # lines with "> pr"
    mo_exist = 0        # "Total: 1 MOs$" preceded by "Proxy  MO"
    mo_cre = 0          # lines with "[Proxy ID"
    tot_failed = 0      # lines with "0 MOs set"
    att = 0             # "Total: N MOs attempted, N MOs set" where N > 0
    cmd_set = 0         # lines with "> set"
    no_change = 0       # lines with "-No Change-"
    total_error = 0     # lines with "!!!!" or "ERROR" (excluding soft ones)
    already_exists = 0  # "ERROR: MO already exists" — soft warning on re-run

    for idx, line in enumerate(lines):
        stripped = line.strip()
        # Soft warning: MO already exists (re-run on a node with relations already created)
        if "already exists" in stripped and "ERROR" in stripped:
            already_exists += 1
            continue

        if re.search(r'Total:\s+1\s+MOs\s+attempted,\s+0\s+MOs\s+set', stripped):
            failed += 1
        if re.search(r'Total:\s+0\s+MOs\s+attempted,\s+0\s+MOs\s+set', stripped):
            no_att += 1
        if 'MOs set' in stripped:
            l_set += 1
        if '> pr' in stripped:
            mo_total += 1
        if re.search(r'Total:\s+1\s+MOs\s*$', stripped):
            # Check previous lines for "Proxy  MO"
            context = "\n".join(lines[max(0, idx - 8):idx + 1])
            if 'Proxy  MO' in context:
                mo_exist += 1
        if '[Proxy ID' in stripped:
            mo_cre += 1
        if '0 MOs set' in stripped:
            tot_failed += 1
        if re.search(
            r'Total:\s+[1-9]\d?\s+MOs\s+attempted,\s+[1-9]\d?\s+MOs\s+set',
            stripped
        ):
            att += 1
        if '> set' in stripped:
            cmd_set += 1
        if '-No Change-' in stripped:
            no_change += 1
        if '!!!!' in stripped or 'ERROR' in stripped:
            total_error += 1

    # Determine file type for display format
    is_relation = any(
        kw in filename for kw in ("Relation", "Definition", "RNC")
    )

    if is_relation:
        summary_line = (
            f"MO_TOTAL={mo_total}  MO_EXIST={mo_exist}  "
            f"MO_CREATED={mo_cre}  FAILED={tot_failed}  "
            f"MO_N/A={no_att}  ERROR={total_error}  "
            f"ALREADY_EXISTS={already_exists}"
        )
        has_issues = (total_error > 0 or tot_failed > 0)
    else:
        summary_line = (
            f"CMD_SET={cmd_set}  SUCCEED={att}  "
            f"FAILED={failed}  WRONG_MO={no_att}  "
            f"NO_CHANGE={no_change}  ERROR={total_error}  "
            f"ALREADY_EXISTS={already_exists}"
        )
        has_issues = (total_error > 0 or failed > 0)

    # Collect error details (skip soft "already exists" warnings)
    error_lines = []
    for i, line in enumerate(lines):
        if "already exists" in line and "ERROR" in line:
            continue
        if '!!!!' in line or 'ERROR' in line:
            # Include context line before the error if available
            if i > 0 and '>' in lines[i - 1]:
                error_lines.append(lines[i - 1].strip())
            error_lines.append(line.strip())

    return {
        "is_relation": is_relation,
        "failed": failed if not is_relation else tot_failed,
        "no_att": no_att,
        "l_set": l_set,
        "mo_total": mo_total,
        "mo_exist": mo_exist,
        "mo_cre": mo_cre,
        "tot_failed": tot_failed,
        "att": att,
        "cmd_set": cmd_set,
        "no_change": no_change,
        "total_error": total_error,
        "already_exists": already_exists,
        "summary_line": summary_line,
        "has_issues": has_issues,
        "error_lines": error_lines,
    }


def run_relation(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    relation_local_path: str,
    log_dir: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    ui_cb: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """Upload relation file and run it on the node.

    Supports two input types:
      - **.xml** → upload, run ``netconf /path/file.xml``, check for <ok/> or </error-message>
      - **.zip** → upload, unzip, find node folder, run each .txt with ``run <filepath>``

    Args:
        log_cb: detail log (file). High-volume moshell output.
        ui_cb:  optional high-level UI log for live milestones
                (which script is running, etc.). Defaults to log_cb.

    Returns:
        (success: bool, full_output: str)
    """
    if ui_cb is None:
        ui_cb = log_cb
    all_output = ""
    filename = os.path.basename(relation_local_path)
    is_xml = filename.lower().endswith(".xml")

    # ── 0. Pre-step: set + verify SystemConstant 4631:1 ──────────
    # Some baselines don't include this SC, but the neighbour relation
    # scripts rely on it. Set it once at the top of the relation step,
    # then verify with ``scg`` — we look for a line like
    #     default     4631:1
    # in the "Namespace   SystemConstants" block. ``scw`` may or may
    # not prompt for y/n depending on moshell version, so we use
    # ``run_amos_set_with_confirm`` which handles both transparently.
    log_cb("Setting SystemConstant 4631:1 (pre-relation step)...")
    sc_out = ssh.run_amos_set_with_confirm(
        "scw 4631:1", node_name, answer="y", timeout=60,
    )
    all_output += sc_out
    log_cb(f"scw 4631:1 output:\n{sc_out}")

    log_cb("Verifying with 'scg'...")
    scg_out = ssh.run_amos_command_safe("scg", node_name, timeout=30)
    all_output += scg_out
    log_cb(f"scg output:\n{scg_out}")

    # Look for "default<whitespace>4631:1" — the line from the
    # "Namespace   SystemConstants" block. Word boundary at the end
    # to avoid matching ``4631:10`` etc.
    sc_re = re.compile(r"^\s*default\s+4631:1\s*$", re.MULTILINE)
    sc_ok = bool(sc_re.search(scg_out))
    if not sc_ok:
        msg = (
            f"SystemConstant 4631:1 not visible in 'scg' output for "
            f"{node_name}. Expected a line like 'default     4631:1' "
            f"in the SystemConstants namespace block."
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nIf you've already set this manually, click "
                f"Retry to re-verify with 'scg'. Click Stop to abort "
                f"the relation step."
            )
            if not retry:
                return False, all_output
            scg_out2 = ssh.run_amos_command_safe("scg", node_name, timeout=30)
            all_output += scg_out2
            log_cb(f"Re-check scg output:\n{scg_out2}")
            if not sc_re.search(scg_out2):
                log_cb("✗ SystemConstant 4631:1 still not visible after retry.")
                return False, all_output
        else:
            return False, all_output

    log_cb("✓ SystemConstant 4631:1 confirmed in 'scg' output.")

    # ── 1. Upload relation file via SFTP ─────────────────────────
    remote_dir = f"/home/shared/{ssh.username}/RELATION/{shortcode}"
    log_cb(f"Uploading relation file: {filename} → {remote_dir}/")
    try:
        ssh.sftp_upload(relation_local_path, remote_dir)
        all_output += f"[SFTP] Uploaded {filename} → {remote_dir}/{filename}\n"
        log_cb(f"✓ Relation file uploaded.")
    except Exception as exc:
        msg = f"SFTP upload failed: {exc}"
        log_cb(f"✗ {msg}")
        all_output += f"[SFTP] {msg}\n"
        if wait_for_user:
            retry = wait_for_user(
                f"Relation file upload failed: {exc}\n\n"
                f"Upload manually to {remote_dir}/ and click Retry."
            )
            if not retry:
                return False, all_output
        else:
            return False, all_output

    if is_xml:
        return _run_relation_xml(
            ssh, node_name, remote_dir, filename, log_dir, log_cb, all_output,
            wait_for_user,
        )
    else:
        return _run_relation_zip(
            ssh, node_name, shortcode, remote_dir, filename,
            relation_local_path, log_dir, log_cb, all_output,
            wait_for_user, ui_cb=ui_cb,
        )


def _run_relation_xml(
    ssh: IntegrationSSH,
    node_name: str,
    remote_dir: str,
    filename: str,
    log_dir: str,
    log_cb: Callable[[str], None],
    all_output: str,
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Run a single relation XML via ``netconf`` in AMOS."""
    remote_path = f"{remote_dir}/{filename}"
    log_cb(f"Running netconf: {filename}...")

    out = ssh.run_amos_command_safe(
        f"netconf {remote_path}",
        node_name, timeout=900,
    )
    all_output += out

    # ── Check result ─────────────────────────────────────────────
    has_error = "</error-message>" in out
    has_ok = "<ok/>" in out

    if has_error:
        status = "FAILED — </error-message> detected, check manually"
        log_cb(f"✗ {status}")
    elif has_ok:
        status = "OK — <ok/> received"
        log_cb(f"✓ {status}")
    else:
        status = "UNKNOWN — no <ok/> or </error-message> found, check manually"
        log_cb(f"⚠ {status}")

    # ── Save log ─────────────────────────────────────────────────
    rel_log_name = f"RELATION_{node_name}_NETCONF.txt"
    rel_log_path = os.path.join(log_dir, rel_log_name)
    try:
        with open(rel_log_path, "w", encoding="utf-8") as f:
            f.write(f"Relation NETCONF Log — {node_name}\n")
            f.write(f"File: {filename}\n")
            f.write(f"Remote: {remote_path}\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Status: {status}\n")
            f.write("=" * 72 + "\n\n")
            f.write(out)
        log_cb(f"Log saved: {rel_log_name}")
    except Exception as exc:
        log_cb(f"Failed to save log: {exc}")

    if has_error:
        if wait_for_user:
            wait_for_user(
                f"Relation NETCONF failed with errors.\n"
                f"Please check the log: {rel_log_name}\n\n"
                f"Click OK to continue."
            )
        return False, all_output

    return True, all_output


def _run_relation_zip(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    remote_dir: str,
    zip_filename: str,
    relation_local_path: str,
    log_dir: str,
    log_cb: Callable[[str], None],
    all_output: str,
    wait_for_user: Optional[Callable[[str], bool]] = None,
    ui_cb: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """Unzip relation zip, find node folder, run each .txt file."""
    if ui_cb is None:
        ui_cb = log_cb

    # ── 2. Unzip on server ───────────────────────────────────────
    log_cb(f"Unzipping {zip_filename} on server...")
    out = ssh.run_amos_command_safe(
        f'!unzip -o "{remote_dir}/{zip_filename}" -d "{remote_dir}"',
        node_name, timeout=120,
    )
    all_output += out
    log_cb(f"Unzip output:\n{out}")

    # Sanitize: rename any extracted subfolders that contain spaces,
    # because moshell's `l+`/`run` commands don't support quoted paths.
    rename_cmd = (
        f'!cd "{remote_dir}" && '
        f'find . -depth -name "* *" | '
        f'while IFS= read -r p; do '
        f'np="$(echo "$p" | tr " " "_")"; '
        f'[ "$p" != "$np" ] && mv "$p" "$np"; '
        f'done; echo DONE_RENAME'
    )
    out = ssh.run_amos_command_safe(rename_cmd, node_name, timeout=60)
    all_output += out
    log_cb("Sanitized any paths containing spaces.")

    # ── 3. Find folder matching node_name ────────────────────────
    log_cb(f"Looking for folder matching '{node_name}'...")
    out = ssh.run_amos_command_safe(
        f"!find \"{remote_dir}\" -maxdepth 3 -type d -name \"*{node_name}*\"",
        node_name, timeout=30,
    )
    all_output += out
    log_cb(f"Find output:\n{out}")

    # Parse: get the first directory that matches
    node_folder = None
    for line in out.strip().split("\n"):
        line = line.strip()
        if node_name in line and line.startswith("/"):
            node_folder = line
            break

    if not node_folder:
        msg = (
            f"No folder matching '{node_name}' found in the extracted relation files.\n"
            f"Searched in: {remote_dir}"
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck the zip contents and try again."
            )
            if not retry:
                return False, all_output
            # Re-search
            out = ssh.run_amos_command_safe(
                f"!find \"{remote_dir}\" -maxdepth 3 -type d -name \"*{node_name}*\"",
                node_name, timeout=30,
            )
            all_output += out
            for line in out.strip().split("\n"):
                line = line.strip()
                if node_name in line and line.startswith("/"):
                    node_folder = line
                    break
            if not node_folder:
                log_cb("✗ Still no folder found.")
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ Node folder found: {node_folder}")

    # ── 4. List all .txt files ───────────────────────────────────
    log_cb(f"Listing relation files in {node_folder}...")
    out = ssh.run_amos_command_safe(
        f'!ls -1N "{node_folder}"/*.txt 2>/dev/null',
        node_name, timeout=15,
    )
    all_output += out

    txt_files = []
    for line in out.strip().split("\n"):
        line = line.strip()
        # ls may wrap paths containing spaces with single or double quotes
        if (len(line) >= 2 and line[0] in ("'", '"') and line[-1] == line[0]):
            line = line[1:-1]
        if line.endswith(".txt") and line.startswith("/"):
            txt_files.append(line)

    if not txt_files:
        msg = f"No .txt relation files found in {node_folder}"
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"Found {len(txt_files)} relation file(s) to run.")

    # Production path: execute and confirm one file at a time, persisting a
    # small journal after every file. The legacy single-batch implementation
    # remains below as an emergency config fallback.
    if _CFG.get("relation_journal_enabled", True):
        return _run_relation_files_resumable(
            ssh, node_name, shortcode, relation_local_path=relation_local_path,
            remote_dir=remote_dir, txt_files=txt_files, log_dir=log_dir,
            log_cb=log_cb, all_output=all_output,
            wait_for_user=wait_for_user, ui_cb=ui_cb,
        )

    # ── 5. Generate moshell batch script via bash, then `run` it once ──
    # Produces one `l+ <file>.log / run <file> / l-` block per .txt file.
    # Much faster than Python-side per-file loop and gives isolated logs.
    safe_node = node_name.replace("/", "_")
    batch_script = f"{remote_dir}/run_relation_{safe_node}.mos"
    log_cb(f"Generating batch moshell script: {batch_script}")
    # l+/l- wraps each `run` so the server also produces a per-file .log
    # file, which is later downloaded into LOG/{SHORTCODE}/MOSHELL/
    gen_cmd = (
        f'!for f in "{node_folder}"/*.txt; do '
        f'echo "l+ $f.log"; echo "run $f"; echo "l-"; '
        f'done > "{batch_script}"'
    )
    # Clear any stale server-side .log files from previous runs
    ssh.run_amos_command_safe(
        f'!rm -f "{node_folder}"/*.log', node_name, timeout=15,
    )
    out = ssh.run_amos_command_safe(gen_cmd, node_name, timeout=30)
    all_output += out

    # Verify script has content
    out = ssh.run_amos_command_safe(
        f'!wc -l "{batch_script}"', node_name, timeout=15,
    )
    all_output += out

    # Run the whole batch in one moshell call; capture full live terminal
    # output (includes NODE> prompts, [Proxy ID = ...] lines, crn blocks).
    #
    # Critical: use ``run_amos_blocking_with_sentinel`` — the batch script
    # we generate chains ``l+``, ``run <file>``, ``l-`` for every relation
    # file, and each of those can cause moshell to print a transient
    # ``NODE>`` prompt that ``_read_until_amos`` would wrongly accept as
    # "done". The sentinel (``!echo __TRFS_DONE_<nonce>__``) is queued
    # immediately after the ``run`` command so it only prints once the
    # batch actually finished — i.e. we never mark relation OK early.
    log_cb(f"Running all {len(txt_files)} relation files in one batch...")
    # Announce the list up-front in the UI so the operator sees what's
    # queued, even before per-script live progress starts.
    ui_cb(f"Running {len(txt_files)} relation script(s) in one batch:")
    for tp in sorted(txt_files):
        ui_cb(f"  - {os.path.basename(tp)}")
    batch_timeout = max(3600, len(txt_files) * 300)
    log_cb(
        f"(sentinel + 10s idle quiescence; timeout={batch_timeout}s, "
        "heartbeat every 60s)"
    )

    # ── Live per-script progress ─────────────────────────────────
    # moshell echoes a ``run /path/NN_<name>.txt`` line as it starts
    # each script in the batch. We detect those in the streaming
    # output and emit one UI line per script — "running script
    # NN_<name>.txt" — so the operator sees real-time progress with
    # the node tag (ui_cb is already node-tagged by the caller).
    _seen_scripts: set = set()
    _line_tail = [""]
    _run_line_re = re.compile(r"run\s+\S*/(\d*_?[^/\s]+\.txt)\b")

    def _relation_activity(text: str) -> None:
        combined = _line_tail[0] + text
        parts = combined.split("\n")
        _line_tail[0] = parts[-1]
        for ln in parts[:-1]:
            m = _run_line_re.search(ln)
            if m:
                fname = m.group(1)
                if fname not in _seen_scripts:
                    _seen_scripts.add(fname)
                    ui_cb(
                        f"running script "
                        f"[{len(_seen_scripts)}/{len(txt_files)}] {fname}"
                    )

    batch_out = ssh.run_amos_blocking_with_sentinel(
        f"run {batch_script}", node_name, timeout=batch_timeout,
        quiet_after=10.0,
        on_activity=_relation_activity,
    )
    all_output += batch_out
    log_cb(f"Batch run completed ({len(batch_out)} bytes of live output).")
    ui_cb(
        f"Relation batch finished — {len(_seen_scripts)}/{len(txt_files)} "
        "script(s) executed."
    )

    # ── Sanity check: did any of the relation scripts ACTUALLY run? ──
    # Without this, an empty batch_out (because ``run <batch>``
    # failed: bad path, moshell couldn't open it, all scripts errored
    # at the load stage, etc.) would still flow through the parser
    # and produce "0 errors, 0 failures" → step shows as success even
    # though zero scripts ran. The signal we trust is the count of
    # ``run /path/<file>.txt`` echo lines that moshell prints when
    # it starts executing each script — one per file in the batch.
    run_marker_count = 0
    for tp in txt_files:
        # Each l+/run/l- block in the batch produces at least one
        # ``> run /path/<file>.txt`` echo. Tolerate prompt prefix +
        # whitespace variations; tp is an absolute Unix path.
        pat = re.compile(
            r"(?m)^(?:.*?>\s*)?run\s+" + re.escape(tp) + r"\s*$"
        )
        if pat.search(batch_out):
            run_marker_count += 1

    if run_marker_count == 0:
        msg = (
            f"Relation batch produced no 'run <file>.txt' markers — "
            f"none of the {len(txt_files)} script(s) actually executed. "
            f"This usually means moshell failed to open the batch "
            f"script ({batch_script}) or every file errored at load. "
            f"Batch output tail:\n{batch_out[-600:]}"
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"Relation step FAILED — no scripts actually ran for "
                f"{node_name}.\n\n"
                f"Check the batch output, fix the issue (path / "
                f"permissions / corrupt zip / wrong node folder) and "
                f"click Retry to re-run, or Stop to abort."
            )
            if not retry:
                return False, all_output
            # Retry the batch run once
            log_cb("Re-running batch script after operator confirmation…")
            batch_out2 = ssh.run_amos_blocking_with_sentinel(
                f"run {batch_script}", node_name, timeout=batch_timeout,
                quiet_after=10.0,
            )
            all_output += batch_out2
            batch_out = batch_out2  # use re-run output downstream
            run_marker_count = 0
            for tp in txt_files:
                pat = re.compile(
                    r"(?m)^(?:.*?>\s*)?run\s+" + re.escape(tp) + r"\s*$"
                )
                if pat.search(batch_out):
                    run_marker_count += 1
            if run_marker_count == 0:
                log_cb(
                    "✗ Re-run also produced 0 run markers — aborting."
                )
                return False, all_output
        else:
            return False, all_output

    log_cb(
        f"Sanity check OK: {run_marker_count}/{len(txt_files)} "
        f"scripts started executing (counted 'run <file>.txt' markers)."
    )

    # Register each server-side per-file .log for later download to
    # MOSHELL/RELATION/ (isolated so other steps' logs don't mix in).
    for tp in sorted(txt_files):
        ssh.register_remote_log(f"{tp}.log", subfolder="RELATION")
    # Also register the batch script itself for reference
    ssh.register_remote_log(batch_script, subfolder="RELATION")

    # Save the raw combined live output as a single session log
    combined_path = os.path.join(
        log_dir, f"RELATION_{node_name}_FULL_SESSION.txt"
    )
    try:
        with open(combined_path, "w", encoding="utf-8") as f:
            f.write(batch_out)
        log_cb(f"Full session log saved: {os.path.basename(combined_path)}")
    except Exception as exc:
        log_cb(f"Failed to save full session log: {exc}")

    # ── 6. Build per-file logs ──────────────────────────────────
    # Source of truth is the **server-side ``l+ <file>.log``** that moshell
    # produces around each ``run <file>`` in the batch — one .log per .txt,
    # isolated by moshell itself. We SFTP-download those directly.
    #
    # We previously split the combined session output via regex on
    # ``run <path>`` markers, but that was unreliable: when moshell's
    # echo of the command didn't exactly match (ANSI noise, wrapped line,
    # different prompt prefix) the marker missed and the fallback
    # ``setdefault(tp, batch_out)`` assigned the *entire* batch text to
    # that file — which is why script #11's log sometimes showed script
    # #04's content. Downloading the per-file ``.log`` eliminates that.
    sorted_txt_files = sorted(txt_files)
    file_sections: dict[str, str] = {}

    log_cb(
        f"Downloading {len(sorted_txt_files)} per-file server log(s) "
        "(l+/l- isolates each run)..."
    )
    missing_on_server: list[str] = []
    for tp in sorted_txt_files:
        remote_log = f"{tp}.log"
        local_tmp = os.path.join(
            log_dir, f"_tmp_{os.path.basename(remote_log)}"
        )
        try:
            ssh.sftp_download(remote_log, local_tmp)
            with open(local_tmp, "r", encoding="utf-8", errors="replace") as fr:
                file_sections[tp] = fr.read()
            try:
                os.remove(local_tmp)
            except Exception:
                pass
        except Exception as exc:
            log_cb(
                f"  ! No server-side log for {os.path.basename(tp)} "
                f"({exc}); will try session split."
            )
            missing_on_server.append(tp)

    # Fallback for files whose server-side .log didn't exist (e.g. moshell
    # crashed mid-batch, filesystem error). Use a TIGHT per-file regex
    # split of the combined batch output — never a full-batch fallback,
    # which is what caused cross-file contamination before.
    if missing_on_server:
        import re as _re
        markers = []
        for tp in sorted_txt_files:
            pattern = _re.compile(
                r"(?m)^(?:.*?>\s*)?run\s+" + _re.escape(tp) + r"\s*$"
            )
            m = pattern.search(batch_out)
            markers.append((m.start() if m else -1, tp))
        found = sorted([(pos, tp) for pos, tp in markers if pos >= 0])
        for i, (pos, tp) in enumerate(found):
            end = found[i + 1][0] if i + 1 < len(found) else len(batch_out)
            if tp in missing_on_server:
                file_sections[tp] = batch_out[pos:end]
        # Any file still missing → explicit placeholder, NOT full batch.
        for tp in sorted_txt_files:
            if tp not in file_sections:
                file_sections[tp] = (
                    f"[NO LOG AVAILABLE]\n"
                    f"Neither the server-side l+/l- log ({tp}.log) nor a\n"
                    f"'run {tp}' marker was found in the batch session output.\n"
                    f"This usually means moshell skipped this file or the\n"
                    f"batch terminated before reaching it.\n"
                )

    errors_summary: list[str] = []
    file_summaries: list[str] = []
    for i, txt_path in enumerate(sorted_txt_files, 1):
        txt_name = os.path.basename(txt_path)
        local_name = f"RELATION_{node_name}_{txt_name.replace('.txt', '')}.txt"
        local_path = os.path.join(log_dir, local_name)

        file_output = file_sections.get(txt_path, "")
        source = (
            "server l+ log"
            if txt_path not in missing_on_server
            else "session split (fallback)"
        )
        log_cb(
            f"[{i}/{len(sorted_txt_files)}] Saving log for {txt_name} "
            f"[{source}]..."
        )
        try:
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(file_output)
        except Exception as exc:
            log_cb(f"  ! Failed to write {local_name}: {exc}")

        parsed = _parse_relation_output(file_output, txt_name)
        file_summary = f"{txt_name.replace('.txt', '')}  {parsed['summary_line']}"
        file_summaries.append(file_summary)
        log_cb(f"  → {parsed['summary_line']}")

        if parsed["has_issues"]:
            errors_summary.append(file_summary)

        # Append summary footer to the downloaded log
        try:
            with open(local_path, "a", encoding="utf-8") as f:
                f.write("\n\n" + "=" * 72 + "\n")
                f.write("SUMMARY\n")
                f.write("=" * 72 + "\n")
                f.write(f"File: {txt_name}\n")
                f.write(f"Path: {txt_path}\n")
                f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"{parsed['summary_line']}\n")
                if parsed["has_issues"]:
                    f.write(f"\nStatus: NEEDS REVIEW\n")
                    if parsed["error_lines"]:
                        f.write(f"\nErrors found:\n")
                        f.write("-" * 40 + "\n")
                        for err_line in parsed["error_lines"]:
                            f.write(f"  {err_line}\n")
                else:
                    f.write(f"\nStatus: OK\n")
        except Exception as exc:
            log_cb(f"  ! Failed to append summary to {local_name}: {exc}")

    # ── Overall summary ─────────────────────────────────────────
    summary_log_path = os.path.join(log_dir, f"RELATION_{node_name}_SUMMARY.txt")
    try:
        with open(summary_log_path, "w", encoding="utf-8") as f:
            f.write(f"Relation Summary — {node_name}\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total files: {len(txt_files)}\n")
            f.write("=" * 90 + "\n\n")
            f.write(f"{'File':<55s} {'Summary'}\n")
            f.write("-" * 90 + "\n")
            for s in file_summaries:
                f.write(f"{s}\n")
            f.write("\n" + "=" * 90 + "\n")
            if errors_summary:
                f.write(f"\nFILES WITH ISSUES ({len(errors_summary)}):\n")
                f.write("-" * 90 + "\n")
                for s in errors_summary:
                    f.write(f"  {s}\n")
            else:
                f.write("\nAll files OK — no errors detected.\n")
        log_cb(f"Summary saved: RELATION_{node_name}_SUMMARY.txt")
    except Exception as exc:
        log_cb(f"Failed to save summary: {exc}")

    if errors_summary:
        summary_text = "\n".join(errors_summary)
        log_cb(
            f"⚠ Relation completed with {len(errors_summary)} file(s) "
            f"having issues:\n{summary_text}"
        )
        all_output += f"\n[RELATION SUMMARY — ISSUES]\n{summary_text}\n"
    else:
        log_cb(
            f"✓ All {len(txt_files)} relation file(s) executed OK for "
            f"{node_name}."
        )

    # Relation completes even with errors — user reviews logs later
    return True, all_output


def _relation_sentinel_complete(output: str) -> bool:
    """The blocking runner only declares completion after this bare nonce."""
    return bool(re.search(
        r"(?m)^\s*__TRFS_DONE_[0-9a-fA-F]{8}__\s*$", output or "",
    ))


def _relation_remote_log(
    ssh: IntegrationSSH,
    remote_path: str,
    local_path: str,
    fallback: str,
    log_cb: Callable[[str], None],
) -> tuple[str, str]:
    """Download one l+/l- log; use only its own session output as fallback."""
    try:
        ssh.sftp_download(remote_path, local_path)
        with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), "server l+ log"
    except Exception as exc:
        log_cb(f"  ! Could not download {remote_path}: {exc}; using this "
               f"file's live session output.")
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(fallback or "")
        return fallback or "", "per-file session fallback"


def _append_relation_log_summary(local_path: str, txt_name: str,
                                 remote_path: str, parsed: dict) -> None:
    with open(local_path, "a", encoding="utf-8") as fh:
        fh.write("\n\n" + "=" * 72 + "\nSUMMARY\n" + "=" * 72 + "\n")
        fh.write(f"File: {txt_name}\nPath: {remote_path}\n")
        fh.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write(f"{parsed['summary_line']}\n")
        fh.write("\nStatus: " + (
            "NEEDS REVIEW\n" if parsed["has_issues"] else "OK\n"
        ))
        if parsed["has_issues"] and parsed.get("error_lines"):
            fh.write("\nErrors found:\n" + "-" * 40 + "\n")
            for line in parsed["error_lines"]:
                fh.write(f"  {line}\n")


def _run_relation_files_resumable(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    relation_local_path: str,
    remote_dir: str,
    txt_files: list[str],
    log_dir: str,
    log_cb: Callable[[str], None],
    all_output: str,
    wait_for_user: Optional[Callable[[str], bool]] = None,
    ui_cb: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """Run relation files sequentially with durable per-file completion.

    A local atomic journal is authoritative for already verified files. A
    remote marker closes the small crash window between Moshell completion and
    the local journal write. A file that has neither proof is AMBIGUOUS and is
    never silently skipped or blindly replayed.
    """
    if ui_cb is None:
        ui_cb = log_cb
    sorted_files = sorted(txt_files)
    journal_path, journal, resumed = relation_journal.open_or_create(
        log_dir, node_name, shortcode, relation_local_path,
        sorted_files, remote_dir,
    )
    progress_path = journal["remote_progress_path"]
    if resumed:
        done_count = sum(
            1 for item in journal["items"]
            if item["status"] in ("VERIFIED_OK", "VERIFIED_WITH_ISSUES")
        )
        msg = (f"Relation recovery journal found: {done_count}/"
               f"{len(sorted_files)} file(s) already verified.")
        log_cb(msg)
        ui_cb(msg)
    else:
        ssh.run_amos_command_safe(
            f'!rm -f "{progress_path}" && touch "{progress_path}"',
            node_name, timeout=15,
        )
        log_cb(f"Relation journal created: {journal_path}")

    try:
        progress_out = ssh.run_amos_command_safe(
            f'!cat "{progress_path}" 2>/dev/null', node_name, timeout=15,
        )
    except Exception:
        progress_out = ""
    remote_done = set(re.findall(
        r"(?m)^DONE\s+([0-9a-f]{16})\s*$", progress_out,
    ))
    ssh.register_remote_log(progress_path, subfolder="RELATION")

    full_session_path = os.path.join(
        log_dir, f"RELATION_{node_name}_FULL_SESSION.txt",
    )
    file_summaries = []
    errors_summary = []

    for item in journal["items"]:
        index = int(item["index"])
        txt_name = item["file"]
        txt_path = item["remote_path"]
        marker = item["marker"]
        remote_log = f"{txt_path}.log"
        local_name = f"RELATION_{node_name}_{txt_name[:-4]}.txt"
        local_path = os.path.join(log_dir, local_name)
        ssh.register_remote_log(remote_log, subfolder="RELATION")

        if item["status"] in ("VERIFIED_OK", "VERIFIED_WITH_ISSUES"):
            ui_cb(f"skipping verified script [{index}/{len(sorted_files)}] "
                  f"{txt_name}")
            summary = item.get("summary", "previously verified")
            file_summaries.append(f"{txt_name[:-4]}  {summary}")
            if item["status"] == "VERIFIED_WITH_ISSUES":
                errors_summary.append(f"{txt_name[:-4]}  {summary}")
            continue

        # The remote marker proves the file returned to Moshell before a prior
        # process died. Rebuild the local result from its isolated server log.
        if marker in remote_done:
            file_output, source = _relation_remote_log(
                ssh, remote_log, local_path, "", log_cb,
            )
            if file_output:
                parsed = _parse_relation_output(file_output, txt_name)
                try:
                    _append_relation_log_summary(
                        local_path, txt_name, txt_path, parsed,
                    )
                except Exception as exc:
                    log_cb(f"  ! Could not append relation summary: {exc}")
                item["status"] = (
                    "VERIFIED_WITH_ISSUES" if parsed["has_issues"]
                    else "VERIFIED_OK"
                )
                item["summary"] = parsed["summary_line"]
                item["local_log"] = local_path
                item["completed_at"] = relation_journal.now_iso()
                relation_journal.save(journal_path, journal)
                ui_cb(f"reconciled script [{index}/{len(sorted_files)}] "
                      f"{txt_name} from {source}")
                summary = f"{txt_name[:-4]}  {parsed['summary_line']}"
                file_summaries.append(summary)
                if parsed["has_issues"]:
                    errors_summary.append(summary)
                continue

        if item["status"] in ("RUNNING", "AMBIGUOUS"):
            item["status"] = "AMBIGUOUS"
            relation_journal.save(journal_path, journal)
            msg = (
                f"Relation file {index}/{len(sorted_files)} {txt_name} was "
                "interrupted and has no durable completion marker. Re-run "
                "this file only?"
            )
            log_cb(f"⚠ {msg}")
            if not wait_for_user or not wait_for_user(msg):
                journal["status"] = "INTERRUPTED"
                relation_journal.save(journal_path, journal)
                return False, all_output

        while True:
            item["status"] = "RUNNING"
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["started_at"] = relation_journal.now_iso()
            item["completed_at"] = ""
            relation_journal.save(journal_path, journal)
            ui_cb(f"running script [{index}/{len(sorted_files)}] {txt_name}")
            log_cb(f"[{index}/{len(sorted_files)}] Running {txt_path}")

            try:
                ssh.run_amos_command_safe(
                    f'!rm -f "{remote_log}"', node_name, timeout=15,
                )
                ssh.run_amos_command_safe(
                    f"l+ {remote_log}", node_name, timeout=30,
                )
                file_output = ssh.run_amos_blocking_with_sentinel(
                    f"run {txt_path}", node_name,
                    timeout=int(_CFG.get("relation_file_timeout_s", 3600)),
                    quiet_after=float(_CFG.get("relation_file_quiet_s", 3.0)),
                )
            except Exception as exc:
                file_output = f"[RUNNER ERROR] {type(exc).__name__}: {exc}\n"
            finally:
                try:
                    ssh.run_amos_command_safe("l-", node_name, timeout=30)
                except Exception:
                    pass

            all_output += f"\n--- {txt_name} ---\n{file_output}"
            try:
                with open(full_session_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n\n--- {txt_name} attempt {item['attempts']} ---\n")
                    fh.write(file_output)
            except Exception as exc:
                log_cb(f"Could not append full relation session log: {exc}")

            if not _relation_sentinel_complete(file_output):
                item["status"] = "AMBIGUOUS"
                item["summary"] = "completion sentinel not observed"
                relation_journal.save(journal_path, journal)
                msg = (
                    f"{txt_name} did not produce a completion sentinel. Its "
                    "state is AMBIGUOUS; retry this file only?"
                )
                log_cb(f"⚠ {msg}")
                if wait_for_user and wait_for_user(msg):
                    continue
                journal["status"] = "INTERRUPTED"
                relation_journal.save(journal_path, journal)
                return False, all_output

            # Persist a gateway-side completion marker before updating the
            # local journal. If the desktop dies in between, the next run can
            # still reconcile this exact file from its l+ log.
            marker_out = ssh.run_amos_command_safe(
                f'!printf "DONE {marker}\\n" >> "{progress_path}"',
                node_name, timeout=15,
            )
            all_output += marker_out
            remote_done.add(marker)

            parsed_source, source = _relation_remote_log(
                ssh, remote_log, local_path, file_output, log_cb,
            )
            parsed = _parse_relation_output(parsed_source, txt_name)
            execution_error = bool(re.search(
                r"No such file|cannot open|failed to open|Unknown command",
                file_output, re.IGNORECASE,
            ))
            if execution_error and not parsed["has_issues"]:
                parsed["has_issues"] = True
                parsed["summary_line"] += "  EXECUTION ERROR"
            try:
                _append_relation_log_summary(
                    local_path, txt_name, txt_path, parsed,
                )
            except Exception as exc:
                log_cb(f"  ! Could not append relation summary: {exc}")

            item["status"] = (
                "VERIFIED_WITH_ISSUES" if parsed["has_issues"]
                else "VERIFIED_OK"
            )
            item["summary"] = parsed["summary_line"]
            item["local_log"] = local_path
            item["completed_at"] = relation_journal.now_iso()
            relation_journal.save(journal_path, journal)
            summary = f"{txt_name[:-4]}  {parsed['summary_line']}"
            file_summaries.append(summary)
            if parsed["has_issues"]:
                errors_summary.append(summary)
            log_cb(f"  → {parsed['summary_line']} [{source}]")
            break

    journal["status"] = "COMPLETED"
    journal["completed_at"] = relation_journal.now_iso()
    relation_journal.save(journal_path, journal)

    summary_log_path = os.path.join(log_dir, f"RELATION_{node_name}_SUMMARY.txt")
    try:
        with open(summary_log_path, "w", encoding="utf-8") as fh:
            fh.write(f"Relation Summary — {node_name}\n")
            fh.write(f"Run ID: {journal['run_id']}\n")
            fh.write(f"Input SHA-256: {journal['input_sha256']}\n")
            fh.write(f"Total files: {len(sorted_files)}\n")
            fh.write("=" * 90 + "\n\n")
            for summary in file_summaries:
                fh.write(summary + "\n")
            if errors_summary:
                fh.write("\nFILES WITH ISSUES:\n")
                for summary in errors_summary:
                    fh.write("  " + summary + "\n")
            else:
                fh.write("\nAll files OK — no errors detected.\n")
    except Exception as exc:
        log_cb(f"Failed to save relation summary: {exc}")

    ui_cb(f"Relation finished — {len(sorted_files)}/{len(sorted_files)} "
          f"file(s) verified; journal complete.")
    if errors_summary:
        log_cb(f"⚠ Relation completed with {len(errors_summary)} file(s) "
               "requiring log review.")
    else:
        log_cb(f"✓ All {len(sorted_files)} relation file(s) executed OK for "
               f"{node_name}.")
    return True, all_output


# ── Verify MME step ──────────────────────────────────────────────
def _parse_ping_test_output(output: str) -> dict:
    """Parse a ``Ping_Test_Cmd.txt`` (or any ``PING <ip>`` block) output
    and categorise each unique target into one of four states:

      - ``ok``      → 0 % packet loss, all replies received
      - ``partial`` → between 1 % and 99 % packet loss (some replies lost)
      - ``failed``  → 100 % packet loss, ``Destination Host Unreachable``,
                      ``Network is unreachable`` — no reply at all
      - ``no_route``→ ``ping: $VAR: Name or service not known`` (the
                      script's pre-resolution variable wasn't populated
                      — a node config issue, NOT a network failure)

    Returns:
        {
          "results": [(ip, state, loss_pct, tx, rx), …],
          "n_ok": int, "n_partial": int, "n_failed": int,
          "n_no_route": int,
        }
    """
    lines = output.split("\n")
    results: list[tuple[str, str, int, int, int]] = []

    ping_re = re.compile(
        r"^\s*PING\s+(\d+\.\d+\.\d+\.\d+)\s*\(",
        re.IGNORECASE,
    )
    stats_re = re.compile(
        r"(\d+)\s+packets\s+transmitted,\s*(\d+)\s+received"
        r"(?:,\s*\+?\d+\s+errors)?,\s*(\d+)%\s+packet\s+loss",
    )
    var_not_set_re = re.compile(r"ping:\s+\$\S+:\s+Name or service not known")
    no_route_re = re.compile(r"ping:.*Name or service not known")

    current_ip: Optional[str] = None
    no_route = 0
    for line in lines:
        m = ping_re.search(line)
        if m:
            current_ip = m.group(1)
            continue
        if var_not_set_re.search(line) or (
            current_ip is None and no_route_re.search(line)
        ):
            no_route += 1
            current_ip = None
            continue
        if current_ip:
            if ("Destination Host Unreachable" in line
                    or "Network is unreachable" in line):
                results.append((current_ip, "failed", 100, 0, 0))
                current_ip = None
                continue
            sm = stats_re.search(line)
            if sm:
                tx, rx, loss = int(sm.group(1)), int(sm.group(2)), int(sm.group(3))
                if loss <= 0 and rx == tx:
                    state = "ok"
                elif loss >= 100 or rx == 0:
                    state = "failed"
                else:
                    state = "partial"
                results.append((current_ip, state, loss, tx, rx))
                current_ip = None

    counts = {"ok": 0, "partial": 0, "failed": 0}
    for _, st, _, _, _ in results:
        if st in counts:
            counts[st] += 1
    return {
        "results": results,
        "n_ok": counts["ok"],
        "n_partial": counts["partial"],
        "n_failed": counts["failed"],
        "n_no_route": no_route,
    }


def _ping_overall_status(parsed: dict) -> tuple[str, str, str]:
    """Map the per-IP counts to one of the four user-facing buckets.

    Returns ``(status_key, short_detail, long_message)``:
      - status_key      → "all_ok" | "partial_loss" | "some_failed" |
                          "all_failed" | "no_data"
      - short_detail    → cell label for the progress / summary column
      - long_message    → fuller sentence for the operator's log
    """
    n_ok = parsed["n_ok"]
    n_partial = parsed["n_partial"]
    n_failed = parsed["n_failed"]
    n_total = n_ok + n_partial + n_failed

    if n_total == 0:
        return ("no_data",
                "No ping data",
                "Ping test produced no parseable results (the script may "
                "have errored before any ping ran).")

    if n_failed == 0 and n_partial == 0:
        return ("all_ok",
                f"All {n_ok} pings OK",
                f"All {n_ok} target(s) reachable with 0 % packet loss.")

    if n_failed == 0 and n_partial > 0:
        return ("partial_loss",
                f"{n_partial}/{n_total} with packet loss",
                f"All {n_total} target(s) reachable, but {n_partial} "
                "showed partial packet loss — review per-IP detail.")

    if n_failed > 0 and (n_ok > 0 or n_partial > 0):
        return ("some_failed",
                f"{n_failed}/{n_total} failed",
                f"{n_failed} of {n_total} target(s) failed (100 % loss); "
                f"{n_ok + n_partial} still reachable.")

    # n_failed == n_total
    return ("all_failed",
            f"All {n_total} pings failed",
            f"All {n_total} target(s) failed (100 % loss / unreachable).")


def run_sgw_check(
    ssh: "IntegrationSSH",
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    node_type: str = "lte_nr",
    gsm_on_primary: bool = False,
    gsm_ping_targets: Optional[list[str]] = None,
    bsc_name: Optional[str] = None,
) -> tuple[bool, str, str, bool]:
    """Transport reachability check.

    Scripts:
      * LTE/NR — ``/home/shared/ESETARI/INOC/SCRIPTS/DM/ping.txt``
        (backhaul + MME + SGW for both LTE and NR routers).
      * GSM    — ``/home/shared/common/INTEGRATION_TEAM/script/
        Ping_Test_BSC_brokerIP.txt`` (BSC broker IP reachability).

    What gets run:
      * Pure LTE/NR node       → LTE/NR script only.
      * Pure GSM node          → GSM script only.
      * Co-located LTE+GSM     → BOTH scripts back-to-back on the
        (``gsm_on_primary``)     same node. Results from both runs
                                 are merged into a single 4-level
                                 status verdict.

    Each script runs through ``run_amos_blocking_with_sentinel`` so
    the mid-script ``NODE>`` prompts don't mark completion early.
    Wrapped in ``l+ / l-``; the server-side log goes to ``MOSHELL/``.

    Broker IP validation (GSM only): when ``bsc_name`` is given and a GSM
    script ran, the ``$bscBrokerIp`` the node reported is compared against
    ``config.json → bsc_broker_map[bsc_name]``. A mismatch means the node
    is pointed at the WRONG BSC — ping alone can't catch that (the wrong
    broker still answers).

    Returns:
        ``(success: bool, full_output: str, detail: str, broker_wrong: bool)``
        where ``success`` is True for ``all_ok`` AND ``partial_loss``,
        False for ``some_failed`` / ``all_failed`` / ``no_data``.
        ``detail`` is the short cell label (carries the
        "Wrong IP Broker BSC" remark when ``broker_wrong``).
    """
    all_output = ""
    command_output = ""

    # ── Decide which scripts to run ──────────────────────────────
    # ``scripts`` is a list of ``(label, path)`` tuples. Order is
    # deterministic so the log reads naturally (LTE/NR before GSM in
    # co-located mode).
    scripts: list[tuple[str, str]] = []
    if node_type == "gsm":
        scripts.append(("GSM", _PING_TEST_GSM))
    else:
        scripts.append(("LTE/NR", _PING_TEST_LTE_NR))
        if gsm_on_primary:
            # Single physical node carries both RATs — run the GSM
            # ping script too. ``Ping_Test_BSC_brokerIP.txt`` knows
            # how to talk to the BSC broker via the same node.
            scripts.append(("GSM (co-located)", _PING_TEST_GSM))

    remote_log = f"/home/shared/{ssh.username}/SGW_Check_{node_name}.log"

    log_cb(
        f"Running ping check(s) for {node_name} "
        f"({len(scripts)} script(s): "
        + ", ".join(lbl for lbl, _ in scripts) + ")"
    )
    ssh.run_amos_command_safe(f"!rm -f {remote_log}", node_name, timeout=15)
    ssh.run_amos_command_safe(f"l+ {remote_log}", node_name, timeout=15)

    for label, script_path in scripts:
        log_cb(f"── {label} ── $ run {script_path}")
        log_cb("(sentinel + 10s idle quiescence; usually 1-2 min)")
        out = ssh.run_amos_blocking_with_sentinel(
            f"run {script_path}",
            node_name, timeout=900, quiet_after=10.0,
        )
        log_cb(f"{label} output: {len(out)} bytes")
        all_output += out
        command_output += out

    ssh.run_amos_command_safe("l-", node_name, timeout=15)
    ssh.register_remote_log(remote_log)
    log_cb(f"Combined ping output captured ({len(command_output)} bytes).")

    # ── Parse + categorise ───────────────────────────────────────
    parsed = _parse_ping_test_output(command_output)
    status_key, short_detail, long_msg = _ping_overall_status(parsed)

    log_cb(
        f"Parsed: {parsed['n_ok']} OK, {parsed['n_partial']} partial-loss, "
        f"{parsed['n_failed']} failed, "
        f"{parsed['n_no_route']} no-route (script var not set)."
    )
    log_cb(long_msg)

    # Per-target lines for operator review
    summary_lines = [
        "[PING TEST SUMMARY]",
        (f"Status: {short_detail}  |  "
         f"OK={parsed['n_ok']}  "
         f"Partial={parsed['n_partial']}  "
         f"Failed={parsed['n_failed']}  "
         f"NoRoute={parsed['n_no_route']}"),
        "-" * 64,
    ]
    for ip, st, loss, tx, rx in parsed["results"]:
        if st == "ok":
            summary_lines.append(f"  {ip} > OK ({rx}/{tx} replies)")
        elif st == "partial":
            summary_lines.append(
                f"  {ip} > Packet loss {loss}% ({rx}/{tx} replies)"
            )
        else:
            summary_lines.append(f"  {ip} > FAILED ({loss}% loss)")
    summary_text = "\n".join(summary_lines)
    all_output += "\n" + summary_text + "\n"
    log_cb(summary_text)

    success = status_key in ("all_ok", "partial_loss")

    # ── BSC broker IP validation (GSM only) ──────────────────────
    # The GSM script reads the node's own AbisIp bscBrokerIpAddress and
    # pings it — so a node pointed at the WRONG BSC still pings fine.
    # Compare what the node reported against the expected IP for the
    # form's BSC (config.json bsc_broker_map).
    broker_wrong = False
    gsm_ran = any("GSM" in lbl for lbl, _ in scripts)
    if gsm_ran and (bsc_name or "").strip():
        bsc = bsc_name.strip().upper()
        # Primary: the script's own "$bscBrokerIp = <ip>" echo.
        found = re.findall(
            r"\$bscBrokerIp\s*=\s*(\d+\.\d+\.\d+\.\d+)", command_output)
        if not found:
            # Fallback: the `get AbisIp` table rows.
            found = re.findall(
                r"bscBrokerIpAddress\s+(\d+\.\d+\.\d+\.\d+)", command_output)
        found_ips = sorted(set(found))

        expected = _BSC_BROKER_MAP.get(bsc)
        if expected is None:
            broker_wrong = True
            short_detail = f"BSC {bsc} not in bsc_broker_map"
            log_cb(
                f"⚠ Broker IP check: BSC '{bsc}' is not defined in "
                "config.json bsc_broker_map — add it there. "
                f"Node reported: {', '.join(found_ips) or 'none'}"
            )
        elif not found_ips:
            log_cb(
                "⚠ Broker IP check: could not extract $bscBrokerIp from "
                f"the output — cannot verify against {expected} ({bsc}). "
                "Verdict unchanged."
            )
        elif any(ip != expected for ip in found_ips):
            broker_wrong = True
            got = ", ".join(found_ips)
            remark = f"Wrong IP Broker BSC ({got} ≠ {expected})"
            short_detail = (remark if success
                            else f"{short_detail} — {remark}")
            log_cb(
                f"✗ Broker IP check: expected {expected} ({bsc}) but the "
                f"node reports {got} → WRONG BSC broker."
            )
        else:
            log_cb(
                f"✓ Broker IP check: node broker {expected} matches "
                f"{bsc} (config.json)."
            )

    return success, all_output, short_detail, broker_wrong


def run_verify_mme(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Verify all MME connections are ENABLED using ``st mme``.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # Run st mme
    log_cb("Checking MME status (st mme)...")
    out = ssh.run_amos_command_safe("st mme", node_name, timeout=30)
    all_output += out
    log_cb(f"st mme output:\n{out}")

    # Parse: MME rows look like
    #   "2966  1 (UNLOCKED)  1 (ENABLED)   ENodeBFunction=1,TermPointToMme=S1-MME1"
    #   "22862              1 (ENABLED)   Transport=1,SctpEndpoint=S1_MME"
    # Every row MUST show "(ENABLED)" in the op-state column; any "(DISABLED)"
    # or missing "(ENABLED)" marker = failure.
    lines = out.split("\n")
    mme_lines = [l for l in lines if "TermPointToMme" in l or "SctpEndpoint" in l]

    if not mme_lines:
        msg = f"No MME entries found in 'st mme' output for {node_name}."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    def _bad_rows(rows):
        bad = []
        for l in rows:
            s = l.strip()
            if "(DISABLED)" in s or "(ENABLED)" not in s:
                bad.append(s)
        return bad

    disabled = _bad_rows(mme_lines)

    if disabled:
        disabled_list = "\n".join(disabled)
        msg = (
            f"MME is DISABLED, check the IP configuration or Transport.\n\n"
            f"Disabled entries:\n{disabled_list}"
        )
        log_cb(f"✗ {msg}")

        # Retry loop — user can fix and re-check
        while disabled:
            if not wait_for_user:
                return False, all_output
            retry = wait_for_user(
                f"{msg}\n\nFix the issue, then click Retry to re-check."
            )
            if not retry:
                log_cb("User chose to stop.")
                return False, all_output

            log_cb("Re-checking MME status...")
            out = ssh.run_amos_command_safe("st mme", node_name, timeout=30)
            all_output += out
            log_cb(f"Re-check st mme:\n{out}")

            mme_lines = [l for l in out.split("\n")
                         if "TermPointToMme" in l or "SctpEndpoint" in l]
            disabled = _bad_rows(mme_lines)
            if disabled:
                disabled_list = "\n".join(disabled)
                msg = (
                    f"MME is DISABLED, check the IP configuration or Transport.\n\n"
                    f"Disabled entries:\n{disabled_list}"
                )

    log_cb(f"✓ All MME connections ENABLED for {node_name} ({len(mme_lines)} entries).")
    return True, all_output


# ── Synchronization (GPS / PTP) check ───────────────────────────
def _sync_source_label(sync_type: Optional[str]) -> str:
    """Map a raw ``syncRefType`` to a short source label."""
    if not sync_type:
        return "Unknown"
    t = sync_type.upper()
    if "GNSS" in t or "GPS" in t:
        return "GPS"
    if "PTP" in t:
        return "PTP"
    if "SYNC_ETH" in t or "SYNCE" in t:
        return "SyncE"
    return sync_type   # fall back to the raw type for anything else


def _is_dashes(s: str) -> bool:
    s = s.strip()
    return len(s) >= 3 and set(s) <= set("-")


def _parse_sts(output: str) -> tuple[Optional[str], Optional[str]]:
    """Parse ``sts`` output → (radioClockState, syncRefType).

    radioClockState comes from the ``radioClockState : <STATE>`` line.

    syncRefType is read from the SyncReference table — bounded strictly
    to the rows between the two ``---`` rules right under the header (so
    we never bleed into a following ``DU/Port/sharedUnitRef`` table whose
    rows ALSO start with a digit). The row marked active with a leading
    ``*`` is preferred; otherwise the first data row.
        ``*1   1  GNSS_RECEIVER  NO_FAULT  GNSS  Synchronization=1,...``

    Some nodes have an EMPTY syncRefType table and sync from a radio unit
    instead (``nodeGroupRole`` / ``sharedUnitRef`` with an ``OK_ACTIVE``
    row) — that case is reported as source ``RRU``.
    """
    state = None
    m = re.search(r"radioClockState\s*:\s*([A-Za-z0-9_]+)", output)
    if m:
        state = m.group(1)

    lines = output.splitlines()
    header = None
    for i, l in enumerate(lines):
        if "syncRefType" in l and "Prio" in l:
            header = i
            break

    sync_type = None
    if header is not None:
        # Bound the table body to between the first and second '---'
        # rule after the header (the empty-table case has no rows there).
        region = lines[header + 1:]
        dash_idx = [i for i, l in enumerate(region) if _is_dashes(l)]
        if len(dash_idx) >= 2:
            body = region[dash_idx[0] + 1: dash_idx[1]]
        elif len(dash_idx) == 1:
            body = region[dash_idx[0] + 1:]
        else:
            body = region

        candidates: list[tuple[bool, str]] = []   # (is_active, type)
        for l in body:
            s = l.strip()
            if not s or _is_dashes(s):
                continue
            toks = s.split()
            # Prio token is like "1" or "*1"; syncRefType is the 3rd col.
            if len(toks) >= 3 and re.match(r"\*?\d+$", toks[0]):
                candidates.append((toks[0].startswith("*"), toks[2]))
        for is_active, t in candidates:
            if is_active:
                sync_type = t
                break
        if sync_type is None and candidates:
            sync_type = candidates[0][1]

    # Fallback: empty syncRefType table but the node syncs from a radio
    # unit (DU/RRU node sync) → label the source RRU.
    if sync_type is None and re.search(r"nodeGroupRole|sharedUnitRef",
                                       output):
        sync_type = "RRU"

    return state, sync_type


def run_sync_check(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str, str]:
    """Check node synchronization via the moshell ``sts`` command.

    OK when ``radioClockState`` contains "LOCKED" (RNT_TIME_LOCKED,
    TIME_OFFSET_LOCKED, FREQUENCY_LOCKED, …). The source label is
    derived from ``syncRefType`` (GNSS→GPS, PTP→PTP, …).

    Returns ``(ok, full_output, detail)`` where detail is the short
    string shown in the progress cell, e.g. ``"GPS - OK (RNT_TIME_LOCKED)"``.
    """
    log_cb("Checking synchronization (sts)...")
    out = ssh.run_amos_command_safe("sts", node_name, timeout=60)
    log_cb(f"sts output:\n{out}")

    state, sync_type = _parse_sts(out)
    source = _sync_source_label(sync_type)
    ok = bool(state) and "LOCKED" in state.upper()

    if state:
        detail = f"{source} - {'OK' if ok else 'Not OK'} ({state})"
    else:
        detail = f"{source} - Not OK (no radioClockState found)"

    log_cb(("✓ Synchronization " if ok else "✗ Synchronization ") + detail)
    return ok, out, detail


# ── SW level check ──────────────────────────────────────────────
def run_sw_check(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    expected: Optional[str] = None,
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str, str]:
    """Check the node's active Upgrade Package against the expected
    version from config.json (``uri_setting.upgrade_package_id``).

    Runs ``pr SystemFunctions=1,SwM=1,UpgradePackage=`` and reads the
    ``UpgradePackage=<version>`` shown. OK when it matches the expected
    id (e.g. ``CXP2010174/2-R42J13``).

    Returns ``(ok, full_output, detail)`` — detail e.g. ``"OK (CXP.../2-R42J13)"``
    or ``"Not OK (got R42H05, want R42J13)"``.
    """
    if expected is None:
        expected = _UPGRADE_PKG_ID
    expected = (expected or "").strip()

    log_cb(f"Checking SW level (expected UpgradePackage: {expected})...")
    out = ssh.run_amos_command_safe(
        "pr SystemFunctions=1,SwM=1,UpgradePackage=", node_name, timeout=60)
    log_cb(f"SW level output:\n{out}")

    # The echoed command ends with a bare 'UpgradePackage=' (no value);
    # only the result row(s) carry a real value → \S+ skips the empty one.
    found = [v for v in re.findall(r"UpgradePackage=(\S+)", out) if v]
    ok = expected in found
    if not found:
        detail = f"Not OK (no UpgradePackage found, want {expected})"
    elif ok:
        detail = f"OK ({expected})"
    else:
        detail = f"Not OK (got {', '.join(found)}, want {expected})"

    log_cb(("✓ SW level " if ok else "✗ SW level ") + detail)
    return ok, out, detail


# ── Backup CV step ──────────────────────────────────────────────
def run_backup_cv(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    backup_name: Optional[str] = None,
) -> tuple[bool, str]:
    """Create an SHM backup and wait until it succeeds.

    ``backup_name`` is optional so the integration workflow keeps its existing
    ``PreIntegration_*`` naming while other workflows (for example Cut Over)
    can create an explicitly labelled pre-change CV through the same proven
    SHM job/polling implementation.
    """
    all_output = ""
    backup_name = backup_name or f"PreIntegration_{time.strftime('%Y%m%d_%H%M')}"
    backup_cmd = (
        f'!python {CLI_PY} "shm backup --nodes {node_name} '
        f'--backupname {backup_name} --upload"'
    )

    def _extract_value(field: str, text: str) -> str:
        match = re.search(
            rf"^\s*{re.escape(field)}\s*:\s*(.+?)\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        return match.group(1).strip() if match else ""

    log_cb(
        f"Starting Backup CV for {node_name} "
        f"(backup name: {backup_name})..."
    )
    out = ssh.run_amos_command_safe(backup_cmd, node_name, timeout=180)
    all_output += out
    log_cb(f"Backup command output:\n{out}")

    job_name = ""
    for pattern in (
        r"job name:\s*([A-Za-z0-9_.-]+)",
        r"shm status --jobname\s+([A-Za-z0-9_.-]+)",
    ):
        match = re.search(pattern, out, re.IGNORECASE)
        if match:
            job_name = match.group(1).strip()
            break

    if not job_name:
        msg = f"Could not parse Backup CV job name for {node_name}."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck the backup command output and retry if needed."
            )
            if retry:
                return run_backup_cv(
                    ssh, node_name, log_cb, wait_for_user,
                    backup_name=backup_name,
                )
        return False, all_output

    log_cb(f"✓ Backup job started: {job_name}")

    status_cmd = f'!python {CLI_PY} "shm status --jobname {job_name}"'
    max_attempts = 90  # up to ~15 minutes at 10s intervals

    for attempt in range(1, max_attempts + 1):
        out = ssh.run_amos_command_safe(status_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Backup status check #{attempt}:\n{out}")

        status_value = _extract_value("Status", out).upper()
        result_value = _extract_value("Result", out).upper()

        if status_value == "COMPLETED" and result_value == "SUCCESS":
            log_cb(f"✓ Backup CV completed successfully (job: {job_name}).")
            return True, all_output

        if status_value in {"FAILED", "ABORTED", "CANCELLED"} or \
                result_value in {"FAILED", "FAILURE", "ERROR"}:
            msg = (
                f"Backup CV failed for {node_name} "
                f"(job: {job_name}, status: {status_value or 'UNKNOWN'}, "
                f"result: {result_value or 'UNKNOWN'})."
            )
            log_cb(f"✗ {msg}")
            if wait_for_user:
                retry = wait_for_user(
                    f"{msg}\n\nCheck the backup job and retry if needed."
                )
                if retry:
                    return run_backup_cv(
                        ssh, node_name, log_cb, wait_for_user,
                        backup_name=backup_name,
                    )
            return False, all_output

        if attempt < max_attempts:
            state_text = status_value or "IN PROGRESS"
            result_text = result_value or "PENDING"
            log_cb(
                f"Backup job {job_name} is {state_text} / {result_text} "
                f"({attempt}/{max_attempts}); waiting 10s..."
            )
            time.sleep(10)

    msg = (
        f"Backup CV did not reach COMPLETED/SUCCESS after "
        f"{max_attempts} checks (job: {job_name})."
    )
    log_cb(f"✗ {msg}")
    if wait_for_user:
        retry = wait_for_user(f"{msg}\n\nClick Retry to check again.")
        if retry:
            out = ssh.run_amos_command_safe(status_cmd, node_name, timeout=60)
            all_output += out
            status_value = _extract_value("Status", out).upper()
            result_value = _extract_value("Result", out).upper()
            if status_value == "COMPLETED" and result_value == "SUCCESS":
                log_cb(f"✓ Backup CV completed successfully (job: {job_name}).")
                return True, all_output
    return False, all_output


# ── Take Dump step ──────────────────────────────────────────────
def run_take_dump(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    local_dump_dir: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    local_filename: Optional[str] = None,
) -> tuple[bool, str]:
    """Run ``dcgk`` in AMOS, find the zip in the output path, download it locally.

    The ``dcgk`` command produces output like:
        dcg completed successfully, logs stored in /ericsson/log/amos/moshell_logfiles/USER/logs_moshell/dcg/NODE/TIMESTAMP

    We then list .zip files in that path and SFTP-download them to
    ``<local_dump_dir>/DUMP/<nodename>_modump.zip``.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # ── 1. Run dcgk ─────────────────────────────────────────────
    log_cb(f"Running dcgk for {node_name} (this may take a while)...")
    out = ssh.run_amos_command_safe("dcgk", node_name, timeout=900)
    all_output += out
    log_cb(f"dcgk output:\n{out}")

    # ── 2. Parse output for the log path ────────────────────────
    # ``dcgk`` output varies a bit between LTE/NR and GSM. Common
    # forms we've seen (case differs, path prefix differs):
    #   "dcg completed successfully, logs stored in /ericsson/log/..."
    #   "Logs stored in /home/shared/<user>/.../dcg/<node>/<ts>"
    #   "DCG completed; output: /opt/ericsson/.../dcg/<node>/..."
    # Strategy: scan every line for an absolute Unix path that
    # contains ``dcg`` somewhere (case-insensitive). That catches both
    # ``/ericsson/.../dcg/...`` and ``/home/.../dcg/...`` without
    # needing to hard-code the prefix.
    dcg_path = None
    for line in out.split("\n"):
        low = line.lower()
        if (
            "dcg completed" in low
            or "logs stored in" in low
            or "stored in" in low
            or "/dcg/" in low
        ):
            # Find the FIRST absolute path on this line
            idx = line.find("/")
            while idx != -1:
                # Path token = run until whitespace or end of line
                end = idx
                while end < len(line) and not line[end].isspace():
                    end += 1
                candidate = line[idx:end].strip().rstrip(".,;:)")
                if "/dcg/" in candidate.lower() and candidate.startswith("/"):
                    dcg_path = candidate
                    break
                idx = line.find("/", idx + 1)
            if dcg_path:
                break

    if not dcg_path:
        msg = f"Could not parse dcg output path from dcgk output for {node_name}."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck the output above and retry if needed."
            )
            if not retry:
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ DCG path: {dcg_path}")

    # ── 3. Find the zip file in the dcg path ────────────────────
    log_cb(f"Looking for zip file in {dcg_path}...")
    out = ssh.run_amos_command_safe(
        f"!ls -1 {dcg_path}/*.zip 2>/dev/null",
        node_name, timeout=15,
    )
    all_output += out

    zip_file = None
    for line in out.strip().split("\n"):
        line = line.strip()
        if line.endswith(".zip") and line.startswith("/"):
            zip_file = line
            break

    if not zip_file:
        msg = f"No .zip file found in {dcg_path}"
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"✓ Found zip: {zip_file}")

    # ── 4. Download zip to local DUMP folder ────────────────────
    local_dir = os.path.join(local_dump_dir, "DUMP")
    os.makedirs(local_dir, exist_ok=True)
    local_filename = local_filename or f"{node_name}_modump.zip"
    local_path = os.path.join(local_dir, local_filename)

    log_cb(f"Downloading {os.path.basename(zip_file)} → {local_path}...")
    try:
        ssh.sftp_download(zip_file, local_path)
        log_cb(f"✓ Dump saved: {local_path}")
        all_output += f"[SFTP] Downloaded → {local_path}\n"
    except Exception as exc:
        msg = f"SFTP download failed: {exc}"
        log_cb(f"✗ {msg}")
        all_output += f"[SFTP] {msg}\n"
        if wait_for_user:
            retry = wait_for_user(
                f"Download failed: {exc}\n\n"
                f"You can manually copy the file from:\n{zip_file}"
            )
            if not retry:
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ Take Dump completed for {node_name}.")
    return True, all_output


# ── GSM Cell Define in BSC step ─────────────────────────────────
def _shortcode_to_cell_id(shortcode: str) -> str:
    """Convert shortcode like MIN2790 to cell ID format M2790.

    Rule: take the first letter + the numeric part.
    E.g. MIN2790 → M2790, MIN149 → M149, MIN3884 → M3884
    """
    import re
    m = re.match(r"([A-Za-z])[A-Za-z]*(\d+)", shortcode)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return shortcode  # fallback: return as-is


def gsm_cell_id_re(shortcode: str):
    """Precise regex matching ONLY this site's GSM cell ids.

    A GSM cell id is ``<letter><siteDigits><band 8|9><sector…>`` — e.g. MIN18 →
    M189S1 (GSM900) / M188S1 (GSM1800). A bare prefix wildcard ``M18*`` also
    wrongly matches M1800.. (MIN1800), M18328.. (MIN1832) etc. Anchoring the
    band digit (8/9) AND requiring a NON-digit sector char right after the exact
    site digits excludes those foreign sites.
    """
    import re
    m = re.match(r"([A-Za-z])[A-Za-z]*(\d+)", shortcode or "")
    if not m:
        return None
    return re.compile(rf"^{m.group(1)}{m.group(2)}[89]\D", re.IGNORECASE)


def gsm_cells_in_output(output: str, shortcode: str) -> set:
    """Distinct GSM cell ids in a cmedit output that TRULY belong to the site
    (precise-regex filtered — see gsm_cell_id_re). Use this instead of cmedit's
    'N instance(s)' line, which counts prefix-wildcard false matches too."""
    import re
    rx = gsm_cell_id_re(shortcode)
    if not rx:
        return set()
    return {t for t in re.split(r"[\s,]+", output or "") if t and rx.match(t)}


def run_gsm_cell_define(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    bsc_name: Optional[str] = None,
    log_dir: Optional[str] = None,
) -> tuple[bool, str]:
    """Verify GSM Cell and MO are defined in BSC.

    Pre-step (NEW): if ``bsc_name`` is provided, set ``controllingBsc``
    on the node's NetworkElement FIRST. The GSM cell/MO lookups below
    rely on the node being linked to its BSC, so this is the natural
    place to guarantee the link exists — "every GSM check sets
    controllingBsc". Runs inside the current AMOS session via
    ``!python cli.py`` (``in_amos=True``).

    Two checks:
      1. MO check:  cmedit get * G31Tg.rsite==<SHORTCODE>* -t  → must be > 0 instances
      2. Cell check: cmedit get * gerancell.gerancellid==<MODIFIED_SHORTCODE>* -t  → must be > 0 instances

    The modified shortcode takes the first letter + digits, e.g. MIN2790 → M2790.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
    modified_sc = _shortcode_to_cell_id(shortcode)

    # ── 0. Pre-step: set controllingBsc (GSM link) ──────────────
    bsc = (bsc_name or "").strip()
    if bsc:
        log_cb(
            f"[GSM check] Ensuring controllingBsc link on {node_name} "
            f"→ NetworkElement={bsc} before cell/MO lookup."
        )
        set_ok, set_out = _set_controlling_bsc(
            ssh, node_name, bsc, log_cb,
            wait_for_user=wait_for_user,
            log_dir=log_dir,
            in_amos=True,  # gsm_cell_define runs inside an AMOS session
        )
        all_output += set_out + "\n"
        if set_ok:
            log_cb(f"✓ controllingBsc OK on {node_name} → NetworkElement={bsc}")
        else:
            # Not fatal to the cell/MO check itself — log loudly and
            # continue so the operator still gets the cell verdict.
            log_cb(
                f"⚠ controllingBsc could not be confirmed on {node_name} "
                f"→ NetworkElement={bsc}. Continuing with cell/MO check; "
                "BSC cell lookups may return 0 instances if the link is "
                "missing."
            )
    else:
        log_cb(
            f"[GSM check] No BSC name provided for {node_name} — "
            "skipping controllingBsc set (check the BSC Name field in "
            "the form if this node needs the BSC link)."
        )

    # ── 1. Check MO (G31Tg.rsite) ──────────────────────────────
    mo_cmd = f'!python {CLI_PY} "cmedit get * G31Tg.rsite=={shortcode}* -t"'
    log_cb(f"Checking GSM MO in BSC (rsite=={shortcode}*)...")
    out = ssh.run_amos_command_safe(mo_cmd, node_name, timeout=60)
    all_output += out
    log_cb(f"MO check output:\n{out}")

    mo_ok = False
    for line in out.split("\n"):
        if "instance" in line.lower():
            if "0 instance" not in line.lower():
                mo_ok = True
            break

    if mo_ok:
        log_cb(f"✓ GSM MO found for {shortcode}.")
    else:
        log_cb(f"✗ GSM MO not found (0 instances for rsite=={shortcode}*).")

    # ── 2. Check Cell (gerancell.gerancellid) ───────────────────
    cell_cmd = f'!python {CLI_PY} "cmedit get * gerancell.gerancellid=={modified_sc}* -t"'
    log_cb(f"Checking GSM Cell in BSC (gerancellid=={modified_sc}*)...")
    out = ssh.run_amos_command_safe(cell_cmd, node_name, timeout=60)
    all_output += out
    log_cb(f"Cell check output:\n{out}")

    # Count only cells that TRULY belong to this site — a prefix wildcard
    # (gerancellid==M18*) also matches M1800.. / M18328.. from other sites.
    cells = gsm_cells_in_output(out, shortcode)
    cell_ok = len(cells) > 0

    if cell_ok:
        log_cb(f"✓ GSM Cell found for {modified_sc} — {len(cells)} cell(s) "
               "(precise match).")
    else:
        log_cb(f"✗ GSM Cell not found (0 precise matches for site {shortcode}; "
               "any prefix hits were other sites).")

    # ── Result ──────────────────────────────────────────────────
    if mo_ok and cell_ok:
        log_cb(f"✓ BSC Cell and MO verified OK for {shortcode}.")
        return True, all_output

    problems = []
    if not mo_ok:
        problems.append(f"MO (G31Tg.rsite=={shortcode}*) — 0 instances")
    if not cell_ok:
        problems.append(f"Cell (gerancell.gerancellid=={modified_sc}*) — 0 instances")

    msg = (
        f"GSM Cell Define verification failed:\n"
        + "\n".join(problems)
    )
    log_cb(f"✗ {msg}")

    # Retry loop
    while not (mo_ok and cell_ok):
        if not wait_for_user:
            return False, all_output
        retry = wait_for_user(
            f"{msg}\n\nCheck BSC configuration, then click Retry to re-check."
        )
        if not retry:
            log_cb("User chose to stop.")
            return False, all_output

        if not mo_ok:
            log_cb(f"Re-checking MO (rsite=={shortcode}*)...")
            out = ssh.run_amos_command_safe(mo_cmd, node_name, timeout=60)
            all_output += out
            log_cb(f"MO re-check:\n{out}")
            for line in out.split("\n"):
                if "instance" in line.lower():
                    if "0 instance" not in line.lower():
                        mo_ok = True
                    break
            if mo_ok:
                log_cb(f"✓ GSM MO now found.")

        if not cell_ok:
            log_cb(f"Re-checking Cell (gerancellid=={modified_sc}*)...")
            out = ssh.run_amos_command_safe(cell_cmd, node_name, timeout=60)
            all_output += out
            log_cb(f"Cell re-check:\n{out}")
            cell_ok = len(gsm_cells_in_output(out, shortcode)) > 0
            if cell_ok:
                log_cb(f"✓ GSM Cell now found.")

        if not (mo_ok and cell_ok):
            problems = []
            if not mo_ok:
                problems.append(f"MO (G31Tg.rsite=={shortcode}*) — 0 instances")
            if not cell_ok:
                problems.append(f"Cell (gerancell.gerancellid=={modified_sc}*) — 0 instances")
            msg = "GSM Cell Define verification still failing:\n" + "\n".join(problems)

    log_cb(f"✓ BSC Cell and MO verified OK for {shortcode}.")
    return True, all_output


# ── BSC Neighbours (relation) check ─────────────────────────────
def run_bsc_neighbours(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Verify GSM neighbour relations are defined in the BSC.

    Two checks, both keyed on the modified shortcode (first letter +
    digits, e.g. MIN3884 → M3884), run inside AMOS via ``!python cli.py``:

      1. cmedit get *BS* gerancell.gerancellid==<MOD>*,gerancellrelation -t
      2. cmedit get *BS* gerancell.gerancellid==<MOD>*,externalgerancellrelation -t

    The ``*BS*`` scope restricts the search to BSC nodes (vs ``*`` which
    scans the whole network) and ``-t`` prints table output — both make
    the query much faster; the trailing "N instance(s)" line used for
    the verdict is unchanged.

    Each is OK when its instance count is NOT 0 (same rule as the GSM
    cell check). Both must be OK for the step to pass.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
    modified_sc = _shortcode_to_cell_id(shortcode)

    intra_cmd = (
        f'!python {CLI_PY} "cmedit get *BS* '
        f'gerancell.gerancellid=={modified_sc}*,gerancellrelation -t"'
    )
    ext_cmd = (
        f'!python {CLI_PY} "cmedit get *BS* '
        f'gerancell.gerancellid=={modified_sc}*,externalgerancellrelation -t"'
    )

    def _has_instances(out: str) -> bool:
        """True if the cmedit output reports a non-zero instance count."""
        for line in out.split("\n"):
            if "instance" in line.lower():
                return "0 instance" not in line.lower()
        return False

    def _check(label: str, cmd: str) -> tuple[bool, str]:
        log_cb(f"Checking BSC {label} (gerancellid=={modified_sc}*)...")
        out = ssh.run_amos_command_safe(cmd, node_name, timeout=60)
        # Only count relations on cells that TRULY belong to this site — a
        # prefix wildcard also matches other sites (M18* → M1800.., M18328..).
        cells = gsm_cells_in_output(out, shortcode)
        ok = len(cells) > 0
        log_cb(f"{label} output:\n{out}")
        if ok:
            log_cb(f"✓ BSC {label} found for {modified_sc} "
                   f"({len(cells)} site cell(s)).")
        else:
            log_cb(
                f"✗ BSC {label} not found (0 precise matches for site "
                f"{shortcode}; any prefix hits were other sites)."
            )
        return ok, out

    intra_ok, out1 = _check("GeranCellRelation", intra_cmd)
    all_output += out1
    ext_ok, out2 = _check("ExternalGeranCellRelation", ext_cmd)
    all_output += "\n" + out2

    if intra_ok and ext_ok:
        log_cb(f"✓ BSC Neighbours verified OK for {modified_sc}.")
        return True, all_output

    def _problems() -> str:
        probs = []
        if not intra_ok:
            probs.append(
                f"GeranCellRelation (gerancellid=={modified_sc}*) — 0 instances")
        if not ext_ok:
            probs.append(
                f"ExternalGeranCellRelation (gerancellid=={modified_sc}*) — 0 instances")
        return "\n".join(probs)

    msg = f"BSC Neighbours verification failed:\n{_problems()}"
    log_cb(f"✗ {msg}")

    # Retry loop (same pattern as gsm_cell_define).
    while not (intra_ok and ext_ok):
        if not wait_for_user:
            return False, all_output
        retry = wait_for_user(
            f"{msg}\n\nCheck BSC neighbour relations, then click Retry "
            "to re-check."
        )
        if not retry:
            log_cb("User chose to stop.")
            return False, all_output
        if not intra_ok:
            intra_ok, out1 = _check("GeranCellRelation", intra_cmd)
            all_output += "\n" + out1
        if not ext_ok:
            ext_ok, out2 = _check("ExternalGeranCellRelation", ext_cmd)
            all_output += "\n" + out2
        msg = f"BSC Neighbours still failing:\n{_problems()}"

    log_cb(f"✓ BSC Neighbours verified OK for {modified_sc}.")
    return True, all_output


# ── Take CM Dump step ───────────────────────────────────────────
def run_take_cm_dump(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    local_dump_dir: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Export CM configuration via cmedit, get the zip from the server path.

    Steps:
      1. cmedit export --ne <node> --filetype 3GPP --jobname <node>_YYMMDD_XML
      2. cmedit export --status --jobname ...  (poll until COMPLETED)
         — the status output contains the file path in the 'File name' column,
           e.g. /ericsson/batch/data/export/3gpp_export/<jobname>.zip
      3. SFTP download the zip to local DUMP/<nodename>_cmdump.zip

    Returns:
        (success: bool, full_output: str)
    """
    import re
    all_output = ""

    date_str = time.strftime("%y%m%d")
    job_name = f"{node_name}_{date_str}_XML"

    # ── 1. Start export ─────────────────────────────────────────
    export_cmd = (
        f'!python {CLI_PY} "cmedit export --ne {node_name} '
        f'--filetype 3GPP --jobname {job_name}"'
    )
    log_cb(f"Starting CM export (job: {job_name})...")
    out = ssh.run_amos_command_safe(export_cmd, node_name, timeout=120)
    all_output += out
    log_cb(f"Export output:\n{out}")

    # ── 2. Poll status until COMPLETED ──────────────────────────
    status_cmd = (
        f'!python {CLI_PY} "cmedit export --status --jobname {job_name}"'
    )
    log_cb("Checking export status...")
    max_attempts = 20  # up to ~10 minutes
    export_done = False
    remote_file = None

    for attempt in range(1, max_attempts + 1):
        out = ssh.run_amos_command_safe(status_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Status check #{attempt}:\n{out}")

        out_lower = out.lower()
        if "completed" in out_lower:
            export_done = True
            # Extract file path from status output
            # The path appears as /ericsson/batch/data/export/3gpp_export/<jobname>.zip
            for line in out.split("\n"):
                m = re.search(r'(/\S+\.zip)', line)
                if m:
                    remote_file = m.group(1)
                    break
            break
        elif "failed" in out_lower:
            msg = f"CM export failed for {node_name}."
            log_cb(f"✗ {msg}")
            if wait_for_user:
                retry = wait_for_user(f"{msg}\n\nCheck the output and retry.")
                if not retry:
                    return False, all_output
            else:
                return False, all_output
        else:
            delay = 10 if attempt <= 5 else 20
            log_cb(f"Export still in progress ({attempt}/{max_attempts}), waiting {delay}s...")
            time.sleep(delay)

    if not export_done:
        msg = f"CM export did not complete after {max_attempts} attempts."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(f"{msg}\n\nClick Retry to keep checking.")
            if not retry:
                return False, all_output
            # One more check
            out = ssh.run_amos_command_safe(status_cmd, node_name, timeout=60)
            all_output += out
            if "completed" in out.lower():
                export_done = True
                for line in out.split("\n"):
                    m = re.search(r'(/\S+\.zip)', line)
                    if m:
                        remote_file = m.group(1)
                        break
            if not export_done:
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ CM export completed.")

    # Fallback: construct the expected path if not parsed
    if not remote_file:
        remote_file = f"/ericsson/batch/data/export/3gpp_export/{job_name}.zip"
        log_cb(f"Using expected path: {remote_file}")

    log_cb(f"✓ Export file: {remote_file}")

    # ── 3. SFTP download to local DUMP folder ───────────────────
    local_dir = os.path.join(local_dump_dir, "DUMP")
    os.makedirs(local_dir, exist_ok=True)
    local_filename = f"{node_name}_cmdump.zip"
    local_path = os.path.join(local_dir, local_filename)

    log_cb(f"Downloading {os.path.basename(remote_file)} → {local_path}...")
    try:
        ssh.sftp_download(remote_file, local_path)
        file_size = os.path.getsize(local_path)
        log_cb(f"✓ CM Dump saved: {local_path} ({file_size:,} bytes)")
        all_output += f"[SFTP] Downloaded → {local_path} ({file_size:,} bytes)\n"
    except Exception as exc:
        msg = f"SFTP download failed: {exc}"
        log_cb(f"✗ {msg}")
        all_output += f"[SFTP] {msg}\n"
        if wait_for_user:
            retry = wait_for_user(
                f"Download failed: {exc}\n\n"
                f"You can manually copy from:\n{remote_file}"
            )
            if not retry:
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ Take CM Dump completed for {node_name}.")
    return True, all_output


def run_take_cm_dump_batch(
    ssh: IntegrationSSH,
    node_list: list,
    cluster: str,
    local_zip_path: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Export CM config for MANY nodes in one ENM job → one combined zip.

    Same ``cmedit export`` as the single-node take, but ``--ne`` receives a
    semicolon-separated node list, so ENM produces one bulk file containing
    every node's MOs. Run over the plain shell (cli.py talks to ENM directly —
    no AMOS/node attach needed). Downloads to ``local_zip_path``.

    Returns (success, full_output).
    """
    import re
    all_output = ""
    ne_scope = ";".join(n.strip() for n in node_list if n.strip())
    date_str = time.strftime("%y%m%d_%H%M%S")
    safe_cluster = re.sub(r"[^A-Za-z0-9._-]", "_", cluster or "BATCH")
    job_name = f"{safe_cluster}_{date_str}_XML"

    export_cmd = (f'python {CLI_PY} "cmedit export --ne {ne_scope} '
                  f'--filetype 3GPP --jobname {job_name}"')
    log_cb(f"Starting batch CM export for {len(node_list)} node(s) "
           f"(job: {job_name})...")
    out = ssh.run_command(export_cmd, timeout=180)
    all_output += out
    log_cb(f"Export output:\n{out}")

    status_cmd = f'python {CLI_PY} "cmedit export --status --jobname {job_name}"'
    log_cb("Checking export status...")
    max_attempts = 30          # batch can take longer than a single node
    remote_file = None
    for attempt in range(1, max_attempts + 1):
        out = ssh.run_command(status_cmd, timeout=90)
        all_output += out
        low = out.lower()
        if "completed" in low:
            for line in out.split("\n"):
                m = re.search(r"(/\S+\.zip)", line)
                if m:
                    remote_file = m.group(1)
                    break
            log_cb("✓ Batch CM export completed.")
            break
        if "failed" in low:
            log_cb(f"✗ Batch CM export failed (job {job_name}).")
            if wait_for_user and wait_for_user("Batch export failed. Retry?"):
                continue
            return False, all_output
        delay = 15 if attempt <= 6 else 30
        log_cb(f"Export in progress ({attempt}/{max_attempts}), waiting {delay}s...")
        time.sleep(delay)
    else:
        log_cb("✗ Batch CM export did not complete in time.")
        return False, all_output

    if not remote_file:
        remote_file = f"/ericsson/batch/data/export/3gpp_export/{job_name}.zip"
        log_cb(f"Using expected path: {remote_file}")

    os.makedirs(os.path.dirname(local_zip_path), exist_ok=True)
    log_cb(f"Downloading {os.path.basename(remote_file)} → {local_zip_path}...")
    try:
        ssh.sftp_download(remote_file, local_zip_path)
        size = os.path.getsize(local_zip_path)
        log_cb(f"✓ Batch dump saved: {local_zip_path} ({size:,} bytes)")
    except Exception as exc:
        log_cb(f"✗ SFTP download failed: {exc}")
        all_output += f"[SFTP] {exc}\n"
        return False, all_output

    return True, all_output


def run_moshell_script(ssh: IntegrationSSH, node_name: str, local_mos: str,
                       log_cb: Callable[[str], None]) -> str:
    """Upload a generated ``.mos`` script and run it against ``node_name`` with
    ``amos <node> <script>`` (batch moshell). Returns the command output.

    Used by the audit 'Run Scripts' flow — the user reviews/edits the .mos file
    first, then explicitly runs it. This SETS parameters on the live node."""
    import os as _os
    sftp = ssh.client.open_sftp()
    try:
        home = sftp.normalize(".")
        rdir = f"{home}/NODECRAFT_SCRIPTS"
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            sftp.mkdir(rdir)
        remote = f"{rdir}/{_os.path.basename(local_mos)}"
        sftp.put(local_mos, remote)
        log_cb(f"Uploaded → {remote}")
    finally:
        sftp.close()
    log_cb(f"Running: amos {node_name} {remote}")
    out = ssh.run_command(f"amos {node_name} {remote}", timeout=1200)
    return out


def run_mobatch_scripts(ssh: IntegrationSSH, node_files: list, stamp: str,
                        label: str, log_cb: Callable[[str], None],
                        parallel_cap: int = 30) -> tuple:
    """Run all generated per-node ``.mos`` in PARALLEL via ``mobatch``.

    ``node_files`` = list of (node_name, local_mos_path). Every file shares the
    same timestamp and is named ``<node>_SetParameter_<stamp>.mos``, so a single
    mobatch command with the ``$nodename`` variable runs each node's own script:

        mobatch -p <N> <sitelist> 'lt all;run <dir>/$nodename_SetParameter_<stamp>.mos' <logdir>

    The argument must be a moshell COMMAND (``lt all`` then ``run <script>``) —
    not a bare file path. Passing just the path makes mobatch hand the literal
    string to moshell as a command (``no such command: …``) and never
    substitutes ``$nodename``. As a command string, mobatch substitutes
    ``$nodename`` per node and moshell ``run``s that node's own script.
    ``$nodename`` is single-quoted so the local bash doesn't expand it — mobatch
    on the gateway substitutes it per node. Returns (output, remote_logdir)."""
    import os as _os
    sftp = ssh.client.open_sftp()
    try:
        home = sftp.normalize(".")
        rdir = f"{home}/NODECRAFT_SCRIPTS"
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            sftp.mkdir(rdir)
        for node, local in node_files:
            sftp.put(local, f"{rdir}/{_os.path.basename(local)}")
        sitelist = f"{rdir}/sitelist_{label}_{stamp}.txt"
        with sftp.open(sitelist, "w") as fh:
            fh.write("\n".join(n for n, _ in node_files) + "\n")
        log_cb(f"Uploaded {len(node_files)} script(s) + sitelist → {rdir}")
    finally:
        sftp.close()

    p = max(1, min(len(node_files), parallel_cap))
    logdir = f"{home}/mobatch_logs/{label}"
    # Pass a moshell COMMAND ('lt all' then 'run <script>'), not a bare path —
    # so mobatch substitutes $nodename per node and moshell actually runs the
    # per-node script (a bare path is taken as a literal command → fails).
    run_cmd = f"'lt all;run {rdir}/$nodename_SetParameter_{stamp}.mos'"
    # 'yes |' auto-answers mobatch's y/n confirmation so the run doesn't hang.
    cmd = (f"mkdir -p {logdir}; yes | mobatch -p {p} {sitelist} "
           f"{run_cmd} {logdir}")
    log_cb(f"Running: mobatch -p {p} {sitelist} "
           f"'lt all;run $nodename_SetParameter_{stamp}.mos' {logdir}")
    out = ssh.run_command(cmd, timeout=5400)
    return out, logdir


def download_remote_dir(ssh: IntegrationSSH, remote_dir: str, local_dir: str,
                        log_cb: Callable[[str], None]) -> list:
    """Recursively SFTP-download every file under ``remote_dir`` into
    ``local_dir`` (flattened structure preserved). Returns local file paths."""
    import os as _os
    from stat import S_ISDIR
    sftp = ssh.client.open_sftp()
    downloaded = []

    def _walk(rdir, ldir):
        _os.makedirs(ldir, exist_ok=True)
        try:
            entries = sftp.listdir_attr(rdir)
        except FileNotFoundError:
            log_cb(f"Remote dir not found: {rdir}")
            return
        for ent in entries:
            rp = f"{rdir}/{ent.filename}"
            lp = _os.path.join(ldir, ent.filename)
            if S_ISDIR(ent.st_mode):
                _walk(rp, lp)
            else:
                try:
                    sftp.get(rp, lp)
                    downloaded.append(lp)
                except Exception as exc:
                    log_cb(f"  ✗ download {ent.filename}: {exc}")

    try:
        _walk(remote_dir, local_dir)
    finally:
        sftp.close()
    log_cb(f"Downloaded {len(downloaded)} file(s) → {local_dir}")
    return downloaded


# ── PM Measurement step ─────────────────────────────────────────
# ── External Alarm step ──────────────────────────────────────────
_EXTERNAL_ALARM_TEMPLATE = _resolve_script_path(_CFG.get(
    "external_alarm_template",
    "/home/shared/common/INTEGRATION_TEAM/script/External_Alarm_Template.txt",
))


def run_external_alarm(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Pre-define External Alarm — install the AlarmPort template on
    the primary BB and verify with ``st alarmport``.

    Sub-steps:
      1. ``run <template>`` — drives a moshell ``run`` of
         ``External_Alarm_Template.txt`` which creates the 8
         AlarmPort MOs under ``Equipment=1,FieldReplaceableUnit=BB-1``.
         Sentinel + 5 s quiescence so the verify command only runs
         once the template script truly finished.
      2. ``st alarmport`` — expect ``Total: 8 MOs`` in output. If the
         count is anything else, prompt the operator to fix manually
         (typical cause: missing or wrong external alarm hardware).

    Args:
        ssh:           Active SSH session, already inside AMOS for
                       ``node_name``.
        node_name:     The primary LTE/NR node DN (caller is
                       responsible for skipping this step on lte2/gsm
                       via the ``applies_to="lte_primary"`` scope).
        log_cb:        Detail-log callback.
        wait_for_user: Optional retry prompt.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # ── 1. Run the External_Alarm_Template.txt ──────────────────
    log_cb(
        f"Running External_Alarm_Template.txt on {node_name}: "
        f"{_EXTERNAL_ALARM_TEMPLATE}"
    )
    log_cb("(sentinel + 5s idle quiescence; usually finishes in <60 s)")
    out = ssh.run_amos_blocking_with_sentinel(
        f"run {_EXTERNAL_ALARM_TEMPLATE}",
        node_name, timeout=600, quiet_after=5.0,
    )
    all_output += out
    log_cb(f"External_Alarm_Template.txt output:\n{out}")

    # ── 2. Verify with st alarmport — expect "Total: 8 MOs" ─────
    def _verify_once() -> tuple[bool, str, Optional[int]]:
        """Run ``st alarmport`` once and parse the total count. Returns
        ``(ok, raw_output, count_or_None)``."""
        v = ssh.run_amos_command_safe("st alarmport", node_name, timeout=30)
        m = re.search(r"Total:\s*(\d+)\s*MOs", v)
        count = int(m.group(1)) if m else None
        return (count == 8), v, count

    log_cb("Verifying with 'st alarmport' (expecting 8 AlarmPort MOs)...")
    ok, verify_out, count = _verify_once()
    all_output += verify_out
    log_cb(f"st alarmport output:\n{verify_out}")

    if ok:
        log_cb(f"✓ External Alarm verified — Total: 8 MOs on {node_name}.")
        return True, all_output

    # ── Retry loop via wait_for_user ────────────────────────────
    while not ok:
        msg = (
            f"External Alarm verification failed for {node_name}.\n"
            f"Expected 'Total: 8 MOs' from 'st alarmport', got "
            f"{'(no Total line in output)' if count is None else f'Total: {count} MOs'}."
        )
        log_cb(f"✗ {msg}")
        if not wait_for_user:
            return False, all_output
        retry = wait_for_user(
            f"{msg}\n\nIf you've fixed the hardware / re-run the "
            f"template manually, click Retry to re-verify with "
            f"'st alarmport'. Click Stop to abort."
        )
        if not retry:
            log_cb("User chose to stop the External Alarm verification.")
            return False, all_output
        log_cb("Re-checking 'st alarmport'...")
        ok, verify_out, count = _verify_once()
        all_output += verify_out
        log_cb(f"Re-check output:\n{verify_out}")

    log_cb(f"✓ External Alarm verified — Total: 8 MOs on {node_name}.")
    return True, all_output


def run_pm_measurement(
    ssh: IntegrationSSH,
    node_name: str,
    node_type: str,  # "lte_nr" or "gsm"
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Verify PM measurements are active with ``pst . act``.

    Expected USERDEF PM job per node type:
      - lte_nr → "USERDEF-Radionode_4G_5G_PM.Cont.Y.STATS"
      - gsm    → "USERDEF-Radionode_2G_PM.Cont.Y.STATS"

    Returns:
        (success: bool, full_output: str)
    """
    if node_type == "gsm":
        expected_marker = "USERDEF-Radionode_2G_PM.Cont.Y.STATS"
    else:
        expected_marker = "USERDEF-Radionode_4G_5G_PM.Cont.Y.STATS"

    log_cb(f"Checking PM Measurement (pst . act) — looking for '{expected_marker}'...")

    out = ssh.run_amos_command_safe("pst . act", node_name, timeout=120)
    log_cb(f"pst . act output:\n{out}")

    if expected_marker in out:
        # Also ensure the row shows ACTIVE state (not e.g. SUSPENDED)
        active_ok = False
        for line in out.split("\n"):
            if expected_marker in line and "ACTIVE" in line:
                active_ok = True
                break
        if active_ok:
            log_cb(f"✓ PM Measurement OK — {expected_marker} is ACTIVE")
            return True, out
        else:
            log_cb(f"⚠ {expected_marker} present but not ACTIVE.")

    # Retry loop
    while True:
        msg = (
            f"PM Measurement verification failed for {node_name}.\n"
            f"Expected '{expected_marker}' ACTIVE in 'pst . act' output."
        )
        log_cb(f"✗ {msg}")

        if not wait_for_user:
            return False, out
        retry = wait_for_user(
            f"{msg}\n\nFix the PM jobs, then click Retry to re-check."
        )
        if not retry:
            log_cb("User chose to stop.")
            return False, out

        log_cb("Re-checking PM Measurement...")
        out = ssh.run_amos_command_safe("pst . act", node_name, timeout=120)
        log_cb(f"Re-check output:\n{out}")

        for line in out.split("\n"):
            if expected_marker in line and "ACTIVE" in line:
                log_cb(f"✓ PM Measurement OK — {expected_marker} is ACTIVE")
                return True, out
