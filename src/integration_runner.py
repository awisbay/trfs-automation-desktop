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
from typing import Callable, Optional

import paramiko

logger = logging.getLogger(__name__)

# ── Load dynamic config ─────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config() -> dict:
    """Load config.json. Returns defaults if file is missing."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("config.json not found, using built-in defaults.")
        return {}


_CFG = _load_config()

SCRIPTS_PATH = _CFG.get("scripts_path", "/home/shared/ESETARI/INOC/SCRIPTS")
CLI_PY = _CFG.get("cli_py", f"{SCRIPTS_PATH}/cli.py")

_CREATE_ARNE = _CFG.get("create_arne_script", "ES/create_arne.py")
_ENTITY_MAKER = _CFG.get("entity_maker_script", "ES/entity_maker.sh")
_EXE_ENTITY = _CFG.get("exe_entity_script", "ES/exe_entity.py")
_ENROLLMENT_MOS = _CFG.get("enrollment_mos", "ES/enroll/lhgenm1.mos")
_SGW_CHECK_MOS = _CFG.get("sgw_check_mos", "SWG_Check.mos")

_LKF_IMPORT = _CFG.get("lkf_import_script", "lkfimport.py")
_LKF_INSTALL = _CFG.get("lkf_install_script", "lkfinstall.py")
_LKF_STATUS = _CFG.get("lkf_status_script", "lkfstatus.py")

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
) -> tuple[bool, str]:
    """Run create_arne.py, fill the 3 prompts, then verify with cli.py.

    If verification fails (0 instances), calls ``wait_for_user(message)``
    which should block until the user clicks Retry (returns True) or
    Cancel (returns False).  Re-verifies in a loop until success or
    cancellation.

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
        command=f"python {SCRIPTS_PATH}/{_CREATE_ARNE}",
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
        f"!bash {SCRIPTS_PATH}/{_ENTITY_MAKER} {node_name}",
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
        f"!python {SCRIPTS_PATH}/{_EXE_ENTITY} "
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
    log_cb("Running enrollment script (lhgenm1.mos)...")
    out = ssh.run_amos_command_safe(
        f"run {SCRIPTS_PATH}/{_ENROLLMENT_MOS}",
        node_name, timeout=600,
    )
    all_output += out
    log_cb(f"Enrollment script output:\n{out}")

    # ── 6. Validate enrollment — poll NodeCredential progress ───
    # The enrollmentProgress can transition IDLE → ONGOING → SUCCESS.
    # Auto-poll up to 6 times at 15s intervals BEFORE bothering the user,
    # so a slow node that's still transitioning won't show a false FAILED.
    log_cb("Validating enrollment (NodeCredential enrollmentProgress)...")
    enroll_success = False
    max_auto_polls = 10
    poll_interval = 8
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

    # ── 7. Force sync (stay in AMOS, use !python) ────────────────
    log_cb(f"Forcing sync for {node_name}...")
    out = ssh.run_amos_command_safe(
        f'!python {CLI_PY} "cmedit action {node_name} cmfunction=1 SYNC"',
        node_name, timeout=60,
    )
    all_output += out
    log_cb(f"Sync command output:\n{out}")

    # ── 8. Wait for SYNCHRONIZED (stay in AMOS, use !python) ─────
    log_cb(f"Waiting for {node_name} to reach SYNCHRONIZED status...")
    sync_cmd = f'!python {CLI_PY} "cmedit get {node_name} cmfunction.syncstatus -t"'
    max_sync_attempts = 30
    synced = False
    for attempt in range(1, max_sync_attempts + 1):
        out = ssh.run_amos_command_safe(sync_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Sync check #{attempt}:\n{out}")

        if "SYNCHRONIZED" in out:
            synced = True
            break

        # Faster polling: 15s first 6 attempts, then 30s
        delay = 15 if attempt <= 6 else 30
        if "TOPOLOGY" in out or "UNSYNCHRONIZED" in out or "PENDING" in out:
            log_cb(f"Status not yet SYNCHRONIZED ({attempt}/{max_sync_attempts}), "
                   f"waiting {delay}s...")
        else:
            log_cb(f"Unexpected sync status ({attempt}/{max_sync_attempts}), "
                   f"waiting {delay}s...")
        time.sleep(delay)

    if not synced:
        msg = (
            f"Sync for {node_name} did not reach SYNCHRONIZED after "
            f"{max_sync_attempts} attempts."
        )
        log_cb(f"✗ {msg}")
        while not synced:
            if not wait_for_user:
                return False, all_output
            retry = wait_for_user(
                f"{msg}\n\nClick Retry to keep checking, or Stop to abort."
            )
            if not retry:
                log_cb("User chose to stop.")
                return False, all_output
            out = ssh.run_amos_command_safe(sync_cmd, node_name, timeout=60)
            all_output += out
            log_cb(f"Re-check sync:\n{out}")
            synced = "SYNCHRONIZED" in out

    log_cb(f"✓ {node_name} is SYNCHRONIZED. Enrollment complete.")
    return True, all_output


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
        f"python {SCRIPTS_PATH}/{_LKF_IMPORT} /home/shared/{ssh.username}/LKF/{zip_filename}",
        timeout=600,
    )
    all_output += out
    log_cb(f"lkfimport.py output:\n{out}")

    # ── 3. Install LKF — extract job name (direct SSH exec) ──────
    log_cb(f"Installing LKF for {node_name}...")
    out = ssh.exec_ssh(
        f"python {SCRIPTS_PATH}/{_LKF_INSTALL} {node_name}",
        timeout=600,
    )
    all_output += out
    log_cb(f"lkfinstall.py output:\n{out}")

    # Extract job name from output
    # Expected: "... with job name: Shm_Cli_InstallLicense_USER_TIMESTAMP"
    job_name = None
    for line in out.split("\n"):
        if "job name:" in line.lower():
            # Get everything after "job name:"
            parts = line.split("job name:")
            if len(parts) > 1:
                job_name = parts[-1].strip()
                # Clean: take only the first word (no trailing junk)
                job_name = job_name.split()[0] if job_name else None
            break

    if not job_name:
        msg = f"Could not extract job name from lkfinstall.py output for {node_name}."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck the output above. If the install was initiated "
                f"manually, click Retry to continue to status check."
            )
            if not retry:
                return False, all_output
            # Ask again — maybe user can provide it or it appeared
            out2 = ssh.exec_ssh(
                f"python {SCRIPTS_PATH}/{_LKF_INSTALL} {node_name}",
                timeout=300,
            )
            all_output += out2
            for line in out2.split("\n"):
                if "job name:" in line.lower():
                    parts = line.split("job name:")
                    if len(parts) > 1:
                        job_name = parts[-1].strip().split()[0]
                    break
            if not job_name:
                log_cb("✗ Still no job name found.")
                return False, all_output
        else:
            return False, all_output

    log_cb(f"✓ Job name: {job_name}")

    # ── 4. Poll status until COMPLETED (direct SSH exec) ─────────
    log_cb(f"Checking LKF installation status for job: {job_name}...")
    status_cmd = f"python {SCRIPTS_PATH}/{_LKF_STATUS} {job_name}"
    max_attempts = 20
    completed = False

    for attempt in range(1, max_attempts + 1):
        out = ssh.exec_ssh(status_cmd, timeout=120)
        all_output += out
        log_cb(f"Status check #{attempt}:\n{out}")

        if "COMPLETED" in out:
            completed = True
            break
        # Early-exit on terminal failure states
        if "FAILED" in out.upper() or "ERROR" in out.upper():
            log_cb("Terminal failure detected in LKF status — stopping poll.")
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

    # ── 1. Check $rats ───────────────────────────────────────────
    log_cb("Checking node RAT type (pv $rats)...")
    out = ssh.run_amos_command_safe("pv $rats", node_name, timeout=30)
    all_output += out
    log_cb(f"pv $rats output:\n{out}")

    # Parse $rats value: look for "$rats = L" or "$rats = LN" etc.
    rats_value = None
    for line in out.split("\n"):
        clean = line.strip()
        if "$rats" in clean and "=" in clean:
            # e.g. "$rats = LN"
            val = clean.split("=")[-1].strip()
            if val:
                rats_value = val.upper()
            break

    if not rats_value or rats_value not in BASELINE_MAP:
        msg = (
            f"Could not determine RAT type from 'pv $rats' output.\n"
            f"Got: {rats_value!r}\n"
            f"Expected one of: L, LN, N"
        )
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"RAT type detected: $rats = {rats_value}")

    # ── 2. Determine baseline file ───────────────────────────────
    baseline_file = BASELINE_MAP[rats_value]
    baseline_path = f"{SCRIPTS_PATH}/{baseline_file}"
    log_cb(f"Baseline file: {baseline_path}")

    # ── 3. List available baseline files so user can verify ──────
    log_cb("Listing available baseline files on server...")
    ls_out = ssh.run_amos_command_safe(
        f"!ls -1 {SCRIPTS_PATH}/Globe_Baseline_*.mos",
        node_name, timeout=15,
    )
    all_output += ls_out
    log_cb(f"Available baseline files:\n{ls_out}")

    # ── 4. Confirm with user before running ──────────────────────
    if confirm_baseline:
        confirmed = confirm_baseline(
            "Confirm Baseline Script",
            f"Node: {node_name}\n"
            f"RAT type: $rats = {rats_value}\n\n"
            f"Baseline file to run:\n"
            f"  {baseline_path}\n\n"
            f"Available files on server:\n{ls_out.strip()}\n\n"
            f"Is this the correct baseline file?\n"
            f"If the filename has been updated, cancel and update the script path."
        )
        if not confirmed:
            log_cb("User cancelled baseline execution.")
            return False, all_output

    # ── 5. Run baseline ──────────────────────────────────────────
    log_cb(f"Running baseline: run {baseline_path}")
    out = ssh.run_amos_command_safe(
        f"run {baseline_path}",
        node_name, timeout=600,  # baselines can take a while
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
      4. curl login to get cookie
      5. curl POST to updateUpMoFtpServerDetails with node name

    Both curl commands must return "SUCCESS" to pass.

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""

    # ── 1-3. AMOS set commands (each requires y/n confirmation) ─
    amos_cmds = [
        "set SwM=1 defaultUri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID} uri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID} "
        "password cleartext=true,password=",
    ]

    for cmd in amos_cmds:
        log_cb(f"Running (with y/n confirm): {cmd}")
        out = ssh.run_amos_set_with_confirm(cmd, node_name, answer="y", timeout=60)
        all_output += out
        log_cb(f"Output:\n{out}")

    # ── 4. curl login to get cookie ─────────────────────────────
    login_cmd = (
        f'!curl --insecure --request POST '
        f'--data "IDToken1={enm_username}" '
        f'--data "IDToken2={enm_password}" '
        f'--cookie-jar ./cookie.txt {ENM_LOGIN_URL}'
    )
    log_cb("Logging in to ENM (curl)...")
    out = ssh.run_amos_command_safe(login_cmd, node_name, timeout=60)
    all_output += out
    log_cb(f"Login output:\n{out}")

    # Check for "Authentication Successful"
    if "Authentication Successful" not in out and "SUCCESS" not in out.upper():
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
            if "Authentication Successful" not in out and \
                    "SUCCESS" not in out.upper():
                log_cb("✗ Login still failed after retry.")
                return False, all_output
        else:
            return False, all_output

    log_cb("✓ ENM login successful.")

    # ── 5. curl POST to update URI FTP server details ───────────
    update_cmd = (
        f"!curl --insecure --request POST "
        f"'{ENM_URI_UPDATE_URL}' "
        f"--cookie cookie.txt "
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
            return False, all_output
        # Retry: re-run curl update + re-verify
        out = ssh.run_amos_command_safe(update_cmd, node_name, timeout=60)
        all_output += out
        vout = ssh.run_amos_command_safe("get depack", node_name, timeout=60)
        all_output += vout
        uri_value = ""
        for line in vout.splitlines():
            s = line.strip()
            if s.startswith("uri") and len(s) > 3:
                parts = s.split(None, 1)
                if len(parts) > 1:
                    uri_value = parts[1].strip()
                break
        if uri_value.startswith("sftp://mm-software@"):
            log_cb(f"✓ URI verified on retry: {uri_value}")
            return True, all_output
        log_cb(f"✗ URI still not valid on retry: {uri_value or '(empty)'}")
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
    log_cb(f"Running all {len(txt_files)} relation files in one batch...")
    batch_timeout = max(900, len(txt_files) * 120)
    batch_out = ssh.run_amos_command_safe(
        f"run {batch_script}", node_name, timeout=batch_timeout,
    )
    all_output += batch_out
    log_cb(f"Batch run completed ({len(batch_out)} bytes of live output).")

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

    # ── 6. Split the live output into per-file sections ─────────
    # Each file's section starts at a line like: "run /path/to/XX_File.txt"
    # and ends right before the next "run /path/..." or end-of-output.
    file_sections: dict[str, str] = {}
    sorted_txt_files = sorted(txt_files)
    # Build regex matching any `run <path>` start-of-line
    import re as _re
    markers = []
    for tp in sorted_txt_files:
        # Match lines containing "run <path>" (prompt prefix optional)
        pattern = _re.compile(
            r"(?m)^(?:.*?>\s*)?run\s+" + _re.escape(tp) + r"\s*$"
        )
        m = pattern.search(batch_out)
        markers.append((m.start() if m else -1, tp))
    # Sort by position in output (skip missing)
    found = [(pos, tp) for pos, tp in markers if pos >= 0]
    found.sort()
    for i, (pos, tp) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(batch_out)
        file_sections[tp] = batch_out[pos:end]
    # Fallback: any txt file with no marker gets the full batch output
    for tp in sorted_txt_files:
        file_sections.setdefault(tp, batch_out)

    errors_summary: list[str] = []
    file_summaries: list[str] = []
    for i, txt_path in enumerate(sorted_txt_files, 1):
        txt_name = os.path.basename(txt_path)
        local_name = f"RELATION_{node_name}_{txt_name.replace('.txt', '')}.txt"
        local_path = os.path.join(log_dir, local_name)

        file_output = file_sections.get(txt_path, "")
        log_cb(f"[{i}/{len(sorted_txt_files)}] Saving log for {txt_name}...")
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

    remote_log = f"/home/shared/{ssh.username}/SGW_Check_{node_name}.log"

    log_cb(f"Running SGW Check for {node_name} ({node_type})...")
    ssh.run_amos_command_safe(f"!rm -f {remote_log}", node_name, timeout=15)

    ssh.run_amos_command_safe(f"l+ {remote_log}", node_name, timeout=15)
    if node_type == "gsm":
        targets = gsm_ping_targets or ["10.14.194.131"]
        for ip in targets:
            cmd = (
                f"mcc Transport=1,Router=Abis,InterfaceIPv4=Abis,"
                f"AddressIPv4=1 ping -c 5 {ip}"
            )
            log_cb(f"  $ {cmd}")
            # mcc emits a "[y/n]" confirmation prompt — auto-answer 'y'
            out = ssh.run_amos_command_autoyes(cmd, timeout=180)
            all_output += out
    else:
        out = ssh.run_amos_command_safe(
            f"run {SCRIPTS_PATH}/{_SGW_CHECK_MOS}", node_name, timeout=900,
        )
        all_output += out
    ssh.run_amos_command_safe("l-", node_name, timeout=15)
    log_cb(f"SGW_Check output ({len(out)} bytes).")

    # Register for MOSHELL/ download
    ssh.register_remote_log(remote_log)

    # Parse ping blocks. A "ping <ip>" command appears, then later the
    # stats line "N packets transmitted, M received, K% packet loss, ..."
    # Failure = received=0  OR  100% packet loss  OR  Destination unreachable
    lines = out.split("\n")
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
    dcg_path = None
    for line in out.split("\n"):
        if "dcg completed successfully" in line.lower() or "logs stored in" in line.lower():
            # Extract path after "logs stored in "
            idx = line.find("/ericsson/")
            if idx != -1:
                dcg_path = line[idx:].strip()
                break
            # Also try "in /" pattern
            idx = line.find("in /")
            if idx != -1:
                dcg_path = line[idx + 3:].strip()
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
