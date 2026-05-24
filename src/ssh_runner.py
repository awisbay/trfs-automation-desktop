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

# A stale AMOS prompt looks like `<NODE_DN>>`. Node DNs used here are long
# (>=8 chars) AND always contain at least one underscore or digit. A plain
# gateway shell like `tigos>` won't match — it has no digit/underscore.
_AMOS_PROMPT_RE = re.compile(r"(?=[A-Za-z0-9_\-]*[\d_])[A-Za-z0-9_\-]{6,}>\s*$")

# Commands that legitimately take longer than the default `command_timeout`.
# Matched against the command text, case-insensitive. The read loop still
# returns the INSTANT the prompt appears — this only raises the safety net.
_SLOW_COMMAND_PREFIXES = ("cvcu", "pmxhetd", "pmx", "alt", "get ", "st ")
_SLOW_COMMAND_TIMEOUT = 120


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


def _looks_like_amos_prompt(line: str) -> bool:
    """True if *line* is a stale AMOS/moshell prompt (e.g. ``MIN3117>``)."""
    return bool(_AMOS_PROMPT_RE.search(line.rstrip()))


def _is_slow_command(command: str) -> bool:
    """Heuristic: these commands can pull large counter/MO tables and need
    a generous timeout (default 120s)."""
    cmd = command.strip().lower()
    return any(cmd.startswith(p) for p in _SLOW_COMMAND_PREFIXES)


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
        try:
            import ssh_registry
            ssh_registry.register(self)
        except Exception:
            pass

        # Wait for initial shell prompt
        self._read_until_prompt(shell_prompt=True)
        logger.info("SSH connection established.")

        # ── Stale-AMOS guard ────────────────────────────────────────────
        # The AMOS gateway (port 5023) pools backend sessions per user.
        # If a previous paramiko channel for this user was closed while
        # still inside `amos <node>`, the next invoke_shell() attaches to
        # that SAME backend shell — we land at `<previous_node>>` instead
        # of the gateway shell. _is_shell_prompt() accepts `>` leniently,
        # so we think we're at shell and then send `amos <our_node>` from
        # inside the previous node's AMOS (which silently no-ops). Every
        # subsequent command runs on the WRONG node — this is what was
        # producing GSM screenshots full of LTE/NR content.
        #
        # Detect it here, send `exit` to drop back to the gateway shell,
        # then proceed. Safe on a genuinely-fresh shell: the gateway
        # prompt doesn't match _AMOS_PROMPT_RE, so we skip the exit.
        self._reset_if_stale_amos()

    def login_moshell(self):
        """Login to AMOS/moshell on the connected server."""
        if not self._connected:
            raise RuntimeError("Not connected to SSH. Call connect() first.")

        login_cmd = self.config.moshell.login_command
        expected_node = self.config.site.node_name
        logger.info(f"Starting AMOS: {login_cmd}")
        self._send_command(login_cmd)

        # STRICT prompt wait. Creds-aware so if the node prompts for
        # rbs/rbs during `amos <node>` or `lt all` we auto-answer.
        output = self._read_until_prompt_with_creds(timeout=120)

        # Same sanity check as switch_node — make sure the prompt really
        # is the expected node, not some stale AMOS from a pooled backend.
        clean_tail = strip_ansi(output).rstrip()
        last_line = clean_tail.split("\n")[-1] if clean_tail else ""
        if expected_node and f"{expected_node}>" not in last_line:
            raise RuntimeError(
                f"[login_moshell] Node mismatch after `{login_cmd}`: "
                f"expected prompt to contain {expected_node!r}+'>' but last "
                f"line was {last_line!r}. REFUSING to run commands on the "
                f"wrong node."
            )
        self._in_moshell = True
        logger.info(f"AMOS session started on {expected_node}.")

        # Run 'lt all' to load MO tree (required before running commands)
        logger.info("Loading MO tree: lt all")
        self._send_command("lt all")
        lt_output = self._read_until_prompt_with_creds(timeout=300)
        self._node_connected = True
        logger.info("MO tree loaded. Ready for commands.")

        return output + lt_output

    def switch_node(self, new_node_name: str) -> str:
        """Switch this live SSH session from the current AMOS node to a new one.

        Sequence:
          1. Send ``exit`` inside AMOS → returns to the gateway shell.
          2. Wait for a real gateway shell prompt (not an AMOS prompt).
          3. Update ``self.config`` so login_command + prompt_pattern
             target *new_node_name*.
          4. Send ``amos <new_node_name>`` (creds-aware reader).
          5. Send ``lt all`` and wait for the node to load.

        This avoids opening a new SSH connection — the gateway keeps one
        clean shell and we just swap the moshell process running on top
        of it. Result: no session pooling race, no stale AMOS state, no
        gateway re-auth.

        Returns the combined output of ``amos`` + ``lt all``.
        """
        if not self._connected:
            raise RuntimeError("Not connected to SSH. Call connect() first.")

        logger.info(f"Switching AMOS session to node: {new_node_name}")

        # Step 1 & 2 — drop out of current AMOS back to gateway shell
        if self._in_moshell:
            logger.info("Exiting current AMOS session...")
            self._send_command("exit")
            self._wait_for_gateway_shell(timeout=30)
            self._in_moshell = False
            self._node_connected = False

        # Step 3 — rewrite config so prompt_pattern / login_command match
        # the new node. Both _read_until_prompt_with_creds and run_command
        # pull the pattern out of self.config, so updating here is enough.
        self.config.site.node_name = new_node_name
        self.config.moshell.login_command = f"amos {new_node_name}"
        self.config.moshell.prompt_pattern = f"{new_node_name}>"

        # Step 4 & 5 — log into the new node and load its MO tree.
        # _read_until_prompt_with_creds is STRICT — it only returns when
        # the prompt line contains `<new_node_name>>`. If AMOS can't reach
        # the node (wrong name, credentials, stale session, etc.) it will
        # raise TimeoutError rather than silently land on the wrong node.
        logger.info(f"Starting AMOS: amos {new_node_name}")
        self._send_command(f"amos {new_node_name}")
        try:
            amos_output = self._read_until_prompt_with_creds(timeout=120)
        except TimeoutError as exc:
            logger.error(
                f"[switch_node] Failed to enter AMOS for {new_node_name}: {exc}"
            )
            raise

        # Sanity check: verify the last visible prompt really is the new
        # node. If something weird happened (lenient-prompt leftovers, a
        # gateway re-login, etc.) we abort loudly instead of running the
        # caller's commands on whatever happens to be on the other end.
        clean_tail = strip_ansi(amos_output).rstrip()
        last_line = clean_tail.split("\n")[-1] if clean_tail else ""
        if f"{new_node_name}>" not in last_line:
            raise RuntimeError(
                f"[switch_node] Node mismatch after `amos {new_node_name}`: "
                f"expected prompt to contain {new_node_name!r}+'>' but last "
                f"line was {last_line!r}. REFUSING to run commands on the "
                f"wrong node."
            )
        self._in_moshell = True

        logger.info(f"AMOS confirmed on {new_node_name}. Loading MO tree: lt all")
        self._send_command("lt all")
        lt_output = self._read_until_prompt_with_creds(timeout=300)
        self._node_connected = True
        logger.info(f"Switch complete — session is now on {new_node_name}.")
        return amos_output + lt_output

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

        # Pick an appropriate timeout. The read loop still exits the instant
        # the prompt appears — timeout is only the safety net. So giving
        # PIM / cvcu / alt etc. 120s costs us nothing on fast runs but stops
        # the short default (often 30-60s) from cutting them off mid-output.
        if timeout is None:
            if not self._node_connected:
                # First command after login triggers node connection — slow.
                timeout = 120
            elif _is_slow_command(command):
                timeout = _SLOW_COMMAND_TIMEOUT
                logger.debug(f"Using extended timeout ({timeout}s) for slow command: {command}")
            else:
                timeout = self.config.moshell.command_timeout

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
        """Close AMOS and SSH connection.

        Cleanly tears down the BACKEND session, not just the paramiko
        channel — otherwise the AMOS gateway pools the backend shell and
        the next `connect()` lands right back in our AMOS session for the
        old node (see the stale-AMOS guard in connect()).

        Sequence:
          1. If in AMOS, send `exit` and WAIT for the gateway shell prompt
             (don't just sleep 2s — paramiko closing the socket before the
             shell actually returns is what leaves the backend session
             pooled).
          2. From the gateway shell, send `exit` so the gateway drops our
             backend login entirely.
          3. Close the paramiko channel + client.
        """
        # Step 1 — drop out of AMOS back to the gateway shell.
        if self._in_moshell and self.shell:
            try:
                self._send_command("exit")
                # Wait up to 15s for the gateway shell prompt. If we time
                # out we still proceed — the follow-up `exit` + channel
                # close below will eventually kill the backend process.
                self._wait_for_gateway_shell(timeout=15)
                self._in_moshell = False
                self._node_connected = False
            except Exception as e:
                logger.warning(f"Error exiting AMOS cleanly: {e}")

        # Step 2 — log out of the gateway so its pooled shell dies.
        if self._connected and self.shell:
            try:
                self._send_command("exit")
                # Small drain — don't wait for a prompt, the shell is gone.
                time.sleep(0.3)
                try:
                    while self.shell.recv_ready():
                        self.shell.recv(65536)
                except Exception:
                    pass
            except Exception:
                pass

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
        logger.info("Disconnected.")

    def _reset_if_stale_amos(self):
        """If the initial prompt after connect looks like a stale AMOS
        prompt (``<NODE_DN>>``), send ``exit`` to drop back to the real
        gateway shell before ``login_moshell()`` fires off ``amos <node>``.

        No-op on a genuinely-fresh gateway shell: the gateway prompt
        (usually ``$``/``#`` or a short token like ``rcli>``) doesn't
        match ``_AMOS_PROMPT_RE``.
        """
        try:
            # Read whatever is sitting in the buffer without waiting.
            buf = ""
            drained = True
            while drained:
                drained = False
                if self.shell.recv_ready():
                    buf += self.shell.recv(65536).decode("utf-8", errors="replace")
                    drained = True
            if buf:
                # We already consumed past the prompt in connect(). Put
                # the data back by logging it — we don't need it.
                pass

            # Probe: send a newline, read the resulting prompt line.
            self.shell.send("\n")
            probe = ""
            start = time.time()
            while time.time() - start < 3:
                if self.shell.recv_ready():
                    probe += self.shell.recv(65536).decode("utf-8", errors="replace")
                    clean = strip_ansi(probe).rstrip()
                    if clean:
                        # Take the last non-empty line as the prompt
                        last = clean.split("\n")[-1].rstrip()
                        if last.endswith((">", "$", "#")):
                            break
                else:
                    time.sleep(0.05)

            clean = strip_ansi(probe).rstrip()
            last_line = clean.split("\n")[-1].rstrip() if clean else ""
            if _looks_like_amos_prompt(last_line):
                logger.warning(
                    f"Stale AMOS prompt detected on fresh channel: {last_line!r} "
                    f"— sending `exit` to reset to gateway shell."
                )
                self.shell.send("exit\n")
                # Wait for gateway shell (non-AMOS) prompt.
                self._wait_for_gateway_shell(timeout=15)
            else:
                logger.debug(f"Gateway shell prompt OK: {last_line!r}")
        except Exception as exc:
            logger.warning(f"Stale-AMOS reset check failed: {exc} — proceeding anyway")

    def _wait_for_gateway_shell(self, timeout: int = 15) -> str:
        """Wait for a gateway shell prompt — defined as a line NOT matching
        the AMOS prompt regex. Typical gateway shells end in ``$``/``#`` or
        something like ``rcli>`` (short token, won't match ``_AMOS_PROMPT_RE``).
        """
        buffer = ""
        start = time.time()
        POLL = 0.05
        while time.time() - start < timeout:
            if self.shell.recv_ready():
                buffer += self.shell.recv(65536).decode("utf-8", errors="replace")
                clean = strip_ansi(buffer).rstrip()
                if clean:
                    last_line = clean.split("\n")[-1].rstrip()
                    if last_line and not _looks_like_amos_prompt(last_line):
                        if last_line.endswith((">", "$", "#")):
                            return buffer
            else:
                time.sleep(POLL)
        logger.warning(
            f"Timeout after {timeout}s waiting for gateway shell prompt. "
            f"Last 200 chars: {strip_ansi(buffer)[-200:]!r}"
        )
        return buffer

    def _send_command(self, command: str):
        """Send a command through the shell channel.

        No sleep after send — the next read loop polls at 50 ms anyway and
        exits the instant the `>` prompt is seen. Zero artificial delay
        between commands.
        """
        self.shell.send(command + "\n")

    def _read_until_prompt_with_creds(self, timeout: int = 120) -> str:
        """Read until the configured AMOS prompt is seen. Answers ``rbs``/
        ``rbs`` if the node asks for credentials during ``amos <node>`` /
        ``lt all``.

        **STRICT match**: we require ``self.config.moshell.prompt_pattern``
        (e.g. ``<this_node>>``) to appear in the final line. Lenient
        ``>/$/#`` matching was what let a `amos <gsm>` command silently land
        back at the previous LTE node's prompt — subsequent commands ran on
        the wrong node, and GSM screenshots got filled with LTE output.

        If the expected pattern never appears within *timeout*, we log a
        full diagnostic (last 400 chars of what we actually saw) and raise
        so the caller doesn't proceed blindly.
        """
        buffer = ""
        start = time.time()
        prompt_pattern = self.config.moshell.prompt_pattern
        sent_user = False
        sent_pass = False
        POLL = 0.05
        last_log_len = 0
        while True:
            if time.time() - start > timeout:
                clean = strip_ansi(buffer)
                tail = clean[-400:].replace("\n", "\\n")
                logger.error(
                    f"Timeout after {timeout}s waiting for AMOS prompt "
                    f"{prompt_pattern!r}. Last 400 chars: {tail!r}"
                )
                raise TimeoutError(
                    f"AMOS prompt {prompt_pattern!r} never appeared within "
                    f"{timeout}s. The session is NOT on the expected node."
                )
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buffer += chunk
                clean = strip_ansi(buffer)
                if len(clean) - last_log_len > 2000:
                    last_log_len = len(clean)
                    logger.debug(f"shell: {len(clean)} chars received so far")
                tail = clean[-500:].lower()
                if not sent_user and ("enter username" in tail or "username:" in tail):
                    logger.info("lt all: sending username 'rbs'")
                    self._send_command("rbs")
                    sent_user = True
                    continue
                if sent_user and not sent_pass and "password" in tail:
                    logger.info("lt all: sending password")
                    self._send_command("rbs")
                    sent_pass = True
                    continue
                last_line = clean.rstrip().split("\n")[-1].rstrip() if clean.strip() else ""
                # STRICT: the line must contain the configured node prompt.
                # No more lenient `>`/`$`/`#` fallback — that was the bug.
                if last_line and prompt_pattern and prompt_pattern in last_line:
                    drained = True
                    while drained:
                        drained = False
                        if self.shell.recv_ready():
                            buffer += self.shell.recv(65536).decode("utf-8", errors="replace")
                            drained = True
                    break
            else:
                time.sleep(POLL)
        return buffer

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
        POLL = 0.05

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                clean = strip_ansi(buffer)
                tail = clean[-400:].replace("\n", "\\n")
                logger.warning(
                    f"Timeout after {timeout}s waiting for prompt "
                    f"(expected pattern={prompt_pattern!r}). Last 400 chars: {tail!r}"
                )
                break

            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                buffer += chunk

                # Strip ANSI for prompt detection
                clean_buffer = strip_ansi(buffer)
                last_line = (
                    clean_buffer.rstrip().split("\n")[-1].rstrip()
                    if clean_buffer.strip() else ""
                )

                # STRICT moshell match — require the configured node prompt
                # (e.g. `<this_node>>`) to be present. Exits immediately the
                # moment it's seen. If we silently end up on the wrong node,
                # the prompt won't match and we'll time out with diagnostic
                # output instead of happily returning the wrong node's data.
                if moshell_prompt and last_line and prompt_pattern and prompt_pattern in last_line:
                    drained = True
                    while drained:
                        drained = False
                        if self.shell.recv_ready():
                            buffer += self.shell.recv(65536).decode("utf-8", errors="replace")
                            drained = True
                    break
                if shell_prompt and _is_shell_prompt(last_line):
                    break
            else:
                time.sleep(POLL)

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
