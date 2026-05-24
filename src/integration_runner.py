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
_UPGRADE_PKG_ID = _URI_CFG.get("upgrade_package_id", "CXP2010174/2-R42H05")

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
            self._step_log_fp = open(path, "w", encoding="utf-8", buffering=1)
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

    def _tee(self, chunk: str) -> None:
        """Write a decoded recv chunk to the step log, if open."""
        if self._step_log_fp is not None and chunk:
            try:
                self._step_log_fp.write(chunk)
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
        buf = ""
        start = time.time()
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for shell prompt")
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for shell prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                buf += chunk
                clean = strip_ansi(buf)
                last = clean.strip().split("\n")[-1].strip()
                if _is_shell_prompt(last):
                    # Drain any trailing bytes
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        buf += (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                    break
            else:
                time.sleep(0.3)
        return buf

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
        buf = ""
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
                chunk = (lambda _c=self.shell.recv(65536).decode(
                    "utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                buf += chunk
                clean = strip_ansi(buf)
                # Auto-answer y/n confirmation
                if not answered and "[y/n]" in clean.lower():
                    self._log("  auto-answering [y/n] prompt with 'y'")
                    self.send("y")
                    answered = True
                    continue
                last = clean.strip().split("\n")[-1].strip()
                if (prompt_re.match(last)
                        and "<" not in last
                        and "/" not in last):
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        buf += (lambda _c=self.shell.recv(65536).decode(
                            "utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                    break
            else:
                time.sleep(0.3)
        return strip_ansi(buf)

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
            f"+ {quiet_after:.0f}s quiet"
        )
        self.send(command)
        # Small gap so moshell reads the command line before the echo.
        time.sleep(0.4)
        self.send(f"!echo {sentinel}")

        prompt_re = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")
        buf = ""
        start = time.time()
        saw_sentinel = False
        saw_prompt_after_sentinel = False
        last_byte_time = start
        last_progress = start
        while True:
            if self._channel_dead():
                self._log("[sentinel] channel dead — aborting wait.")
                break
            if time.time() - start > timeout:
                self._log(
                    f"[sentinel] TIMEOUT after {timeout}s — "
                    f"sentinel {'seen' if saw_sentinel else 'NOT seen'}. "
                    "Returning what we have."
                )
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                self._tee(chunk)
                buf += chunk
                now = time.time()
                last_byte_time = now
                last_progress = now
                clean = strip_ansi(buf)
                if not saw_sentinel:
                    # IMPORTANT: distinguish PTY input-echo from real
                    # output. When we ``send("!echo SENTINEL")``, the PTY
                    # may echo those bytes back to us *immediately* —
                    # long before moshell actually executes the echo.
                    # The input-echo line looks like
                    #     "!echo __TRFS_DONE_xxx__"
                    # while the real output line is
                    #     "__TRFS_DONE_xxx__"   (on its own).
                    # We only count the latter; otherwise we could flip
                    # ``saw_sentinel`` true while the batch is still
                    # running and a slow MO commit (>10s without output)
                    # would falsely satisfy the quiescence gate.
                    for line in clean.splitlines():
                        s = line.strip()
                        if s == sentinel:
                            saw_sentinel = True
                            elapsed = int(now - start)
                            self._log(
                                f"[sentinel] nonce seen on its own line "
                                f"after {elapsed}s — script finished; "
                                f"waiting for AMOS prompt + "
                                f"{quiet_after:.0f}s of idle."
                            )
                            break
                if saw_sentinel:
                    last_line = ""
                    for line in reversed(clean.splitlines()):
                        if line.strip():
                            last_line = line.strip()
                            break
                    if (prompt_re.match(last_line)
                            and "<" not in last_line
                            and "/" not in last_line):
                        saw_prompt_after_sentinel = True
            else:
                now = time.time()
                # Quiescence gate: sentinel fired, prompt returned, and
                # channel has been silent for ``quiet_after`` seconds.
                if (saw_sentinel
                        and saw_prompt_after_sentinel
                        and (now - last_byte_time) >= quiet_after):
                    self._log(
                        f"[sentinel] prompt idle for {quiet_after:.0f}s — "
                        "baseline/relation confirmed complete."
                    )
                    break
                # Periodic heartbeat during multi-minute waits
                if now - last_progress > 60:
                    phase = (
                        "pending sentinel"
                        if not saw_sentinel
                        else (
                            "waiting for AMOS prompt"
                            if not saw_prompt_after_sentinel
                            else f"waiting for {quiet_after:.0f}s idle "
                                 f"(last byte {int(now-last_byte_time)}s ago)"
                        )
                    )
                    self._log(
                        f"[sentinel] still alive — {phase}, "
                        f"elapsed={int(now-start)}s"
                    )
                    last_progress = now
                time.sleep(0.5)
        return strip_ansi(buf)

    def _read_until_amos(self, timeout: int = 120) -> str:
        """Read output until an AMOS/moshell prompt (``NODENAME>``) appears.

        Must NOT match XML closing tags like ``</hello>`` or ``</rpc-reply>``
        when a command like ``netconf`` emits XML — only the actual shell
        prompt (alphanumeric+underscore only, ending with ``>``).
        """
        import re
        prompt_re = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")
        buf = ""
        start = time.time()
        while True:
            if self._channel_dead():
                logger.info("Channel closed while waiting for AMOS prompt")
                break
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for AMOS prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                buf += chunk
                clean = strip_ansi(buf)
                last = clean.strip().split("\n")[-1].strip()
                # Real AMOS prompt: word-only token + '>', no XML chars
                if (prompt_re.match(last)
                        and "<" not in last
                        and "/" not in last):
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        buf += (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                    break
            else:
                time.sleep(0.3)
        return buf

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
        """Read until AMOS prompt, handling username/password prompts with 'rbs'/'rbs'."""
        buf = ""
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
                chunk = (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                buf += chunk
                clean = strip_ansi(buf)
                tail = clean[-500:].lower()
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
                last = clean.strip().split("\n")[-1].strip()
                import re as _re
                if (_re.match(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$", last)
                        and "<" not in last and "/" not in last):
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        buf += (lambda _c=self.shell.recv(65536).decode("utf-8", errors="replace"): (self._tee(_c), _c)[1])()
                    break
            else:
                time.sleep(0.3)
        return buf

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
        """
        output = self.run_amos_command(command, timeout=timeout)
        if not self.is_session_expired(output):
            return output

        self._log("⚠ Session expired detected — reconnecting SSH and re-entering AMOS...")
        self.reconnect()
        if in_amos:
            self.enter_amos(node_name, timeout=90)
        output2 = self.run_amos_command(command, timeout=timeout)
        return output + "\n[SESSION RECONNECTED]\n" + output2

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

    # ── GSM only: set controllingBsc → NetworkElement=<BSC> ─────
    # Same invocation pattern as ``verify_arne``: python + cli.py with
    # the cmedit string wrapped in shell quotes. We then read the
    # attribute back with ``cmedit get`` to confirm it was applied.
    if bsc_name:
        bsc = bsc_name.strip()
        if not bsc:
            log_cb("(controllingBsc skipped — empty BSC name)")
        else:
            set_ok, set_out = _set_controlling_bsc(
                ssh, node_name, bsc, log_cb,
                wait_for_user=wait_for_user,
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
) -> tuple[bool, str]:
    """Set ``controllingBsc`` on a GSM NetworkElement and verify.

    Sends, via the same ``python cli.py "<cmd>"`` wrapper used by
    ``verify_arne``::

        cmedit set NetworkElement=<node> \\
                   controllingBsc="NetworkElement=<bsc>"

    Then reads back with ``cmedit get NetworkElement=<node> \\
    controllingBsc`` and confirms the response shows the expected BSC.
    On mismatch, prompts the operator to retry (same pattern as the
    ARNE verification loop above).
    """
    all_output = ""
    expected_value = f"NetworkElement={bsc_name}"

    # Outer single quotes wrap the whole cmedit string for bash; the
    # inner double quotes around the controllingBsc value are preserved
    # and reach cli.py intact.
    set_cmd = (
        f"python {CLI_PY} "
        f"'cmedit set NetworkElement={node_name} "
        f'controllingBsc="NetworkElement={bsc_name}"\''
    )
    log_cb(f"Setting controllingBsc on {node_name} → {expected_value}")
    log_cb(f"  $ {set_cmd}")
    set_out = ssh.run_command(set_cmd, timeout=60)
    all_output += set_out
    log_cb(f"cmedit set output:\n{set_out}")

    # Verification: read attribute back. Successful "1 instance(s)
    # updated" plus the BSC name appearing in the get output is our
    # double-check.
    get_cmd = (
        f"python {CLI_PY} "
        f'"cmedit get {node_name} controllingBsc"'
    )

    def _verify_once() -> tuple[bool, str]:
        log_cb(f"  $ {get_cmd}")
        out = ssh.run_command(get_cmd, timeout=60)
        log_cb(f"cmedit get output:\n{out}")
        ok = (
            "1 instance(s)" in out
            and expected_value in out
        )
        return ok, out

    ok, get_out = _verify_once()
    all_output += "\n" + get_out
    if ok:
        log_cb(
            f"✓ controllingBsc verified on {node_name} → {expected_value}"
        )
        return True, all_output

    # Verification failed — same retry loop pattern as ARNE.
    while not ok:
        log_cb(
            f"✗ controllingBsc not yet showing {expected_value} on "
            f"{node_name}."
        )
        if wait_for_user is None:
            return False, all_output
        retry = wait_for_user(
            f"Setting controllingBsc on '{node_name}' → "
            f"'{expected_value}' did not verify.\n\n"
            f"Check the output above (the attribute may need ENM "
            f"propagation time, or the BSC name may be wrong).\n"
            f"Click 'Retry' to re-check, or 'Stop' to skip the BSC "
            f"link step."
        )
        if not retry:
            log_cb("User chose to stop the controllingBsc verification.")
            return False, all_output
        log_cb("User clicked Retry — re-checking controllingBsc…")
        ok, get_out = _verify_once()
        all_output += "\n" + get_out

    log_cb(
        f"✓ controllingBsc verified on {node_name} → {expected_value}"
    )
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
    """Upload LKF zip via SFTP, import, install, and poll status.

    Sub-steps:
      1. SFTP upload LKF zip to ~/LKF/
      2. lkfimport.py <zipfile>.zip
      3. lkfinstall.py <nodename>  → extract job name
      4. lkfstatus.py <jobname>    → poll until COMPLETED (max 10×60s)

    Must be called while inside an AMOS session (or will enter one).

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
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
        # ── 1. Upload LKF file via SFTP ──────────────────────────────
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

        # ── 2. Import LKF (direct SSH exec — much faster than !python in AMOS) ──
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
    max_attempts = 20
    completed = False

    # Real fatal states the LKF status tool prints — anchored to a
    # status field, not free-text.
    FATAL_RE = re.compile(
        r"(?:^|\s)(?:status|state|result)\s*[:=]\s*"
        r"(failed|failure|error|cancelled|canceled|terminated)\b",
        re.IGNORECASE,
    )
    COMPLETED_RE = re.compile(
        r"(?:^|\s)(?:status|state|result)\s*[:=]\s*completed\b",
        re.IGNORECASE,
    )

    for attempt in range(1, max_attempts + 1):
        out = ssh.exec_ssh(status_cmd, timeout=120)
        all_output += out
        log_cb(f"Status check #{attempt}:\n{out}")

        if COMPLETED_RE.search(out) or "COMPLETED" in out.split("\n")[-5:].__str__().upper():
            # Bias toward the regex match, but accept "COMPLETED" if it
            # appears near the end of output (last 5 lines).
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

    if not completed:
        msg = (
            f"LKF installation is failed, need manual check.\n"
            f"Job '{job_name}' did not reach COMPLETED after {max_attempts} attempts."
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"✓ LKF installation COMPLETED for {node_name} (job: {job_name}).")
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

    # ── 1. Detect the current UpgradePackage ID ─────────────────
    # The package ID is not constant across nodes (e.g. ``CXP2010174/2-
    # R42H05`` on some, ``CXP2010174/2-R42G13`` on others). We used to
    # hard-code one from config, which silently broke nodes running a
    # different package. Now we ``pr UpgradePackage`` and parse the
    # actual ID off the printed MO line.
    log_cb(f"Detecting UpgradePackage on {node_name}...")
    pr_out = ssh.run_amos_command_safe(
        "pr UpgradePackage", node_name, timeout=30,
    )
    all_output += pr_out
    log_cb(f"pr UpgradePackage output:\n{pr_out}")

    # Lines look like:
    #   1234 UNLOCKED  ENABLED  SystemFunctions=1,SwM=1,UpgradePackage=CXP2010174/2-R42G13
    # We capture the ID token after ``UpgradePackage=``. The token can
    # include letters, digits, ``/``, ``-`` and ``.`` — anything up to
    # whitespace or end-of-line.
    pkg_re = re.compile(r"UpgradePackage=([A-Za-z0-9_/\-.]+)")
    detected = pkg_re.findall(pr_out)

    # De-duplicate while preserving order. Most nodes list one
    # UpgradePackage; if more than one shows up we prefer the LAST
    # one (typically the current/active package after an upgrade —
    # older packages tend to be printed first).
    seen: dict[str, None] = {}
    for pid in detected:
        seen[pid] = None
    detected_unique = list(seen.keys())

    if not detected_unique:
        msg = (
            f"Could not detect any UpgradePackage on {node_name} from "
            f"'pr UpgradePackage'. Falling back to config default "
            f"'{_UPGRADE_PKG_ID}' (may not match this node)."
        )
        log_cb(f"⚠ {msg}")
        upgrade_pkg_id = _UPGRADE_PKG_ID
    else:
        upgrade_pkg_id = detected_unique[-1]
        if len(detected_unique) == 1:
            log_cb(f"✓ Detected UpgradePackage: {upgrade_pkg_id}")
        else:
            log_cb(
                f"✓ Found {len(detected_unique)} UpgradePackages "
                f"({', '.join(detected_unique)}); using most recent: "
                f"{upgrade_pkg_id}"
            )

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
) -> tuple[bool, str]:
    """Upload relation file and run it on the node.

    Supports two input types:
      - **.xml** → upload, run ``netconf /path/file.xml``, check for <ok/> or </error-message>
      - **.zip** → upload, unzip, find node folder, run each .txt with ``run <filepath>``

    Returns:
        (success: bool, full_output: str)
    """
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
            ssh, node_name, shortcode, remote_dir, filename, log_dir, log_cb,
            all_output, wait_for_user,
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
    log_dir: str,
    log_cb: Callable[[str], None],
    all_output: str,
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Unzip relation zip, find node folder, run each .txt file."""

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
    batch_timeout = max(3600, len(txt_files) * 300)
    log_cb(
        f"(sentinel + 10s idle quiescence; timeout={batch_timeout}s, "
        "heartbeat every 60s)"
    )
    batch_out = ssh.run_amos_blocking_with_sentinel(
        f"run {batch_script}", node_name, timeout=batch_timeout,
        quiet_after=10.0,
    )
    all_output += batch_out
    log_cb(f"Batch run completed ({len(batch_out)} bytes of live output).")

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


# ── Verify MME step ──────────────────────────────────────────────
def run_sgw_check(
    ssh: "IntegrationSSH",
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
    node_type: str = "lte_nr",
    gsm_ping_targets: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """SGW transport reachability check.

    - LTE/NR: ``run {SCRIPTS_PATH}/SWG_Check.mos`` (multi-ping script on server).
    - GSM:    ``mcc Transport=1,Router=Abis,InterfaceIPv4=Abis,
              AddressIPv4=1 ping -c 5 <ip>`` per target.

    Wrapped in ``l+ / l-``; the server-side log is registered for download
    into MOSHELL/. Ping blocks are parsed; failed targets are reported as
    "<ip> > Not OK".

    Returns:
        (success: bool, full_output: str) — success=True iff every ping OK.
    """
    import re
    all_output = ""
    command_output = ""

    remote_log = f"/home/shared/{ssh.username}/SGW_Check_{node_name}.log"

    log_cb(f"Running SGW Check for {node_name} ({node_type})...")
    ssh.run_amos_command_safe(f"!rm -f {remote_log}", node_name, timeout=15)

    ssh.run_amos_command_safe(f"l+ {remote_log}", node_name, timeout=15)
    if node_type == "gsm":
        targets = gsm_ping_targets or ["10.14.194.131"]
        for ip in targets:
            gsm_cmds = [
                (
                    "Abis",
                    f"mcc Transport=1,Router=Abis,InterfaceIPv4=Abis,"
                    f"AddressIPv4=1 ping -c 5 {ip}",
                ),
                (
                    "Traffic",
                    f"mcc Transport=1,Router=Abis,InterfaceIPv4=Traffic,"
                    f"AddressIPv4=1 ping -c 5 {ip}",
                ),
            ]

            for idx, (iface_name, cmd) in enumerate(gsm_cmds, start=1):
                log_cb(f"  $ {cmd}")
                # mcc emits a "[y/n]" confirmation prompt — auto-answer 'y'
                out = ssh.run_amos_command_autoyes(cmd, timeout=180)
                all_output += out

                # Some GSM nodes expose the ping MO only on Traffic. If Abis
                # returns zero MOs, fall back to the Traffic interface.
                if "Total: 0 MOs" in out and idx < len(gsm_cmds):
                    log_cb(
                        f"No MO found on InterfaceIPv4={iface_name}; "
                        f"retrying with InterfaceIPv4={gsm_cmds[idx][0]}..."
                    )
                    continue

                break
        command_output = all_output
    else:
        command_output = ssh.run_amos_command_safe(
            f"run {_SGW_CHECK_MOS}", node_name, timeout=900,
        )
        all_output += command_output
    ssh.run_amos_command_safe("l-", node_name, timeout=15)
    log_cb(f"SGW_Check output ({len(command_output)} bytes).")

    # Register for MOSHELL/ download
    ssh.register_remote_log(remote_log)

    # Parse ping blocks. A "ping <ip>" command appears, then later the
    # stats line "N packets transmitted, M received, K% packet loss, ..."
    # Failure = received=0  OR  100% packet loss  OR  Destination unreachable
    lines = command_output.split("\n")
    current_ip: Optional[str] = None
    results: list[tuple[str, bool]] = []  # (ip, ok)
    seen_ips: set[str] = set()
    # Match the "PING <ip> (" header emitted by /bin/ping — reliable and
    # independent of caller-supplied options like "-c 5" or "--count 3".
    ping_re = re.compile(r"^\s*PING\s+(\d+\.\d+\.\d+\.\d+)\s*\(", re.IGNORECASE)
    stats_re = re.compile(
        r"(\d+)\s+packets\s+transmitted,\s*(\d+)\s+received"
    )

    for line in lines:
        m = ping_re.search(line)
        if m:
            current_ip = m.group(1)
            continue
        if current_ip:
            if "Destination Host Unreachable" in line or \
               "Network is unreachable" in line or \
               "100% packet loss" in line:
                if current_ip not in seen_ips:
                    results.append((current_ip, False))
                    seen_ips.add(current_ip)
                current_ip = None
                continue
            sm = stats_re.search(line)
            if sm:
                transmitted = int(sm.group(1))
                received = int(sm.group(2))
                ok = received > 0 and received == transmitted
                if current_ip not in seen_ips:
                    results.append((current_ip, ok))
                    seen_ips.add(current_ip)
                current_ip = None

    if not results:
        msg = "No ping blocks detected in SGW_Check output."
        log_cb(f"⚠ {msg}")
        all_output += f"\n[SGW CHECK] {msg}\n"
        return False, all_output

    failed = [ip for ip, ok in results if not ok]
    ok_ips = [ip for ip, ok in results if ok]

    log_cb(f"SGW Check parsed {len(results)} target(s): "
           f"{len(ok_ips)} OK, {len(failed)} failed.")

    summary_lines = [
        "[SGW CHECK SUMMARY]",
        f"Total: {len(results)}  OK: {len(ok_ips)}  FAIL: {len(failed)}",
        "-" * 60,
    ]
    for ip, ok in results:
        if ok:
            summary_lines.append(f"  {ip} > OK")
        else:
            summary_lines.append(f"  {ip} > Not OK")
    summary_text = "\n".join(summary_lines)
    all_output += "\n" + summary_text + "\n"
    log_cb(summary_text)

    return (len(failed) == 0), all_output


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


# ── Backup CV step ──────────────────────────────────────────────
def run_backup_cv(
    ssh: IntegrationSSH,
    node_name: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Create a pre-integration SHM backup and wait until it succeeds."""
    all_output = ""
    backup_name = f"PreIntegration_{time.strftime('%Y%m%d_%H%M')}"
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
                return run_backup_cv(ssh, node_name, log_cb, wait_for_user)
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
                    return run_backup_cv(ssh, node_name, log_cb, wait_for_user)
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
    local_filename = f"{node_name}_modump.zip"
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


def run_gsm_cell_define(
    ssh: IntegrationSSH,
    node_name: str,
    shortcode: str,
    log_cb: Callable[[str], None],
    wait_for_user: Optional[Callable[[str], bool]] = None,
) -> tuple[bool, str]:
    """Verify GSM Cell and MO are defined in BSC.

    Two checks:
      1. MO check:  cmedit get * G31Tg.rsite==<SHORTCODE>* -t  → must be > 0 instances
      2. Cell check: cmedit get * gerancell.gerancellid==<MODIFIED_SHORTCODE>* -t  → must be > 0 instances

    The modified shortcode takes the first letter + digits, e.g. MIN2790 → M2790.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
    modified_sc = _shortcode_to_cell_id(shortcode)

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

    cell_ok = False
    for line in out.split("\n"):
        if "instance" in line.lower():
            if "0 instance" not in line.lower():
                cell_ok = True
            break

    if cell_ok:
        log_cb(f"✓ GSM Cell found for {modified_sc}.")
    else:
        log_cb(f"✗ GSM Cell not found (0 instances for gerancellid=={modified_sc}*).")

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
            for line in out.split("\n"):
                if "instance" in line.lower():
                    if "0 instance" not in line.lower():
                        cell_ok = True
                    break
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
