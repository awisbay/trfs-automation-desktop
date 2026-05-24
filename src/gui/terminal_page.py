"""
Multi-tab interactive SSH terminal — MobaXterm-style.

A single terminal window with tabs at the top. Each tab is an
independent paramiko interactive shell to a host:port. Each tab has:

  * a scrolling output area (monospaced, ANSI-aware, keyword-coloured)
  * a command input box at the bottom
  * background reader thread → UI queue → main thread renders

Coloring rules applied on each line:
  * red   — ``error``, ``failed``, ``denied``, ``traceback``
  * green — ``ok``, ``success``, ``passed``, ``completed``, ``connected``
  * amber — ``warning``, ``warn``, ``deprecated``
  * cyan  — IP addresses, port numbers
  * blue  — shell prompts (lines ending in ``$``, ``#``, ``>``)
  * grey  — ANSI escape sequences are stripped, but ANSI colour codes
            we recognise are translated to Flet text colour.

Usage:
  page.go("/terminal") — opens the terminal route. The first tab is
  pre-populated with the SSH credentials from the form. The "+" button
  on the tab bar opens a small dialog to add more sessions.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
from inspect import iscoroutine as asyncio_iscoroutine
from typing import Optional

import flet as ft
import paramiko

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gui.theme import (
    ACCENT,
    ACCENT_WARM,
    BG_BOTTOM,
    BG_TOP,
    BORDER,
    DANGER,
    INFO,
    PANEL,
    PANEL_RAISED,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    panel,
    secondary_button_style,
)

logger = logging.getLogger(__name__)


# ── Colour palette (MobaXterm-ish) ──────────────────────────────
TERM_BG = "#0B1B26"
TERM_FG = "#E0E6EB"
TERM_PROMPT = "#62D6FF"
TERM_ERROR = "#FF6B6B"
TERM_OK = "#7BE495"
TERM_WARN = "#F4C95D"
TERM_NUMBER = "#62D6FF"
TERM_KEYWORD = "#D58BFF"
TERM_MUTED = "#6E8294"
TERM_CURSOR = "#E0E6EB"
TERMINAL_FONT_FAMILY = "Consolas"
TERMINAL_FONT_SIZE = 12

# Patterns for keyword colouring. Each tuple is (regex, colour). The
# regex is searched against an ANSI-stripped line; the first match
# wins for that token (longest-match wins because we sort regions).
KEYWORD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:error|errors|failed|failure|denied|traceback|fatal|critical)\b", re.I), TERM_ERROR),
    (re.compile(r"\b(?:ok|success|successful|passed|completed|connected|established|installed|verified)\b", re.I), TERM_OK),
    (re.compile(r"\b(?:warning|warn|deprecated|notice|caution)\b", re.I), TERM_WARN),
    (re.compile(r"\b(?:set|get|run|create|delete|update|enable|disable|start|stop|restart|show|list)\b", re.I), TERM_KEYWORD),
    # IPv4
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"), TERM_NUMBER),
    # Bare numbers (4+ digits) — e.g. timestamps, port numbers
    (re.compile(r"\b\d{4,}\b"), TERM_NUMBER),
]

# Comprehensive ANSI escape stripper. We don't try to render every
# attribute (colour codes etc.) — we just remove every escape so the
# text comes out clean. Without this, bash's title-setting OSC
# sequences (``\x1b]0;<title>\x07``) and charset-selection
# (``\x1b(B``) leak into the visible output as garbage like
# ``]0;user@host:~`` or a stray ``(B``.
ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;?]*[A-Za-z]"                # CSI: cursor moves, colours
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC: window title, hyperlink
    r"|[()*+\-./][A-Z0-9]"               # charset selection: \x1b(B etc
    r"|P[^\x1b]*\x1b\\"                  # DCS
    r"|_[^\x1b]*\x1b\\"                  # APC
    r"|\^[^\x1b]*\x1b\\"                 # PM
    r"|[=>78cDEHMNOZ\\]"                 # simple ESC sequences
    r")"
)
# Stray BEL bytes (`\x07`) sometimes slip through unbalanced OSC
# sequences — strip them too so they don't render as a tofu glyph.
BEL_RE = re.compile(r"\x07+")


def strip_ansi(s: str) -> str:
    return BEL_RE.sub("", ANSI_RE.sub("", s))


def _apply_backspaces(text: str) -> str:
    """Process ``\\b`` (BS, 0x08) bytes the way a real terminal does:
    each backspace erases the preceding character on the same line.
    Without this, the bash echo for a backspace keystroke (``\\b \\b``)
    appears as visible tofu/square glyphs in the output."""
    if "\b" not in text:
        return text
    out: list[str] = []
    for ch in text:
        if ch == "\b":
            if out and out[-1] != "\n":
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


# Drop the control characters we don't otherwise handle. We keep
# tab/newline (handled elsewhere). Anything else in the C0/C1 range
# would render as a tofu square in Flet so it's better stripped.
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    return CTRL_RE.sub("", text)


def normalize_terminal_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "")
    text = strip_ansi(text)
    text = _apply_backspaces(text)
    return strip_control_chars(text)


def line_to_spans(line: str) -> list[ft.TextSpan]:
    """Convert one line of plain text into a list of coloured TextSpans.

    Strategy: find all keyword/number regions, render the line as a
    mix of "default-coloured" runs and coloured runs. Prompt lines
    (ending in ``$``, ``#``, ``>``) get the whole line painted prompt
    blue.
    """
    line = strip_ansi(line)
    if not line:
        return [ft.TextSpan(" ", style=ft.TextStyle(color=TERM_FG))]

    # Whole-line prompt detection: cheap and high-signal.
    stripped = line.rstrip()
    if stripped and stripped[-1] in ("$", "#") and len(stripped) <= 200:
        return [ft.TextSpan(line, style=ft.TextStyle(color=TERM_PROMPT))]
    # AMOS prompt: NODE> on its own line
    if re.match(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$", stripped):
        return [ft.TextSpan(line, style=ft.TextStyle(color=TERM_PROMPT))]

    # Collect non-overlapping regions: (start, end, colour)
    regions: list[tuple[int, int, str]] = []
    for pattern, colour in KEYWORD_PATTERNS:
        for m in pattern.finditer(line):
            regions.append((m.start(), m.end(), colour))

    if not regions:
        return [ft.TextSpan(line, style=ft.TextStyle(color=TERM_FG))]

    # Resolve overlaps: prefer earlier-pattern (errors > success > ...)
    # by sorting by (start, then position in pattern list — already
    # implicit because we iterate in order). For simplicity, drop any
    # region that overlaps an earlier-kept region.
    regions.sort(key=lambda r: (r[0], -r[1]))
    kept: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, colour in regions:
        if start >= last_end:
            kept.append((start, end, colour))
            last_end = end

    spans: list[ft.TextSpan] = []
    cursor = 0
    for start, end, colour in kept:
        if start > cursor:
            spans.append(
                ft.TextSpan(
                    line[cursor:start],
                    style=ft.TextStyle(color=TERM_FG),
                )
            )
        spans.append(
            ft.TextSpan(
                line[start:end],
                style=ft.TextStyle(color=colour, weight=ft.FontWeight.W_500),
            )
        )
        cursor = end
    if cursor < len(line):
        spans.append(
            ft.TextSpan(line[cursor:], style=ft.TextStyle(color=TERM_FG))
        )
    return spans


# ── One SSH session = one tab ───────────────────────────────────
class TerminalSession:
    """Owns a paramiko interactive shell + reader thread + UI controls
    for a single tab. Lifecycle:

      * ``connect()`` — opens SSH + invoke_shell, starts reader thread
      * reader thread pushes raw chunks into ``self.rx_queue``
      * UI poller (driven by main page tick) drains the queue and
        appends coloured spans to ``self.output_column``
      * ``send(line)`` — writes to channel + echoes to UI
      * ``close()`` — stops reader, closes SSH
    """

    _id_counter = 0

    def __init__(
        self,
        page: ft.Page,
        host: str, port: int, username: str, password: str,
        title: str,
        on_status_change=None,
    ):
        TerminalSession._id_counter += 1
        self.session_id = TerminalSession._id_counter
        self.page = page
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.title = title
        self.on_status_change = on_status_change

        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.alive = False
        self.rx_queue: queue.Queue[str] = queue.Queue()
        self._line_buffer = ""
        self._local_echo_buffer = ""
        self._optimistic_submitted_line: Optional[str] = None

        # Transcript file: every byte received from the shell is
        # appended here, so output that ages out of the in-memory
        # 5000-line buffer is still on disk for review. Path is
        # filled in by ``connect()``.
        self._transcript_path: Optional[str] = None
        self._transcript_fp = None
        self._transcript_lock = threading.Lock()
        # The "live tail" Text control shows the partial last line of
        # the remote shell — typically the PS1 prompt
        # (``EWISBAY@svc-3-scripting(lhgenm1) ~]$ ``), which never
        # ends with a newline because the shell is waiting for input.
        # Without this, the prompt would stay buffered invisibly until
        # the next newline comes through, making the terminal look
        # "dead" right after connect.
        self._live_tail_text: Optional[ft.Text] = None
        self._scroll_requested = False
        # ── UI controls ─────────────────────────────────────────
        # Output: a scrollable column of Text controls (one per line).
        # Re-using ft.Text per line keeps each line's TextSpans isolated
        # and lets Flet paint them quickly without re-rendering the
        # whole transcript on every append.
        self.output_column = ft.Column(
            controls=[],
            spacing=0,
            scroll=ft.ScrollMode.ALWAYS,
            auto_scroll=False,
            expand=True,
        )
        # NOTE: in the new typing model, all keystrokes go straight to
        # the SSH channel via the page-level keyboard handler in
        # ``TerminalPage._on_page_keyboard``. No bottom input field is
        # needed — typing is "inside" the terminal output area, and
        # the remote shell echoes the characters back so the user
        # sees them inline with the prompt (just like a real terminal).
        self.status_chip = ft.Container(
            content=ft.Text(
                "disconnected", size=11, color=TERM_MUTED,
                weight=ft.FontWeight.BOLD,
            ),
            bgcolor=ft.Colors.with_opacity(0.10, TERM_MUTED),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        )
        self.tab_view = self._build_tab_view()

    # ── UI construction ─────────────────────────────────────────
    def _build_tab_view(self) -> ft.Control:
        # Output area only — no separate input field. The
        # ``GestureDetector`` wrapper only handles right-click (paste
        # clipboard → send to shell). Left-click goes through to the
        # inner Text controls so the operator can still select text
        # and Ctrl+C to copy. ``selectable=True`` on every line.
        output_panel = ft.Container(
            content=ft.GestureDetector(
                content=ft.Container(
                    content=self.output_column,
                    bgcolor=TERM_BG,
                    padding=ft.Padding(left=10, top=8, right=10, bottom=8),
                    expand=True,
                ),
                on_secondary_tap=self._on_paste_clipboard,
            ),
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            expand=True,
        )

        save_btn = ft.IconButton(
            icon=ft.Icons.SAVE_OUTLINED,
            icon_size=18,
            icon_color=TERM_MUTED,
            tooltip="Save visible transcript to a file",
            on_click=self._on_save_transcript,
        )

        meta_row = ft.Row(
            [
                ft.Icon(ft.Icons.TERMINAL, size=14, color=TERM_MUTED),
                ft.Text(
                    f"{self.username}@{self.host}:{self.port}",
                    size=12, color=TERM_MUTED,
                ),
                ft.Container(expand=True),
                ft.Text(
                    "type to send · Ctrl+V or right-click to paste · select+Ctrl+C to copy",
                    size=10, color=TERM_MUTED, italic=True,
                ),
                ft.Container(width=4),
                save_btn,
                ft.Container(width=4),
                self.status_chip,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=6,
        )

        return ft.Column(
            [meta_row, output_panel],
            spacing=8,
            expand=True,
        )

    def _on_save_transcript(self, e) -> None:
        """Save the currently-visible buffer to a user-chosen file.

        Note: the full session is *also* being written automatically
        to ``LOG/TERMINAL/term_<host>_<ts>.log`` since the moment the
        session connected. This button is for grabbing a clean copy
        of what's on screen right now (after backspace processing,
        ANSI strip, etc.) into a path the user picks.
        """
        # We can't easily route through Flet's FilePicker from inside
        # the session class without a back-reference to the page —
        # which we have. Use page.overlay to host a transient picker.
        picker = ft.FilePicker(on_result=self._save_picker_result)
        # Stash on the session so the result handler can find it.
        self._save_picker = picker
        try:
            self.page.overlay.append(picker)
            self.page.update()
            safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", self.host)
            default_name = (
                f"terminal_{safe_host}_{time.strftime('%Y%m%d_%H%M%S')}.log"
            )
            picker.save_file(
                dialog_title="Save terminal transcript",
                file_name=default_name,
                allowed_extensions=["log", "txt"],
            )
        except Exception as exc:
            self._append_system_line(
                f"Save dialog failed: {exc}", colour=TERM_ERROR,
            )

    def _save_picker_result(self, e) -> None:
        path = getattr(e, "path", None)
        if not path:
            return
        ok = self.save_visible_transcript_to(path)
        if ok:
            self._append_system_line(
                f"Saved transcript → {path}", colour=TERM_OK,
            )
        else:
            self._append_system_line(
                f"Save failed for {path}", colour=TERM_ERROR,
            )

    # ── Transcript file ────────────────────────────────────────
    def _open_transcript(self) -> None:
        """Open a per-session transcript file under
        ``<app_dir>/LOG/TERMINAL/``. Best-effort — failures here just
        disable the on-disk log; the in-memory buffer keeps working."""
        try:
            from app_path import get_app_dir
            log_dir = os.path.join(get_app_dir(), "LOG", "TERMINAL")
            os.makedirs(log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", self.host)
            self._transcript_path = os.path.join(
                log_dir, f"term_{safe_host}_{ts}.log",
            )
            self._transcript_fp = open(
                self._transcript_path, "w", encoding="utf-8", buffering=1,
            )
            header = (
                f"# TRFS terminal transcript\n"
                f"# Session: {self.username}@{self.host}:{self.port}\n"
                f"# Opened: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                + "=" * 72 + "\n\n"
            )
            self._transcript_fp.write(header)
        except Exception as exc:
            logger.warning(f"[term] transcript file open failed: {exc}")
            self._transcript_path = None
            self._transcript_fp = None

    def _write_transcript_chunk(self, chunk: str) -> None:
        if self._transcript_fp is None or not chunk:
            return
        try:
            with self._transcript_lock:
                self._transcript_fp.write(chunk)
        except Exception:
            # Don't let a write failure tear the session down — just
            # quietly disable further writes.
            try:
                self._transcript_fp.close()
            except Exception:
                pass
            self._transcript_fp = None

    def _close_transcript(self) -> None:
        if self._transcript_fp is not None:
            try:
                with self._transcript_lock:
                    self._transcript_fp.write(
                        f"\n\n# Closed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    self._transcript_fp.close()
            except Exception:
                pass
            self._transcript_fp = None

    def save_visible_transcript_to(self, path: str) -> bool:
        """Dump everything that's currently rendered (the in-memory
        buffer) to ``path``. Used by the Save button. Returns True
        on success."""
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# TRFS terminal — visible transcript\n"
                    f"# Session: {self.username}@{self.host}:{self.port}\n"
                    f"# Saved: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    + "=" * 72 + "\n\n"
                )
                for ctrl in self.output_column.controls:
                    line_text = ""
                    try:
                        spans = getattr(ctrl, "spans", None)
                        if spans:
                            line_text = "".join(
                                getattr(sp, "text", "") for sp in spans
                            )
                        else:
                            line_text = getattr(ctrl, "value", "") or ""
                    except Exception:
                        line_text = ""
                    f.write(line_text + "\n")
            return True
        except Exception as exc:
            logger.warning(f"[term] save visible transcript failed: {exc}")
            return False

    # ── Connection lifecycle ───────────────────────────────────
    def connect(self) -> None:
        self._open_transcript()
        self._set_status("connecting…", TERM_WARN)
        self._append_system_line(
            f"Connecting to {self.username}@{self.host}:{self.port}…"
        )
        if self._transcript_path:
            self._append_system_line(
                f"Transcript → {self._transcript_path}",
                colour=TERM_MUTED,
            )
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                hostname=self.host, port=self.port,
                username=self.username, password=self.password,
                timeout=30, allow_agent=False, look_for_keys=False,
            )
            self.shell = self.client.invoke_shell(
                term="xterm", width=200, height=50,
            )
            self.shell.settimeout(0.2)
            self.alive = True
        except Exception as exc:
            self._set_status("failed", TERM_ERROR)
            self._append_system_line(
                f"Connection failed: {type(exc).__name__}: {exc}",
                colour=TERM_ERROR,
            )
            return

        self._set_status("connected", TERM_OK)
        self._append_system_line("Connected.", colour=TERM_OK)

        self.reader_thread = threading.Thread(
            target=self._reader_loop, name=f"term-reader-{self.session_id}",
            daemon=True,
        )
        self.reader_thread.start()

    def close(self) -> None:
        self.alive = False
        try:
            if self.shell:
                self.shell.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self._close_transcript()
        self._set_status("closed", TERM_MUTED)

    # ── Reader thread ──────────────────────────────────────────
    def _reader_loop(self) -> None:
        while self.alive and self.shell is not None:
            try:
                if self.shell.recv_ready():
                    chunk = self.shell.recv(65536).decode(
                        "utf-8", errors="replace"
                    )
                    if chunk:
                        self.rx_queue.put(chunk)
                else:
                    time.sleep(0.02)
            except Exception as exc:
                self.rx_queue.put(
                    f"\n[reader error: {type(exc).__name__}: {exc}]\n"
                )
                self.alive = False
                break

    # ── UI poll (called by parent tick) ────────────────────────
    def drain_rx(self) -> bool:
        """Drain the rx queue into the output column. Returns True iff
        anything was actually drained (so the parent can decide whether
        to call ``page.update()``)."""
        drained = False
        while True:
            try:
                chunk = self.rx_queue.get_nowait()
            except queue.Empty:
                break
            self._append_chunk(chunk)
            drained = True
        return drained

    # ── Output rendering ───────────────────────────────────────
    def _append_chunk(self, chunk: str) -> None:
        """Append raw bytes (already decoded) to the output area,
        splitting on newlines so each rendered ``Text`` is one line.

        The trailing fragment (no newline yet — typically the live PS1
        prompt) is rendered as a *live tail* line that updates in
        place as new bytes arrive, so the operator can see the prompt
        the shell is waiting at without sending any command first.
        """
        # Mirror the raw chunk to the transcript file before any
        # cleanup, so the on-disk log preserves everything (including
        # ANSI codes and control chars) in case it ever needs review.
        self._write_transcript_chunk(chunk)

        text = normalize_terminal_text(self._line_buffer + chunk)
        # Normalize \r\n → \n; treat lone \r as a soft line clear (the
        # PTY uses it to redraw a single line; we just join).
        text = text.replace("\r\n", "\n").replace("\r", "")
        # Strip ANSI escape sequences (CSI, OSC, charset, …) so the
        # window-title and colour codes don't leak into the visible
        # transcript as garbage.
        text = strip_ansi(text)
        # Apply backspace semantics — bash echoes ``\b \b`` to erase a
        # character; we honour the cursor-back so the displayed line
        # matches what a real terminal would show.
        text = _apply_backspaces(text)
        # Drop any remaining C0/C1 control chars that aren't \n or \t
        # so they don't render as tofu squares.
        text = strip_control_chars(text)

        lines = text.split("\n")
        # Last fragment may be incomplete — keep it for next chunk.
        self._line_buffer = lines.pop()
        self._consume_local_echo_from_remote(normalize_terminal_text(chunk))

        # First, drop any stale live-tail control: completed lines are
        # about to be rendered and we'll re-add a fresh tail at the end.
        self._remove_live_tail()

        for line in lines:
            if self._optimistic_submitted_line == line:
                self._optimistic_submitted_line = None
                continue
            if self._optimistic_submitted_line and line.strip():
                self._optimistic_submitted_line = None
            self._append_line(line)

        # Re-render the live-tail with whatever's in the buffer now.
        self._render_live_tail()
        self.request_scroll_to_latest()

    def _remove_live_tail(self) -> None:
        if (self._live_tail_text is not None
                and self.output_column.controls
                and self.output_column.controls[-1] is self._live_tail_text):
            self.output_column.controls.pop()
        self._live_tail_text = None

    def scroll_to_latest(self) -> None:
        try:
            self.output_column.scroll_to(offset=-1, duration=0)
        except TypeError:
            try:
                self.output_column.scroll_to(offset=-1)
            except Exception:
                pass
        except Exception:
            pass

    def request_scroll_to_latest(self) -> None:
        self._scroll_requested = True

    def has_pending_scroll(self) -> bool:
        return self._scroll_requested

    def flush_scroll_request(self) -> bool:
        if not self._scroll_requested:
            return False
        self._scroll_requested = False
        self.scroll_to_latest()
        return True

    def _current_display_line(self) -> str:
        return f"{self._line_buffer}{self._local_echo_buffer}"

    def _render_live_tail(self) -> None:
        visible_line = self._current_display_line()
        if visible_line:
            self._set_live_tail(visible_line)

    def _consume_local_echo_from_remote(self, remote_chunk: str) -> None:
        if not self._local_echo_buffer:
            return
        consumed = 0
        for remote_char, local_char in zip(remote_chunk, self._local_echo_buffer):
            if remote_char != local_char:
                break
            consumed += 1
        for size in range(len(self._local_echo_buffer), consumed, -1):
            if self._line_buffer.endswith(self._local_echo_buffer[:size]):
                consumed = size
                break
        if consumed:
            self._local_echo_buffer = self._local_echo_buffer[consumed:]

    def _apply_local_input_echo(self, data: str) -> None:
        if "\x1b" in data:
            return
        for char in data:
            if char in ("\r", "\n"):
                visible_line = self._current_display_line()
                if visible_line:
                    self._remove_live_tail()
                    self._append_line(visible_line)
                    self._optimistic_submitted_line = visible_line
                    self._line_buffer = ""
                self._local_echo_buffer = ""
                continue
            if char == "\x7f":
                if self._local_echo_buffer:
                    self._local_echo_buffer = self._local_echo_buffer[:-1]
                elif self._line_buffer:
                    self._line_buffer = self._line_buffer[:-1]
                continue
            if char == "\t":
                self._local_echo_buffer += "\t"
                continue
            if char >= " " and char != "\x7f":
                self._local_echo_buffer += char
        self._remove_live_tail()
        self._render_live_tail()
        self.request_scroll_to_latest()

    def _push_local_echo_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            return
        if self.flush_scroll_request():
            try:
                self.page.update()
            except Exception:
                pass

    def _set_live_tail(self, line: str) -> None:
        spans = line_to_spans(line)
        self._live_tail_text = ft.Text(
            spans=spans,
            font_family=TERMINAL_FONT_FAMILY,
            size=TERMINAL_FONT_SIZE,
            selectable=True,
        )
        self.output_column.controls.append(self._live_tail_text)

    def _append_line(self, line: str, colour: Optional[str] = None) -> None:
        if colour:
            spans = [
                ft.TextSpan(
                    line if line else " ",
                    style=ft.TextStyle(color=colour),
                )
            ]
        else:
            spans = line_to_spans(line)
        self.output_column.controls.append(
            ft.Text(
                spans=spans,
                font_family=TERMINAL_FONT_FAMILY,
                size=TERMINAL_FONT_SIZE,
                selectable=True,
            )
        )
        # Cap transcript at 5000 lines so very long sessions don't
        # eat memory or slow down rendering.
        if len(self.output_column.controls) > 5000:
            del self.output_column.controls[: len(self.output_column.controls) - 5000]

    def _append_system_line(
        self, msg: str, colour: str = TERM_MUTED,
    ) -> None:
        self.output_column.controls.append(
            ft.Text(
                spans=[
                    ft.TextSpan(
                        f"[trfs] {msg}",
                        style=ft.TextStyle(
                            color=colour,
                            italic=True,
                        ),
                    )
                ],
                font_family=TERMINAL_FONT_FAMILY,
                size=TERMINAL_FONT_SIZE,
            )
        )
        self.request_scroll_to_latest()

    def _set_status(self, label: str, colour: str) -> None:
        try:
            txt = self.status_chip.content
            if isinstance(txt, ft.Text):
                txt.value = label
                txt.color = colour
            self.status_chip.bgcolor = ft.Colors.with_opacity(0.10, colour)
        except Exception:
            pass
        if self.on_status_change:
            try:
                self.on_status_change(self)
            except Exception:
                pass

    # ── Right-click / Ctrl+V paste ─────────────────────────────
    def _read_clipboard(self) -> str:
        """Best-effort clipboard read. Tries Flet's API first, then
        falls back to tkinter (which works on Windows + most Linux
        desktops without extra deps)."""
        # 1. Flet sync API (older versions)
        try:
            v = self.page.get_clipboard()
            if v is not None and not asyncio_iscoroutine(v):
                return str(v)
        except Exception:
            pass
        # 2. Flet async API (newer versions). We're on a worker
        # thread (right-click handler) — run the coroutine to
        # completion synchronously.
        try:
            coro = self.page.get_clipboard_async()
            if asyncio_iscoroutine(coro):
                import asyncio as _asyncio
                try:
                    loop = _asyncio.new_event_loop()
                    try:
                        return str(loop.run_until_complete(coro) or "")
                    finally:
                        loop.close()
                except Exception:
                    return ""
            elif coro is not None:
                return str(coro)
        except Exception:
            pass
        # 3. tkinter fallback (Windows + most Linux desktops)
        try:
            import tkinter as _tk
            root = _tk.Tk()
            root.withdraw()
            try:
                v = root.clipboard_get()
                return v or ""
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        return ""

    def paste_clipboard_to_shell(self) -> None:
        """Read clipboard via every available mechanism and forward
        to the SSH channel. Used by both right-click and Ctrl+V."""
        clip = self._read_clipboard()
        if not clip:
            self._append_system_line(
                "Paste failed: clipboard empty or inaccessible.",
                colour=TERM_WARN,
            )
            return
        # Normalize line endings — bash expects LF, not CRLF.
        clip = clip.replace("\r\n", "\n").replace("\r", "\n")
        self.send_bytes(clip)

    def _on_paste_clipboard(self, e) -> None:
        """Right-click handler — same effect as Ctrl+V."""
        self.paste_clipboard_to_shell()

    # ── Send raw bytes to the shell ────────────────────────────
    def send_bytes(self, data: str) -> None:
        """Send arbitrary bytes (or a single keystroke) to the SSH
        channel. The remote PTY echoes them back through the reader
        thread, so the user sees what they typed inline with the
        prompt — no local pre-echo needed."""
        if not self.alive or not self.shell:
            self._append_system_line(
                "Cannot send: shell not connected.", colour=TERM_ERROR,
            )
            return
        try:
            self.shell.send(data)
            self._apply_local_input_echo(data)
            self._push_local_echo_update()
        except Exception as exc:
            self._append_system_line(
                f"Send failed: {exc}", colour=TERM_ERROR,
            )


# ── The Terminal page (tab-host) ────────────────────────────────
class TerminalPage:
    """Top-level page: holds a Tabs control + the New / Close / Back
    buttons. Tabs hold individual ``TerminalSession`` views."""

    def __init__(self, page: ft.Page):
        self.page = page
        self.form = getattr(page, "integration_form", {}) or getattr(
            page, "trfs_form", {}
        )
        self.sessions: list[TerminalSession] = []
        self._poll_thread: Optional[threading.Thread] = None
        self._polling = False
        # Whether the page-level keyboard handler is currently
        # forwarding keys to the active SSH session. We toggle this
        # off while a modal dialog (e.g. "New Session") is open, so
        # typing in the dialog's text fields doesn't also go to SSH.
        self._intercept_keys = True
        self._prev_keyboard_handler = None

    # ── Build view ─────────────────────────────────────────────
    def build(self) -> ft.View:
        # Custom tab strip + content swapper. We don't use ``ft.Tabs``
        # because that control's signature changes between Flet
        # versions (older versions accept ``tabs=[…]``, newer require
        # ``content=…, length=…``). A simple Row of clickable
        # containers + a stack of content panes is portable and
        # behaves exactly like a tab control.
        self._selected_index = 0
        self._tab_button_row = ft.Row(
            controls=[],
            spacing=4,
            scroll=ft.ScrollMode.AUTO,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self._tab_content_host = ft.Container(
            content=ft.Container(),  # placeholder
            expand=True,
        )

        new_tab_btn = ft.ElevatedButton(
            "New Session",
            icon=ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED,
            style=ft.ButtonStyle(
                bgcolor=ACCENT,
                color="#06242A",
                padding=ft.Padding.symmetric(horizontal=14, vertical=12),
                shape=ft.RoundedRectangleBorder(radius=12),
                mouse_cursor=ft.MouseCursor.CLICK,
            ),
            on_click=self._on_new_session,
        )
        close_tab_btn = ft.OutlinedButton(
            "Close Tab",
            icon=ft.Icons.CLOSE_ROUNDED,
            tooltip="Close the current tab",
            style=secondary_button_style(),
            on_click=self._on_close_tab,
        )
        back_btn = ft.OutlinedButton(
            "Back",
            icon=ft.Icons.ARROW_BACK,
            style=secondary_button_style(),
            on_click=self._on_back,
        )

        header = ft.Row(
            [
                ft.Icon(ft.Icons.TERMINAL, size=22, color=ACCENT),
                ft.Text(
                    "TRFS Terminal", size=20,
                    weight=ft.FontWeight.BOLD, color=TEXT,
                ),
                ft.Container(width=8),
                ft.Text(
                    "interactive SSH — coloured output",
                    size=12, color=TEXT_MUTED,
                ),
                ft.Container(expand=True),
                new_tab_btn,
                close_tab_btn,
                back_btn,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
        )

        # The "tab strip" is a horizontally-scrolling row of buttons,
        # each one a (label + dot + close-X) capsule.
        tab_strip_panel = ft.Container(
            content=self._tab_button_row,
            bgcolor=PANEL,
            border=ft.Border.all(1, BORDER),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(
                [header, tab_strip_panel, self._tab_content_host],
                spacing=10,
                expand=True,
            ),
        )

        # Hook page-level keyboard so every keystroke goes straight
        # to the active SSH session. Save whatever handler was there
        # before so we can restore it when leaving the route.
        try:
            self._prev_keyboard_handler = self.page.on_keyboard_event
        except Exception:
            self._prev_keyboard_handler = None
        self.page.on_keyboard_event = self._on_page_keyboard

        # Auto-open one tab using the form's SSH credentials, if any.
        host = str(self.form.get("host", "")).strip()
        if host:
            self._open_session(
                host=host,
                port=int(self.form.get("port", 22) or 22),
                username=str(self.form.get("username", "")).strip(),
                password=str(self.form.get("password", "")),
                title=host,
            )
        else:
            # No credentials yet — prompt user to add a session.
            self._show_new_session_dialog()

        # Start the UI poll loop that drains each session's rx queue.
        self._start_poll_loop()

        return ft.View(
            route="/terminal", padding=0, spacing=0,
            bgcolor=BG_TOP, controls=[body],
        )

    # ── Page-level keyboard → SSH channel ──────────────────────
    # Map Flet key names to the bytes a typical xterm-style PTY
    # expects. Function keys / less-common ones are best-effort.
    _SPECIAL_KEYS = {
        "Enter":         "\r",
        "Numpad Enter":  "\r",
        "Backspace":     "\x7f",
        "Tab":           "\t",
        "Escape":        "\x1b",
        "Space":         " ",
        "Arrow Up":      "\x1b[A",
        "Arrow Down":    "\x1b[B",
        "Arrow Right":   "\x1b[C",
        "Arrow Left":    "\x1b[D",
        "Home":          "\x1b[H",
        "End":           "\x1b[F",
        "Page Up":       "\x1b[5~",
        "Page Down":     "\x1b[6~",
        "Delete":        "\x1b[3~",
        "Insert":        "\x1b[2~",
        "F1":  "\x1bOP", "F2":  "\x1bOQ",
        "F3":  "\x1bOR", "F4":  "\x1bOS",
        "F5":  "\x1b[15~", "F6":  "\x1b[17~",
        "F7":  "\x1b[18~", "F8":  "\x1b[19~",
        "F9":  "\x1b[20~", "F10": "\x1b[21~",
        "F11": "\x1b[23~", "F12": "\x1b[24~",
    }

    # Shift-modifier symbol map (US QWERTY). Best-effort — non-US
    # layouts will need adjustments. Letters are handled separately.
    _SHIFT_SYMBOLS = {
        "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
        "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
        "-": "_", "=": "+",
        "[": "{", "]": "}", "\\": "|",
        ";": ":", "'": "\"",
        ",": "<", ".": ">", "/": "?",
        "`": "~",
    }

    def _key_event_to_bytes(self, e) -> Optional[str]:
        """Convert a Flet KeyboardEvent into the byte string a remote
        PTY expects. Returns ``None`` if we should let the event
        through (modifier-only press, unhandled key combo, etc.)."""
        key = e.key or ""
        ctrl = bool(getattr(e, "ctrl", False))
        shift = bool(getattr(e, "shift", False))
        alt = bool(getattr(e, "alt", False))
        meta = bool(getattr(e, "meta", False))

        # Modifier keys alone — ignore (don't send anything)
        if key in ("Control", "Shift", "Alt", "Meta", "Control Left",
                   "Control Right", "Shift Left", "Shift Right",
                   "Alt Left", "Alt Right", "Meta Left", "Meta Right"):
            return None

        # Ctrl+letter → C0 control byte (Ctrl+C → \x03, Ctrl+D → \x04 …)
        if ctrl and not alt and not meta and len(key) == 1 and key.isalpha():
            return chr(ord(key.upper()) - 64)

        # Special keys (arrows, F-keys, Enter, Tab …)
        if key in self._SPECIAL_KEYS:
            return self._SPECIAL_KEYS[key]

        # Single-character keys
        if len(key) == 1:
            ch = key
            if ch.isalpha():
                ch = ch.upper() if shift else ch.lower()
            elif shift and ch in self._SHIFT_SYMBOLS:
                ch = self._SHIFT_SYMBOLS[ch]
            return ch

        # Anything we don't recognise → ignore
        return None

    def _dialog_open(self) -> bool:
        try:
            for o in (self.page.overlay or []):
                if getattr(o, "open", False):
                    return True
        except Exception:
            pass
        return False

    def _on_page_keyboard(self, e) -> None:
        # Don't intercept while a modal dialog (e.g. New Session) is
        # open — let the dialog's own text fields receive keys.
        if self._dialog_open() or not self._intercept_keys:
            if self._prev_keyboard_handler:
                try:
                    self._prev_keyboard_handler(e)
                except Exception:
                    pass
            return
        if not self.sessions:
            return
        idx = max(0, min(self._selected_index, len(self.sessions) - 1))
        session = self.sessions[idx]
        if not session.alive or not session.shell:
            return

        key = (e.key or "").upper()
        ctrl = bool(getattr(e, "ctrl", False))
        shift = bool(getattr(e, "shift", False))

        # Ctrl+V → paste from clipboard (do NOT send literal Ctrl-V)
        if ctrl and key == "V" and not shift:
            session.paste_clipboard_to_shell()
            return
        # Shift+Insert → also paste, mirrors xterm convention
        if shift and key == "INSERT":
            session.paste_clipboard_to_shell()
            return
        # Ctrl+Shift+C → copy selection (the OS handles this — we just
        # don't accidentally translate it into a control byte that
        # would steal the keystroke from the OS clipboard system).
        if ctrl and shift and key == "C":
            return

        data = self._key_event_to_bytes(e)
        if data:
            session.send_bytes(data)

    # ── Poll loop: drain rx queues and push to UI ──────────────
    def _start_poll_loop(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="terminal-poll", daemon=True,
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while self._polling:
            try:
                any_drained = False
                for s in list(self.sessions):
                    if s.drain_rx():
                        any_drained = True
                selected = self._get_selected_session()
                if any_drained or (selected is not None and selected.has_pending_scroll()):
                    self._update_page_and_keep_latest()
            except Exception:
                pass
            time.sleep(0.03)

    # ── Tab management ─────────────────────────────────────────
    def _open_session(
        self,
        host: str, port: int, username: str, password: str,
        title: str,
    ) -> None:
        session = TerminalSession(
            page=self.page,
            host=host, port=port,
            username=username, password=password,
            title=title or host,
            on_status_change=lambda s: self._rebuild_tab_strip(),
        )
        self.sessions.append(session)
        self._selected_index = len(self.sessions) - 1
        self._rebuild_tab_strip()
        self._update_content_pane()
        self._update_page_and_keep_latest()

        # Connect on a background thread so the UI doesn't freeze
        # during the SSH handshake.
        threading.Thread(
            target=session.connect,
            name=f"term-connect-{session.session_id}",
            daemon=True,
        ).start()

    def _make_tab_button(self, idx: int, session: TerminalSession) -> ft.Control:
        """Build one capsule for the tab strip: dot + title + small ✕."""
        # Status → dot colour
        status_text = ""
        try:
            status_text = session.status_chip.content.value
        except Exception:
            pass
        dot_colour = TERM_MUTED
        if status_text == "connected":
            dot_colour = TERM_OK
        elif status_text == "connecting…":
            dot_colour = TERM_WARN
        elif status_text in ("failed", "closed"):
            dot_colour = TERM_ERROR

        is_selected = (idx == self._selected_index)
        bg = (
            ft.Colors.with_opacity(0.20, ACCENT)
            if is_selected
            else ft.Colors.with_opacity(0.04, TEXT)
        )
        border_colour = ACCENT if is_selected else BORDER

        close_btn = ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED,
            icon_size=14,
            icon_color=TEXT_MUTED,
            tooltip="Close this tab",
            on_click=lambda e, i=idx: self._close_tab_at(i),
            padding=0,
        )

        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CIRCLE, size=10, color=dot_colour),
                    ft.Text(
                        session.title, size=13,
                        color=TEXT if is_selected else TEXT_MUTED,
                        weight=ft.FontWeight.W_600 if is_selected else ft.FontWeight.W_500,
                    ),
                    close_btn,
                ],
                spacing=6, tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e, i=idx: self._select_tab(i),
            bgcolor=bg,
            border=ft.Border.all(1, border_colour),
            border_radius=8,
            padding=ft.Padding(left=10, top=4, right=4, bottom=4),
            ink=True,
        )

    def _rebuild_tab_strip(self) -> None:
        controls = []
        for i, s in enumerate(self.sessions):
            controls.append(self._make_tab_button(i, s))
        self._tab_button_row.controls = controls
        try:
            self.page.update()
        except Exception:
            pass

    def _get_selected_session(self) -> Optional[TerminalSession]:
        if not self.sessions:
            return None
        idx = max(0, min(self._selected_index, len(self.sessions) - 1))
        return self.sessions[idx]

    def _update_page_and_keep_latest(self) -> None:
        try:
            self.page.update()
        except Exception:
            return
        session = self._get_selected_session()
        if session is None:
            return
        if session.flush_scroll_request():
            try:
                self.page.update()
            except Exception:
                pass

    def _update_content_pane(self) -> None:
        if not self.sessions:
            self._tab_content_host.content = ft.Container(
                content=ft.Text(
                    "No active sessions. Click 'New Session' to start one.",
                    color=TEXT_MUTED, italic=True,
                ),
                alignment=ft.Alignment(0, 0),
                expand=True,
            )
            return
        idx = max(0, min(self._selected_index, len(self.sessions) - 1))
        session = self.sessions[idx]
        self._tab_content_host.content = ft.Container(
            content=session.tab_view,
            padding=ft.Padding.symmetric(horizontal=4, vertical=8),
            expand=True,
        )
        session.request_scroll_to_latest()
        # Focus the input field of the newly active tab so the
        # operator can start typing without an extra click.
        try:
            session.input_field.focus()
        except Exception:
            pass

    def _select_tab(self, idx: int) -> None:
        if 0 <= idx < len(self.sessions):
            self._selected_index = idx
            self._rebuild_tab_strip()
            self._update_content_pane()
            self._update_page_and_keep_latest()

    def _close_tab_at(self, idx: int) -> None:
        if not (0 <= idx < len(self.sessions)):
            return
        session = self.sessions[idx]
        session.close()
        self.sessions.pop(idx)
        if self._selected_index >= len(self.sessions):
            self._selected_index = max(0, len(self.sessions) - 1)
        self._rebuild_tab_strip()
        self._update_content_pane()
        self._update_page_and_keep_latest()

    def _on_close_tab(self, e) -> None:
        self._close_tab_at(self._selected_index)

    def _on_new_session(self, e) -> None:
        self._show_new_session_dialog()

    def _show_new_session_dialog(self) -> None:
        host_field = ft.TextField(
            label="Host",
            value=str(self.form.get("host", "")),
            autofocus=True,
        )
        port_field = ft.TextField(
            label="Port",
            value=str(self.form.get("port", 22) or 22),
        )
        user_field = ft.TextField(
            label="Username",
            value=str(self.form.get("username", "")),
        )
        pwd_field = ft.TextField(
            label="Password",
            value=str(self.form.get("password", "")),
            password=True,
            can_reveal_password=True,
        )
        title_field = ft.TextField(
            label="Tab title (optional)",
            value="",
            hint_text="Leave blank to use host",
        )

        dlg: ft.AlertDialog

        def _on_open(e):
            host = host_field.value.strip()
            if not host:
                return
            try:
                port = int(port_field.value or "22")
            except Exception:
                port = 22
            self._open_session(
                host=host, port=port,
                username=user_field.value.strip(),
                password=pwd_field.value or "",
                title=title_field.value.strip() or host,
            )
            dlg.open = False
            self.page.update()

        def _on_cancel(e):
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("New SSH session", color=ACCENT,
                          weight=ft.FontWeight.BOLD),
            content=ft.Column(
                [host_field, port_field, user_field, pwd_field, title_field],
                spacing=8, tight=True, width=420,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_on_cancel),
                ft.ElevatedButton(
                    "Open", icon=ft.Icons.PLAY_ARROW_ROUNDED,
                    style=ft.ButtonStyle(
                        bgcolor=ACCENT, color="#06242A",
                        shape=ft.RoundedRectangleBorder(radius=10),
                    ),
                    on_click=_on_open,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=PANEL,
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self.page.update()

    # ── Navigation ─────────────────────────────────────────────
    def _on_back(self, e) -> None:
        # Restore the previous keyboard handler so other pages don't
        # accidentally have their keystrokes routed to the (no longer
        # visible) terminal sessions.
        try:
            self.page.on_keyboard_event = self._prev_keyboard_handler
        except Exception:
            pass
        # Stop the rx-poll loop too — sessions stay alive in memory
        # so the user can come back, but we don't need to keep
        # waking up if no terminal is visible.
        self._polling = False
        # Hard-close every session so SSH channels don't leak when
        # leaving the terminal page. (Going back to /form means the
        # operator is done with the terminal for now.)
        for s in list(self.sessions):
            try:
                s.close()
            except Exception:
                pass
        self.page.go("/form")
