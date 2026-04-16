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

    # ── Core I/O ─────────────────────────────────────────────────
    def send(self, text: str):
        """Send text (with newline) to the shell."""
        self.shell.send(text + "\n")
        time.sleep(0.3)

    def _read_until_prompt(self, timeout: int = 60) -> str:
        """Read output until a shell prompt is detected."""
        buf = ""
        start = time.time()
        while True:
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for shell prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                clean = strip_ansi(buf)
                last = clean.strip().split("\n")[-1].strip()
                if _is_shell_prompt(last):
                    # Drain any trailing bytes
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        buf += self.shell.recv(65536).decode("utf-8", errors="replace")
                    break
            else:
                time.sleep(0.3)
        return buf

    def _read_until(self, marker: str, timeout: int = 60) -> str:
        """Read output until a specific marker string appears in the output."""
        buf = ""
        start = time.time()
        while True:
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for '%s'", timeout, marker)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                if marker.lower() in strip_ansi(buf).lower():
                    time.sleep(0.3)
                    while self.shell.recv_ready():
                        buf += self.shell.recv(65536).decode("utf-8", errors="replace")
                    break
            else:
                time.sleep(0.3)
        return buf

    def _read_until_amos(self, timeout: int = 120) -> str:
        """Read output until an AMOS/moshell prompt (``nodename>``) appears."""
        buf = ""
        start = time.time()
        while True:
            if time.time() - start > timeout:
                logger.warning("Timeout (%ds) waiting for AMOS prompt", timeout)
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buf += chunk
                clean = strip_ansi(buf)
                last = clean.strip().split("\n")[-1].strip()
                # AMOS prompt looks like: NODENAME>
                if last.endswith(">") and not last.startswith("["):
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        buf += self.shell.recv(65536).decode("utf-8", errors="replace")
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
        output += self._read_until_amos(timeout=120)
        self._log("AMOS ready.")
        return strip_ansi(output)

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

    # Step 1: Run the ARNE creation script
    output = ssh.run_interactive(
        command=f"python {SCRIPTS_PATH}/{_CREATE_ARNE}",
        prompts=[
            ("nodename",   node_name),
            ("ipaddress",  node_ip),
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

    # ── 1. Enter AMOS ────────────────────────────────────────────
    log_cb(f"Entering AMOS for {node_name}...")
    out = ssh.enter_amos(node_name, timeout=90)
    all_output += out
    log_cb("AMOS session ready.")

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
                ssh.exit_amos()
                return False, all_output
            check_out2 = ssh.run_amos_command(
                f"!ls ~/INOC/SCRIPTS/NS/{node_name}.xml", timeout=15,
            )
            all_output += check_out2
            if f"{node_name}.xml" not in check_out2 or "No such file" in check_out2:
                log_cb("✗ XML still not found after retry.")
                ssh.exit_amos()
                return False, all_output
        else:
            ssh.exit_amos()
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
                ssh.exit_amos()
                return False, all_output
        else:
            ssh.exit_amos()
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

    # ── 6. Validate enrollment — check NodeCredential ────────────
    log_cb("Validating enrollment (NodeCredential enrollmentProgress)...")
    out = ssh.run_amos_command_safe(
        "get NodeCredential enrollmentProgress result",
        node_name, timeout=60,
    )
    all_output += out
    log_cb(f"NodeCredential output:\n{out}")

    enroll_success = "SUCCESS" in out
    if not enroll_success:
        msg = (
            f"Enrollment validation for {node_name} failed.\n"
            f"Expected 'SUCCESS' in NodeCredential enrollmentProgress result."
        )
        log_cb(f"✗ {msg}")
        while not enroll_success:
            if not wait_for_user:
                ssh.exit_amos()
                return False, all_output
            retry = wait_for_user(
                f"{msg}\n\nCheck the enrollment output for errors.\n"
                f"Fix the issue, then click Retry to re-check."
            )
            if not retry:
                log_cb("User chose to stop.")
                ssh.exit_amos()
                return False, all_output
            log_cb("Re-checking enrollment status...")
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
    max_sync_attempts = 20  # up to ~20 minutes
    synced = False
    for attempt in range(1, max_sync_attempts + 1):
        out = ssh.run_amos_command_safe(sync_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Sync check #{attempt}:\n{out}")

        if "SYNCHRONIZED" in out:
            synced = True
            break

        if "TOPOLOGY" in out or "UNSYNCHRONIZED" in out or "PENDING" in out:
            log_cb(f"Status not yet SYNCHRONIZED (attempt {attempt}/{max_sync_attempts}), "
                   f"waiting 60s...")
            time.sleep(60)
        else:
            log_cb(f"Unexpected sync status (attempt {attempt}), waiting 60s...")
            time.sleep(60)

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

    # ── 2. Import LKF ────────────────────────────────────────────
    log_cb(f"Importing LKF: {zip_filename}...")
    out = ssh.run_amos_command_safe(
        f"!python {SCRIPTS_PATH}/{_LKF_IMPORT} /home/shared/{ssh.username}/LKF/{zip_filename}",
        node_name, timeout=300,
    )
    all_output += out
    log_cb(f"lkfimport.py output:\n{out}")

    # ── 3. Install LKF — extract job name ────────────────────────
    log_cb(f"Installing LKF for {node_name}...")
    out = ssh.run_amos_command_safe(
        f"!python {SCRIPTS_PATH}/{_LKF_INSTALL} {node_name}",
        node_name, timeout=300,
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
            out2 = ssh.run_amos_command_safe(
                f"!python {SCRIPTS_PATH}/{_LKF_INSTALL} {node_name}",
                node_name, timeout=120,
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

    # ── 4. Poll status until COMPLETED ───────────────────────────
    log_cb(f"Checking LKF installation status for job: {job_name}...")
    status_cmd = f"!python {SCRIPTS_PATH}/{_LKF_STATUS} {job_name}"
    max_attempts = 10
    completed = False

    for attempt in range(1, max_attempts + 1):
        out = ssh.run_amos_command_safe(status_cmd, node_name, timeout=60)
        all_output += out
        log_cb(f"Status check #{attempt}:\n{out}")

        if "COMPLETED" in out:
            completed = True
            break

        if attempt < max_attempts:
            log_cb(f"Status not yet COMPLETED (attempt {attempt}/{max_attempts}), "
                   f"waiting 60s...")
            time.sleep(60)

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

    # Look for POST_<baseline_name_without_extension>_execution
    baseline_stem = baseline_file.replace(".mos", "")
    if f"POST_{baseline_stem}_execution" in out:
        log_cb(f"✓ Baseline verified — found POST_{baseline_stem}_execution")
        return True, all_output

    # Not found — retry loop
    msg = (
        f"Baseline verification failed for {node_name}.\n"
        f"Expected 'POST_{baseline_stem}_execution' in cvls output."
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
        if f"POST_{baseline_stem}_execution" in out:
            log_cb(f"✓ Baseline verified — found POST_{baseline_stem}_execution")
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

    # ── 1-3. AMOS set commands ──────────────────────────────────
    amos_cmds = [
        "set SwM=1 defaultUri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID} uri",
        f"set SystemFunctions=1,SwM=1,UpgradePackage={_UPGRADE_PKG_ID} "
        "password cleartext=true,password=",
    ]

    for cmd in amos_cmds:
        log_cb(f"Running: {cmd}")
        out = ssh.run_amos_command_safe(cmd, node_name, timeout=30)
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

    # Check for SUCCESS in the response
    if "SUCCESS" in out.upper():
        log_cb(f"✓ URI setting completed for {node_name}.")
        return True, all_output
    else:
        msg = f"URI setting failed for {node_name}.\nOutput: {out[:300]}"
        log_cb(f"✗ {msg}")
        if wait_for_user:
            retry = wait_for_user(
                f"{msg}\n\nCheck the output and click Retry."
            )
            if not retry:
                return False, all_output
            # Retry the update
            out = ssh.run_amos_command_safe(update_cmd, node_name, timeout=60)
            all_output += out
            if "SUCCESS" in out.upper():
                log_cb(f"✓ URI setting completed on retry for {node_name}.")
                return True, all_output
            else:
                log_cb("✗ URI setting still failed after retry.")
                return False, all_output
        else:
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
    total_error = 0     # lines with "!!!!" or "ERROR"

    for line in lines:
        stripped = line.strip()

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
            idx = lines.index(line)
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
            f"MO_N/A={no_att}  ERROR={total_error}"
        )
        has_issues = (total_error > 0 or tot_failed > 0 or no_att > 0)
    else:
        summary_line = (
            f"CMD_SET={cmd_set}  SUCCEED={att}  "
            f"FAILED={failed}  WRONG_MO={no_att}  "
            f"NO_CHANGE={no_change}  ERROR={total_error}"
        )
        has_issues = (total_error > 0 or failed > 0 or no_att > 0)

    # Collect error details
    error_lines = []
    for i, line in enumerate(lines):
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
    """Upload relation zip, unzip, find node folder, run each txt file.

    Sub-steps:
      1. SFTP upload zip to /home/shared/<user>/RELATION/<SHORTCODE>/
      2. Unzip on server
      3. Find folder matching node_name inside the extracted content
      4. List all .txt files in that folder
      5. Run each file one by one with ``run <filepath>``
      6. Save a separate log per relation file
      7. Summarize errors (grep placeholder for later)

    Returns:
        (success: bool, full_output: str)
    """
    all_output = ""
    zip_filename = os.path.basename(relation_local_path)

    # ── 1. Upload relation zip via SFTP ──────────────────────────
    remote_dir = f"/home/shared/{ssh.username}/RELATION/{shortcode}"
    log_cb(f"Uploading relation file: {zip_filename} → {remote_dir}/")
    try:
        ssh.sftp_upload(relation_local_path, remote_dir)
        all_output += f"[SFTP] Uploaded {zip_filename} → {remote_dir}/{zip_filename}\n"
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

    # ── 2. Unzip on server ───────────────────────────────────────
    log_cb(f"Unzipping {zip_filename} on server...")
    out = ssh.run_amos_command_safe(
        f"!cd {remote_dir} && unzip -o {zip_filename}",
        node_name, timeout=120,
    )
    all_output += out
    log_cb(f"Unzip output:\n{out}")

    # ── 3. Find folder matching node_name ────────────────────────
    log_cb(f"Looking for folder matching '{node_name}'...")
    out = ssh.run_amos_command_safe(
        f"!find {remote_dir} -maxdepth 3 -type d -name '*{node_name}*'",
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
                f"!find {remote_dir} -maxdepth 3 -type d -name '*{node_name}*'",
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
        f"!ls -1 {node_folder}/*.txt 2>/dev/null",
        node_name, timeout=15,
    )
    all_output += out

    txt_files = []
    for line in out.strip().split("\n"):
        line = line.strip()
        if line.endswith(".txt") and line.startswith("/"):
            txt_files.append(line)

    if not txt_files:
        msg = f"No .txt relation files found in {node_folder}"
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    log_cb(f"Found {len(txt_files)} relation file(s) to run.")

    # ── 5. Run each relation file ────────────────────────────────
    errors_summary: list[str] = []
    file_summaries: list[str] = []
    for i, txt_path in enumerate(sorted(txt_files), 1):
        txt_name = os.path.basename(txt_path)
        log_cb(f"[{i}/{len(txt_files)}] Running: {txt_name}...")

        out = ssh.run_amos_command_safe(
            f"run {txt_path}",
            node_name, timeout=900,
        )
        all_output += out
        log_cb(f"Output for {txt_name}:\n{out}")

        # ── 6. Parse output and build summary ────────────────────
        parsed = _parse_relation_output(out, txt_name)
        file_summary = f"{txt_name.replace('.txt', '')}  {parsed['summary_line']}"
        file_summaries.append(file_summary)
        log_cb(f"  → {parsed['summary_line']}")

        if parsed["has_issues"]:
            errors_summary.append(file_summary)

        # ── 7. Save separate log with summary appended ──────────
        rel_log_name = f"RELATION_{node_name}_{txt_name.replace('.txt', '')}.txt"
        rel_log_path = os.path.join(log_dir, rel_log_name)
        try:
            with open(rel_log_path, "w", encoding="utf-8") as f:
                f.write(f"Relation Log — {node_name}\n")
                f.write(f"File: {txt_name}\n")
                f.write(f"Path: {txt_path}\n")
                f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 72 + "\n\n")
                f.write(out)
                f.write("\n\n")
                f.write("=" * 72 + "\n")
                f.write("SUMMARY\n")
                f.write("=" * 72 + "\n")
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
            log_cb(f"Log saved: {rel_log_name}")
        except Exception as exc:
            log_cb(f"Failed to save log {rel_log_name}: {exc}")

    # ── Overall summary ─────────────────────────────────────────
    # Save a combined summary file
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

    # Parse: check if any line has DISABLED or if no MOs found
    lines = out.split("\n")
    mme_lines = [l for l in lines if "TermPointToMme" in l or "SctpEndpoint" in l]

    if not mme_lines:
        msg = f"No MME entries found in 'st mme' output for {node_name}."
        log_cb(f"✗ {msg}")
        if wait_for_user:
            wait_for_user(msg)
        return False, all_output

    disabled = [l.strip() for l in mme_lines if "DISABLED" in l]

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
            disabled = [l.strip() for l in mme_lines if "DISABLED" in l]
            if disabled:
                disabled_list = "\n".join(disabled)
                msg = (
                    f"MME is DISABLED, check the IP configuration or Transport.\n\n"
                    f"Disabled entries:\n{disabled_list}"
                )

    log_cb(f"✓ All MME connections ENABLED for {node_name} ({len(mme_lines)} entries).")
    return True, all_output


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
    ``<local_dump_dir>/DUMP/<shortcode>/<nodename>_modump.zip``.

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
    local_dir = os.path.join(local_dump_dir, "DUMP", shortcode)
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
      3. SFTP download the zip to local DUMP/<shortcode>/<nodename>_cmdump.zip

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
            log_cb(f"Export still in progress (attempt {attempt}/{max_attempts}), waiting 30s...")
            time.sleep(30)

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
    local_dir = os.path.join(local_dump_dir, "DUMP", shortcode)
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
