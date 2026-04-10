"""
SSH connection and AMOS/moshell command execution via Paramiko.
"""
import re
import time
import logging
import paramiko
from typing import Optional

from config_loader import AppConfig

logger = logging.getLogger(__name__)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]|\x1b\(B")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


class MoshellSession:
    """Manages an SSH connection and interactive AMOS/moshell session."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self._connected = False
        self._in_moshell = False
        self._node_connected = False  # True after first command establishes node connection

    def connect(self):
        """Establish SSH connection to the server."""
        ssh_cfg = self.config.ssh
        logger.info(f"Connecting to {ssh_cfg.host}:{ssh_cfg.port} as {ssh_cfg.username}...")

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=ssh_cfg.host,
            port=ssh_cfg.port,
            username=ssh_cfg.username,
            password=ssh_cfg.password,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )

        # Open interactive shell
        self.shell = self.client.invoke_shell(
            term="xterm",
            width=200,
            height=50,
        )
        self.shell.settimeout(self.config.moshell.command_timeout)
        self._connected = True

        # Wait for initial shell prompt
        self._read_until_prompt(shell_prompt=True)
        logger.info("SSH connection established.")

    def login_moshell(self):
        """Login to AMOS/moshell on the connected server."""
        if not self._connected:
            raise RuntimeError("Not connected to SSH. Call connect() first.")

        login_cmd = self.config.moshell.login_command
        logger.info(f"Starting AMOS: {login_cmd}")
        self._send_command(login_cmd)

        # Wait for AMOS prompt (can take a while on first connect)
        output = self._read_until_prompt(moshell_prompt=True, timeout=90)
        self._in_moshell = True
        logger.info("AMOS session started.")

        # Run 'lt all' to load MO tree (required before running commands)
        logger.info("Loading MO tree: lt all")
        self._send_command("lt all")
        lt_output = self._read_until_prompt(moshell_prompt=True, timeout=120)
        self._node_connected = True
        logger.info("MO tree loaded. Ready for commands.")

        return output + lt_output

    def run_command(self, command: str, timeout: int = None) -> str:
        """
        Run a single AMOS command and return the full text output.

        The first command triggers the node connection (takes ~20-30s).
        Subsequent commands are fast.

        Args:
            command: The AMOS/moshell command to execute
            timeout: Optional timeout override in seconds

        Returns:
            Full cleaned text output of the command
        """
        if not self._in_moshell:
            raise RuntimeError("Not in AMOS session. Call login_moshell() first.")

        # First command takes longer because AMOS connects to the node
        if not self._node_connected:
            timeout = timeout or 120
        else:
            timeout = timeout or self.config.moshell.command_timeout

        logger.info(f"Running command: {command}")

        self._send_command(command)
        output = self._read_until_prompt(moshell_prompt=True, timeout=timeout)

        if not self._node_connected:
            self._node_connected = True
            logger.info("Node connection established (first command complete).")

        # Clean up the output
        output = strip_ansi(output)
        prompt = self.config.moshell.prompt_pattern

        lines = output.split("\n")

        # Remove the trailing prompt line (we'll put prompt+command at the top instead)
        if lines and prompt in lines[-1]:
            lines = lines[:-1]

        # Build output: prompt+command at top, then the rest
        # Find and remove the command echo line (first occurrence)
        body_lines = []
        found_echo = False
        for line in lines:
            if not found_echo and command.strip() in line:
                found_echo = True
                continue
            body_lines.append(line)

        # Prepend prompt + command as first line
        header = f"{prompt} {command}"
        output = header + "\n" + "\n".join(body_lines)

        # Remove trailing whitespace but keep structure
        output = output.rstrip()

        logger.debug(f"Command output ({len(output)} chars)")
        return output

    def disconnect(self):
        """Close AMOS and SSH connection."""
        if self._in_moshell and self.shell:
            try:
                self._send_command("exit")
                time.sleep(2)
                self._in_moshell = False
                self._node_connected = False
            except Exception as e:
                logger.warning(f"Error exiting AMOS: {e}")

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
        logger.info("Disconnected.")

    def _send_command(self, command: str):
        """Send a command through the shell channel."""
        self.shell.send(command + "\n")
        time.sleep(0.5)

    def _read_until_prompt(
        self,
        shell_prompt: bool = False,
        moshell_prompt: bool = False,
        timeout: int = 30,
    ) -> str:
        """
        Read shell output until a prompt is detected.

        Args:
            shell_prompt: Wait for a shell prompt ($ or #)
            moshell_prompt: Wait for AMOS/moshell prompt pattern
            timeout: Timeout in seconds

        Returns:
            All output read from the shell
        """
        buffer = ""
        start_time = time.time()
        prompt_pattern = self.config.moshell.prompt_pattern

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning(f"Timeout after {timeout}s waiting for prompt")
                break

            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buffer += chunk

                # Strip ANSI for prompt detection
                clean_buffer = strip_ansi(buffer)
                last_line = clean_buffer.strip().split("\n")[-1].strip()

                # Check for prompt
                if moshell_prompt and prompt_pattern in last_line:
                    # Wait a bit more to collect any trailing data
                    time.sleep(0.5)
                    while self.shell.recv_ready():
                        buffer += self.shell.recv(65536).decode("utf-8", errors="replace")
                    break
                if shell_prompt and _is_shell_prompt(last_line):
                    break
            else:
                time.sleep(0.3)

        return buffer

    def __enter__(self):
        self.connect()
        self.login_moshell()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


def _is_shell_prompt(line: str) -> bool:
    """Check if line looks like a shell prompt."""
    return line.endswith("$") or line.endswith("#") or line.endswith(">")
