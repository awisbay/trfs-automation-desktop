"""
TRFS GUI - Live run monitor.
"""
import asyncio
import gc
import logging
import os
import queue
import re
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from command_parser import parse_commands_file
from main import (
    process_band,
    process_band_demo,
    run_moshell_for_band,
    capture_enm_shared_artifacts,
    build_enm_screenshots_for_band,
    run_multi_node,
)
from sdir_capture import capture_shared_sdir_screenshot, capture_sdir_for_node, run_band_detection_commands, run_node_setup, run_node_setup_standalone
from band_detector import detect_bands_from_sdir, detect_bands_from_hgetc
from srs_capture import (
    launch_edge_with_debug_port,
    is_edge_debug_port_open,
    wait_for_cdp_ready,
    SrsBrowserSession,
    CDP_PORT,
    SRS_BASE_URL,
    _find_edge_exe,
)
from excel_writer import create_band_excel, insert_screenshots_for_band
from ssh_runner import MoshellSession
from config_loader import get_full_path
from gui.theme import (
    ACCENT,
    BG_BOTTOM,
    DANGER,
    INFO,
    PANEL,
    PANEL_RAISED,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    badge,
    panel,
    secondary_button_style,
)


class FletLogHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self._queue = log_queue

    def emit(self, record):
        # Thread-safe: just push to the queue, never touch UI
        try:
            self._queue.put_nowait(self.format(record))
        except Exception:
            pass


class ProgressPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.cancelled = False
        self._log_handler = None
        self._log_queue = queue.Queue()
        self._run_finished = False
        self.back_button = ft.ElevatedButton(
            "Back to Form",
            icon=ft.Icons.ARROW_BACK,
            disabled=True,
            style=secondary_button_style(),
            on_click=lambda _: asyncio.create_task(self.page.push_route("/")),
        )

    def build(self) -> ft.View:
        config = getattr(self.page, "trfs_config", None)
        demo_mode = getattr(self.page, "trfs_demo_mode", False)
        commands_file = getattr(self.page, "trfs_commands_file", "")
        all_commands = parse_commands_file(commands_file)
        band_names = list(all_commands.keys())

        self.start_time = datetime.now()
        self.status_text = ft.Text("Preparing automation context", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.mode_text = ft.Text(
            "Demo mode enabled" if demo_mode else f"Connecting to {config.ssh.host}:{config.ssh.port}",
            size=13,
            color=TEXT_MUTED,
        )
        self.elapsed_text = ft.Text("Elapsed 00:00:00", size=12, color=TEXT_MUTED)
        self.log_list = ft.ListView(expand=True, spacing=8, auto_scroll=True)

        # Count categories per band for progress tracking
        from command_parser import get_moshell_categories
        moshell_categories = get_moshell_categories()
        self.band_total_steps = {}
        for band in band_names:
            cats = all_commands.get(band, {})
            # Count moshell categories that have commands + 2 (excel create + excel insert)
            steps = sum(1 for c in moshell_categories if cats.get(c)) + 2
            # Add ENM categories
            steps += sum(1 for k in cats if k.endswith("_ENM"))
            self.band_total_steps[band] = max(steps, 1)
        self.band_current_step = {band: 0 for band in band_names}

        self.band_cards = {}
        self.band_start_times = {}
        self.band_elapsed = {}
        band_grid_controls = []
        for band in band_names:
            label = ft.Text(band, size=14, weight=ft.FontWeight.BOLD, color=TEXT)
            pct_text = ft.Text("0%", size=11, color=TEXT_MUTED)
            state = ft.Text("Queued", size=11, color=TEXT_MUTED)
            timer_text = ft.Text("", size=11, color=TEXT_MUTED)
            progress_bar = ft.ProgressBar(
                value=0,
                bgcolor=ft.Colors.with_opacity(0.15, "#36556E"),
                color=INFO,
                bar_height=4,
                border_radius=2,
            )
            chip = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [label, ft.Row([timer_text, pct_text, state], spacing=6)],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        progress_bar,
                    ],
                    spacing=6,
                ),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                bgcolor=PANEL_RAISED,
                border=ft.Border.all(1, ft.Colors.with_opacity(0.5, "#36556E")),
                border_radius=12,
            )
            self.band_cards[band] = {
                "container": chip,
                "state": state,
                "pct_text": pct_text,
                "timer_text": timer_text,
                "progress_bar": progress_bar,
            }
            band_grid_controls.append(chip)

        # Collapsible band radar body
        self.band_body = ft.Row(band_grid_controls, wrap=True, spacing=8, run_spacing=8)
        self.band_chevron = ft.Icon(ft.Icons.EXPAND_LESS, color=TEXT_MUTED, size=20)

        def toggle_band_radar(_):
            self.band_body.visible = not self.band_body.visible
            self.band_chevron.icon = ft.Icons.EXPAND_MORE if not self.band_body.visible else ft.Icons.EXPAND_LESS
            self.page.update()

        # Collapsible log body
        self.log_body = ft.Container(
            expand=True,
            padding=18,
            bgcolor=ft.Colors.with_opacity(0.45, BG_BOTTOM),
            border_radius=20,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.45, "#314D64")),
            content=self.log_list,
        )
        self.log_chevron = ft.Icon(ft.Icons.EXPAND_LESS, color=TEXT_MUTED, size=20)

        def toggle_log(_):
            self.log_body.visible = not self.log_body.visible
            self.log_chevron.icon = ft.Icons.EXPAND_MORE if not self.log_body.visible else ft.Icons.EXPAND_LESS
            self.page.update()

        hero = panel(
            ft.Row(
                [
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    badge("Live Run", ACCENT, ft.Icons.TIMER),
                                    badge("SSH" if not demo_mode else "Simulation", INFO, ft.Icons.HUB),
                                ],
                                spacing=10,
                            ),
                            self.status_text,
                            self.mode_text,
                            self.elapsed_text,
                        ],
                        spacing=8,
                        expand=True,
                    ),
                    ft.Row(
                        [
                            self._metric_box("Bands", str(len(band_names)), INFO),
                            self._metric_box("Mode", "DEMO" if demo_mode else "LIVE", ACCENT),
                            self._metric_box("Session", "Active", SUCCESS),
                        ],
                        spacing=10,
                    ),
                ],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=PANEL,
            padding=20,
        )

        progress_panel = panel(
            ft.Column(
                [
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Text("Band radar", size=14, weight=ft.FontWeight.BOLD, color=TEXT),
                                self.band_chevron,
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        on_click=toggle_band_radar,
                        ink=True,
                    ),
                    self.band_body,
                ],
                spacing=10,
            ),
            bgcolor="#11273A",
            padding=16,
        )

        log_panel = panel(
            ft.Column(
                [
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text("Run stream", size=18, weight=ft.FontWeight.BOLD, color=TEXT),
                                        ft.Text(
                                            "Operational logs, warnings, and completion events appear here in real time.",
                                            size=12,
                                            color=TEXT_MUTED,
                                        ),
                                    ],
                                    spacing=4,
                                    expand=True,
                                ),
                                ft.Row(
                                    [
                                        self.log_chevron,
                                        self.back_button,
                                    ],
                                    spacing=8,
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        on_click=toggle_log,
                        ink=True,
                    ),
                    self.log_body,
                ],
                spacing=16,
                expand=True,
            ),
            bgcolor="#0E1F2F",
            padding=24,
            expand=True,
        )

        self._start_run(config, demo_mode, all_commands)

        return ft.View(
            route="/progress",
            padding=0,
            spacing=0,
            controls=[
                ft.Container(
                    expand=True,
                    gradient=background_gradient(),
                    padding=ft.Padding.symmetric(horizontal=28, vertical=24),
                    content=ft.Column(
                        [
                            hero,
                            progress_panel,
                            log_panel,
                        ],
                        spacing=18,
                        expand=True,
                    ),
                )
            ],
        )

    def _metric_box(self, label: str, value: str, color: str) -> ft.Container:
        return ft.Container(
            width=120,
            padding=12,
            bgcolor=ft.Colors.with_opacity(0.14, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.28, color)),
            border_radius=14,
            content=ft.Column(
                [
                    ft.Text(label, size=10, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                    ft.Text(value, size=16, color=TEXT, weight=ft.FontWeight.BOLD),
                ],
                spacing=4,
            ),
        )

    def _log_message(self, msg: str):
        """Thread-safe: push message to queue for the UI timer to pick up."""
        self._log_queue.put_nowait(msg)

    def _flush_log_queue(self):
        """Called on the main/UI thread by a periodic timer. Drains the
        queue and batch-updates the log list so the user sees real-time output.

        Caps messages per tick at 200 so a huge burst (parallel workers all
        logging at once) can't stall the UI thread on a single page.update().
        Remaining messages are picked up on the next 100ms tick.
        """
        dirty = False
        MAX_PER_TICK = 200
        processed = 0
        while processed < MAX_PER_TICK:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1

            # Handle band status updates pushed from worker threads
            if msg.startswith("__BAND_STATUS__:"):
                _, band, status = msg.split(":", 2)
                self._apply_band_status(band, status)
                dirty = True
                continue

            # Handle status text updates pushed from worker threads
            if msg.startswith("__STATUS__:"):
                _, text = msg.split(":", 1)
                self.status_text.value = text
                dirty = True
                continue
            if msg.startswith("__MODE__:"):
                _, text = msg.split(":", 1)
                self.mode_text.value = text
                dirty = True
                continue

            # Track per-band progress
            for m in re.finditer(r"\[(\w+)\]", msg):
                band_candidate = m.group(1)
                if band_candidate in self.band_cards:
                    self._update_band_progress(band_candidate)
                    break

            color = TEXT_MUTED
            if "[WARNING]" in msg:
                color = "#FFD27A"
            elif "[ERROR]" in msg or "FATAL ERROR" in msg:
                color = "#FF9D9D"
            elif "[INFO]" in msg:
                color = INFO
            elif "Completed" in msg or "[OK]" in msg:
                color = SUCCESS

            self.log_list.controls.append(
                ft.Text(msg, size=12, color=color, selectable=True, font_family="Consolas")
            )
            dirty = True

        # Always tick running band timers and elapsed
        now = datetime.now()
        for band, start in self.band_start_times.items():
            if band not in self.band_elapsed:  # still running
                card = self.band_cards.get(band)
                if card:
                    card["timer_text"].value = str(now - start).split(".")[0]
        self.elapsed_text.value = f"Elapsed {str(now - self.start_time).split('.')[0]}"

        if len(self.log_list.controls) > 600:
            self.log_list.controls = self.log_list.controls[-600:]
        try:
            self.page.update()
        except Exception:
            pass

    def _gui_enm_prompt(self, enm_item: str, save_path: str, band: str, category: str):
        """Worker-thread callback: open a dialog and block until user responds."""
        event = threading.Event()
        result = {"path": None}

        file_picker = ft.FilePicker()

        def finish(path):
            result["path"] = path
            try:
                dialog.open = False
                self.page.update()
            except Exception:
                pass
            event.set()

        def on_skip(_):
            self._log_message(f"  [{band}] {category} ENM: Skipped by user")
            finish(None)

        def on_file_picked(e: ft.FilePickerResultEvent):
            if e.files:
                src = e.files[0].path
                try:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    shutil.copy2(src, save_path)
                    self._log_message(f"  [{band}] {category} ENM: Manual file accepted")
                    finish(save_path)
                except Exception as exc:
                    self._log_message(f"  [{band}] {category} ENM: Copy failed: {exc}")
                    finish(None)

        def on_browse(_):
            file_picker.pick_files(
                dialog_title=f"Select screenshot for {band} / {enm_item}",
                allowed_extensions=["png", "jpg", "jpeg"],
                allow_multiple=False,
            )

        def on_capture(_):
            try:
                from enm_capture import capture_screen_region
                captured = capture_screen_region(save_path)
                self._log_message(f"  [{band}] {category} ENM: Screen captured")
                finish(captured)
            except Exception as exc:
                self._log_message(f"  [{band}] {category} ENM: Capture failed: {exc}")
                finish(None)

        file_picker.on_result = on_file_picked

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"ENM screenshot needed — {band}"),
            content=ft.Column(
                [
                    ft.Text(f"Item: {enm_item}", size=13),
                    ft.Text(f"Category: {category}", size=12, color=TEXT_MUTED),
                    ft.Text(
                        "Open ENM in your browser and navigate to this item, "
                        "then choose Capture Screen, Browse for a saved PNG, or Skip.",
                        size=12,
                        color=TEXT_MUTED,
                    ),
                ],
                tight=True,
                spacing=8,
            ),
            actions=[
                ft.TextButton("Skip", on_click=on_skip),
                ft.TextButton("Browse...", on_click=on_browse),
                ft.FilledButton("Capture Screen", on_click=on_capture),
            ],
        )

        try:
            if file_picker not in self.page.overlay:
                self.page.overlay.append(file_picker)
            self.page.dialog = dialog
            dialog.open = True
            self.page.update()
        except Exception as exc:
            self._log_message(f"  [{band}] {category} ENM: Dialog failed ({exc}) — skipping")
            return None

        event.wait()
        return result["path"]

    def _srs_login_dialog(self) -> bool:
        """Show a dialog asking the user to log in to SRS via Azure SSO in Edge.

        Blocks the worker thread until the user clicks Continue or Skip.
        Returns True if user clicked Continue, False if skipped.
        """
        event = threading.Event()
        result = {"continue": False}

        def on_continue(_):
            result["continue"] = True
            try:
                dialog.open = False
                self.page.update()
            except Exception:
                pass
            event.set()

        def on_skip(_):
            result["continue"] = False
            try:
                dialog.open = False
                self.page.update()
            except Exception:
                pass
            event.set()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("SRS Login Required"),
            content=ft.Column(
                [
                    ft.Text(
                        "Microsoft Edge has been opened to srs.ericsson.com.",
                        size=14,
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "Please log in using your Azure SSO credentials in the Edge window.\n\n"
                        "Once you are fully logged in and can see the SRS dashboard, "
                        "click Continue below to capture the screenshots.\n\n"
                        "If you want to skip SRS screenshots, click Skip.",
                        size=13,
                        color=TEXT_MUTED,
                    ),
                    ft.Container(
                        padding=ft.Padding(top=12, bottom=0, left=0, right=0),
                        content=ft.Row(
                            [
                                ft.Icon(ft.Icons.INFO_OUTLINE, color=INFO, size=18),
                                ft.Text(
                                    "Edge is using your real profile so SSO cookies are preserved.",
                                    size=11,
                                    color=INFO,
                                ),
                            ],
                            spacing=8,
                        ),
                    ),
                ],
                tight=True,
                spacing=8,
            ),
            actions=[
                ft.TextButton("Skip", on_click=on_skip),
                ft.FilledButton("Continue — I'm logged in", on_click=on_continue),
            ],
        )

        try:
            self.page.dialog = dialog
            dialog.open = True
            self.page.update()
        except Exception as exc:
            self._log_message(f"[SRS] Login dialog failed ({exc}) — skipping")
            return False

        event.wait()
        return result["continue"]

    def _update_band_progress(self, band: str):
        """Increment band progress by one step and update the bar.
        Called from _flush_log_queue on the UI thread — safe to mutate controls."""
        card = self.band_cards.get(band)
        if not card:
            return
        self.band_current_step[band] = min(
            self.band_current_step[band] + 1, self.band_total_steps[band]
        )
        pct = self.band_current_step[band] / self.band_total_steps[band]
        card["pct_text"].value = f"{int(pct * 100)}%"
        card["progress_bar"].value = pct
        # No individual update() here — _flush_log_queue does one batch page.update()

    def _update_band_status(self, band: str, status: str):
        """Thread-safe: push a status change that the UI timer will apply."""
        self._log_queue.put_nowait(f"__BAND_STATUS__:{band}:{status}")

    def _apply_band_status(self, band: str, status: str):
        """Apply band card visual state. Called on the UI thread only."""
        card = self.band_cards.get(band)
        if not card:
            return

        container: ft.Container = card["container"]
        state_text: ft.Text = card["state"]
        pct_text: ft.Text = card["pct_text"]
        timer_text: ft.Text = card["timer_text"]
        progress_bar: ft.ProgressBar = card["progress_bar"]

        if status == "running":
            self.band_start_times[band] = datetime.now()
            container.bgcolor = ft.Colors.with_opacity(0.16, INFO)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.38, INFO))
            state_text.value = "Running"
            state_text.color = INFO
            pct_text.color = INFO
            pct_text.value = "0%"
            timer_text.color = INFO
            progress_bar.color = INFO
            progress_bar.value = 0
        elif status == "done":
            # Freeze the final elapsed time
            if band in self.band_start_times:
                elapsed = datetime.now() - self.band_start_times[band]
                self.band_elapsed[band] = elapsed
                timer_text.value = str(elapsed).split(".")[0]
            timer_text.color = SUCCESS
            container.bgcolor = ft.Colors.with_opacity(0.16, SUCCESS)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.38, SUCCESS))
            state_text.value = "Complete"
            state_text.color = SUCCESS
            pct_text.value = "100%"
            pct_text.color = SUCCESS
            progress_bar.color = SUCCESS
            progress_bar.value = 1.0
        elif status == "error":
            if band in self.band_start_times:
                elapsed = datetime.now() - self.band_start_times[band]
                self.band_elapsed[band] = elapsed
                timer_text.value = str(elapsed).split(".")[0]
            timer_text.color = DANGER
            container.bgcolor = ft.Colors.with_opacity(0.16, DANGER)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.38, DANGER))
            state_text.value = "Failed"
            state_text.color = DANGER
            pct_text.color = DANGER
            progress_bar.color = DANGER
        elif status == "skipped":
            container.bgcolor = ft.Colors.with_opacity(0.10, TEXT_MUTED)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.25, TEXT_MUTED))
            state_text.value = "Skipped"
            state_text.color = TEXT_MUTED
            pct_text.value = "-"
            pct_text.color = TEXT_MUTED
            timer_text.value = ""
            progress_bar.color = TEXT_MUTED
            progress_bar.value = 0

    def _start_run(self, config, demo_mode, all_commands):
        handler = FletLogHandler(self._log_queue)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

        # Start a periodic timer on the UI thread to flush the log queue.
        # With parallel workers across 3 nodes, logs arrive in bursts — a
        # 500ms flush interval made output feel chunky. 100ms keeps the
        # stream visibly real-time without stressing page.update().
        async def _poll_logs():
            while not self._run_finished:
                self._flush_log_queue()
                await asyncio.sleep(0.1)
            # Final flush after worker finishes
            self._flush_log_queue()

        self.page.run_task(_poll_logs)

        # Daemon=True so closing the window kills the worker with the
        # process instead of leaving a zombie SSH/Playwright run in the
        # background. Graceful cleanup is provided by gui_app's close
        # handler which calls ssh_registry.kill_all() before os._exit().
        thread = threading.Thread(
            target=self._run_worker,
            args=(config, demo_mode, all_commands),
            daemon=True,
        )
        thread.start()
        self.page.trfs_run_thread = thread

    def _run_worker(self, config, demo_mode, all_commands):
        generated_files = []
        # Per-run summary trackers (consumed by _emit_summary at the end).
        # node_bands_snapshot: node_name -> [bands assigned to that node]
        # band_results:        node_name -> {band: "ok"|"fail"}
        # all_command_bands:   every band present in the loaded commands file
        self._node_bands_snapshot: dict[str, list[str]] = {}
        self._band_results: dict[str, dict[str, str]] = {}
        self._all_command_bands: list[str] = list(all_commands.keys())
        try:
            if demo_mode:
                self._log_message("Starting desktop run in DEMO mode.")
                self._run_demo(config, all_commands, generated_files)
            else:
                self._log_message(f"Opening SSH session to {config.ssh.host}:{config.ssh.port}.")
                self._run_live(config, all_commands, generated_files)
        except BaseException as exc:
            tb = traceback.format_exc()
            try:
                self._log_message(f"FATAL ERROR: {exc}")
                for line in tb.splitlines():
                    self._log_message(f"  {line}")
            except Exception:
                pass
            logging.getLogger(__name__).error("Fatal error in run worker\n%s", tb)

        self.page.trfs_generated_files = generated_files
        duration = datetime.now() - self.start_time
        self.page.trfs_duration = str(duration).split(".")[0]
        self.status_text.value = "Automation complete"
        self.mode_text.value = f"Run finished in {duration}."
        self.elapsed_text.value = ""
        self.back_button.disabled = False

        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)

        # Emit the final per-node summary block before we navigate away.
        try:
            self._emit_summary(config)
        except Exception:
            logging.getLogger(__name__).exception("Failed to emit run summary")

        # Signal the UI poll loop to stop after one final flush
        self._run_finished = True

        try:
            self.page.update()
        except Exception:
            pass
        # _run_worker is a background thread — we can't asyncio.create_task
        # here (no running loop in this thread). Hand the coroutine back to
        # Flet's event loop via page.run_task (thread-safe).
        try:
            self.page.run_task(self.page.push_route, "/result")
        except Exception:
            try:
                loop = getattr(self.page, "loop", None)
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self.page.push_route("/result"), loop
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to navigate to /result after run"
                )

    def _emit_summary(self, config) -> None:
        """Render the end-of-run per-node summary + single output folder path.

        Format (example with 3 nodes, commands file also asks for NR700):

            ════════════════════════════════════════════════════════════════
              TRFS RUN SUMMARY
            ════════════════════════════════════════════════════════════════
              Node 1  MIN2810_MANKLMTAGUMDDNB01   (LTE/NR)
                  L1800   OK
                  L2100   OK
                  L2600   OK
              Node 2  MIN2810_MANKLMTAGUMDDNB02   (LTE/NR)
                  L700    OK
                  L900    OK
              Node 3  MIN2810_MANKLMTAGUMDDNB03   (GSM)
                  GSM     OK
            ────────────────────────────────────────────────────────────────
              Not captured on any node:
                  NR700   — band is not present on any of the configured nodes
            ────────────────────────────────────────────────────────────────
              Output folder:
                  C:\\dev\\TRFS\\output\\MIN2810
            ════════════════════════════════════════════════════════════════

        We intentionally show only the main output folder (not per-band
        subfolders) since every band report and every screenshot lands
        inside the same site-shortcode folder.
        """
        BAR = "═" * 68
        SEP = "─" * 68

        node_bands = self._node_bands_snapshot or {}
        results   = self._band_results or {}
        all_bands = self._all_command_bands or []

        # Structured version of the same summary, for the Result page to
        # render. Keys mirror what _emit_summary prints below.
        summary_data = {
            "nodes": [],           # [{"index","node_name","tech","bands":[{"band","status"}]}]
            "missing_bands": [],   # bands absent from every node
            "output_folder": "",
        }

        # Preserve node order as configured on SiteConfig (node1 → node2 → GSM).
        try:
            ordered_nodes = [n for n in config.site.get_all_nodes() if n]
        except Exception:
            ordered_nodes = list(node_bands.keys())
        # Include any stray node that appeared in results but not in config.
        for n in list(node_bands.keys()) + list(results.keys()):
            if n and n not in ordered_nodes:
                ordered_nodes.append(n)

        gsm_node = getattr(config.site, "gsm_node_name", "") or ""

        self._log_message("")
        self._log_message(BAR)
        self._log_message("  TRFS RUN SUMMARY")
        self._log_message(BAR)

        captured_bands_global: set[str] = set()

        for idx, node_name in enumerate(ordered_nodes, start=1):
            tech = "GSM" if (gsm_node and node_name == gsm_node) else "LTE/NR"
            self._log_message(f"  Node {idx}  {node_name}   ({tech})")

            bands_for_node = node_bands.get(node_name, [])
            node_results   = results.get(node_name, {})
            node_entry = {"index": idx, "node_name": node_name, "tech": tech, "bands": []}
            summary_data["nodes"].append(node_entry)

            if not bands_for_node and not node_results:
                # Special-case the GSM node: the *usual* cause of "no bands"
                # here is that the commands file has no G-prefixed section,
                # so Phase 1 is skipped for this node. Flag that in the
                # summary instead of showing a bare em-dash.
                if tech == "GSM":
                    self._log_message(
                        "      (no SSH commands — commands file has no "
                        "G-prefixed band section)"
                    )
                    node_entry["note"] = (
                        "No G-prefixed band section in commands file — "
                        "Phase 1 SSH was skipped. Phase 0 sdir and Phase 2 "
                        "ENM (alarm/SHM/GSM-Cell) still ran."
                    )
                else:
                    self._log_message("      (no bands captured on this node)")
                node_entry["bands"].append({"band": "—", "status": "empty"})
                continue

            # Show union of assigned bands + any band that actually recorded
            # a result — catches late additions and mismatches.
            seen = []
            for b in bands_for_node:
                if b not in seen:
                    seen.append(b)
            for b in node_results.keys():
                if b not in seen:
                    seen.append(b)

            for band in seen:
                status = node_results.get(band)
                if status == "ok":
                    tag = "OK"
                elif status == "fail":
                    tag = "FAIL"
                else:
                    # Band was assigned but never reached the Excel stage —
                    # commonly caused by SSH/AMOS phase aborting that band.
                    tag = "NOT CAPTURED"
                self._log_message(f"      {band:<8}{tag}")
                node_entry["bands"].append({"band": band, "status": tag})
                if status == "ok":
                    captured_bands_global.add(band.upper())

        # Bands requested by the commands file but absent from every node.
        missing_bands = [
            b for b in all_bands if b.upper() not in captured_bands_global
            and not any(b in node_bands.get(n, []) for n in ordered_nodes)
        ]
        summary_data["missing_bands"] = list(missing_bands)
        if missing_bands:
            self._log_message(SEP)
            self._log_message("  Not captured on any node:")
            for b in missing_bands:
                self._log_message(
                    f"      {b:<8}— band is not present on any of the "
                    f"configured nodes"
                )

        # Single output folder — every band + every screenshot lives here.
        self._log_message(SEP)
        self._log_message("  Output folder:")
        try:
            output_folder = os.path.abspath(
                get_full_path(config, config.paths.output_dir)
            )
        except Exception:
            output_folder = config.paths.output_dir
        self._log_message(f"      {output_folder}")
        self._log_message(BAR)

        summary_data["output_folder"] = output_folder
        # Hand off to the Result page.
        try:
            self.page.trfs_summary = summary_data
        except Exception:
            pass

    def _run_demo(self, config, all_commands, generated_files):
        from command_parser import filter_gsm_pim
        for idx, (band, categories) in enumerate(all_commands.items()):
            if self.cancelled:
                break
            if band.upper().startswith("G"):
                categories = filter_gsm_pim(band, categories)
                self._log_message(f"[{band}] PIM skipped (GSM requires BSC access)")
            self._update_band_status(band, "running")
            self._log_queue.put_nowait(f"__STATUS__:Simulating {band}")
            self._log_queue.put_nowait("__MODE__:Generating sample screenshots and Excel output.")
            try:
                excel_path = process_band_demo(config, band, categories, is_first_band=(idx == 0))
                generated_files.append(excel_path)
                self._update_band_status(band, "done")
            except Exception as exc:
                self._log_message(f"Error processing band {band}: {exc}")
                self._update_band_status(band, "error")

    def _run_live_multi_node(self, config, all_commands, generated_files):
            """Multi-node live run with automatic band detection via hgetc + sdir.

            Flow (the user's design):

              Phase 0 — sdir + hgetc IN PARALLEL across nodes. Each worker
              opens its OWN SSH session; the stale-AMOS guard in connect()
              + the proper exit-wait in disconnect() keep them isolated at
              the gateway.

              Phase 1 — one SSH session for the whole phase. Run every
              band for node 1, then call ``session.switch_node(node 2)``
              which does ``exit`` → ``amos <node2>`` → ``lt all`` on the
              SAME SSH connection. Keeps the gateway login warm (no
              re-auth) and eliminates any session-pooling ambiguity.
            """
            from config_loader import create_node_config
            from band_detector import detect_bands_from_hgetc

            sdir_nodes = config.site.get_all_nodes()
            gsm_node = config.site.gsm_node_name if config.site.gsm_node_name else None
            all_band_keys = list(all_commands.keys())

            # ── Phase 0: parallel hgetc + sdir across nodes ──
            # Each worker opens its own SSH session → closes it. With the
            # stale-AMOS guard in ssh_runner.connect() and the proper
            # shell-prompt wait in disconnect(), concurrent workers get
            # distinct gateway shells.
            self._log_queue.put_nowait(
                f"__STATUS__:Phase 0: hgetc + sdir (parallel x{len(sdir_nodes)})"
            )
            self._log_queue.put_nowait(
                f"__MODE__:Running hgetc + sdir simultaneously on {len(sdir_nodes)} node(s)."
            )
            self._log_message(
                f"[multi-node] Starting PARALLEL node setup on {len(sdir_nodes)} node(s)"
            )

            node_bands: dict[str, list[str]] = {}
            sdir_screenshots: dict[str, str | None] = {}
            phase0_raw: dict[str, tuple[str, str, str]] = {}  # node -> (sdir_raw, lte, nr)

            def _phase0_worker(node_name: str):
                """Runs in its own thread — opens a dedicated SSH session,
                does hgetc + sdir, closes the session. Returns everything
                the main thread needs for band detection."""
                self._log_message(
                    f"[{node_name}] Opening SSH session (amos → lt all → hgetc → sdir)..."
                )
                try:
                    sdir_path, sdir_raw, lte_output, nr_output = (
                        run_node_setup_standalone(config, node_name)
                    )
                except Exception as exc:
                    self._log_message(f"[{node_name}] Setup raised: {exc}")
                    sdir_path, sdir_raw, lte_output, nr_output = None, "", "", ""

                # Fallback: if setup failed, try sdir-only (still its own
                # session, still properly torn down).
                if not sdir_raw and not lte_output and not nr_output:
                    self._log_message(
                        f"[{node_name}] Setup empty — falling back to sdir-only"
                    )
                    try:
                        sdir_path, sdir_raw = capture_sdir_for_node(config, node_name)
                    except Exception as exc:
                        self._log_message(
                            f"[{node_name}] sdir-only fallback failed: {exc}"
                        )
                        sdir_path, sdir_raw = None, ""
                return node_name, sdir_path, sdir_raw, lte_output, nr_output

            # Run Phase 0 across all nodes in parallel.
            if sdir_nodes:
                with ThreadPoolExecutor(max_workers=len(sdir_nodes)) as _p0:
                    futures = {
                        _p0.submit(_phase0_worker, n): n for n in sdir_nodes
                    }
                    for fut in as_completed(futures):
                        if self.cancelled:
                            break
                        try:
                            node_name, sdir_path, sdir_raw, lte_output, nr_output = fut.result()
                        except Exception as exc:
                            node_name = futures[fut]
                            self._log_message(f"[{node_name}] Phase 0 worker crashed: {exc}")
                            sdir_path, sdir_raw, lte_output, nr_output = None, "", "", ""
                        sdir_screenshots[node_name] = sdir_path
                        phase0_raw[node_name] = (sdir_raw, lte_output, nr_output)

            if self.cancelled:
                return

            # ── Band detection (main thread — order is deterministic) ──
            for node_name in sdir_nodes:
                sdir_raw, lte_output, nr_output = phase0_raw.get(node_name, ("", "", ""))
                sdir_path = sdir_screenshots.get(node_name)
                is_gsm = gsm_node and node_name == gsm_node

                if lte_output or nr_output:
                    detection = detect_bands_from_hgetc(lte_output, nr_output)
                    detected = detection.detected_bands
                    node_band_list = [b for b in detected if b in all_commands]
                    if node_band_list:
                        node_bands[node_name] = node_band_list
                        self._log_message(
                            f"[hgetc] {node_name}: detected bands = [{', '.join(detected)}]"
                        )
                        self._log_message(
                            f"[hgetc] {node_name}: matching commands = [{', '.join(node_band_list)}]"
                        )
                        for line in lte_output.splitlines():
                            stripped = line.strip()
                            if "EUtranCell" in stripped and ";" in stripped:
                                self._log_message(f"  {stripped}")
                        for line in nr_output.splitlines():
                            stripped = line.strip()
                            if "NRCellDU" in stripped and ";" in stripped:
                                self._log_message(f"  {stripped}")
                        for band in node_band_list:
                            self._update_band_status(band, "running")
                        if sdir_path:
                            self._log_message(f"[sdir] {node_name}: VSWR screenshot captured")
                        continue

                if node_name not in node_bands:
                    if sdir_raw:
                        detection = detect_bands_from_sdir(sdir_raw)
                        detected = detection.detected_bands
                        node_band_list = [b for b in detected if b in all_commands]
                        if not node_band_list and not detection.cell_prefixes_found:
                            if is_gsm:
                                node_band_list = [b for b in all_band_keys if b.upper().startswith("G")]
                            else:
                                node_band_list = all_band_keys
                        node_bands[node_name] = node_band_list
                        self._log_message(
                            f"[sdir] {node_name}: detected bands = [{', '.join(detected)}]"
                        )
                        for band in node_band_list:
                            self._update_band_status(band, "running")
                    else:
                        if is_gsm:
                            node_bands[node_name] = [b for b in all_band_keys if b.upper().startswith("G")]
                        else:
                            node_bands[node_name] = all_band_keys
                        self._log_message(
                            f"[{node_name}] All detection failed — using all bands as fallback"
                        )

            if self.cancelled:
                return

            # GSM guard
            if gsm_node:
                if gsm_node not in node_bands:
                    node_bands[gsm_node] = []
                kept_gsm = [b for b in node_bands[gsm_node] if b.upper().startswith("G")]
                if len(kept_gsm) != len(node_bands[gsm_node]):
                    self._log_message(
                        f"[gsm-guard] {gsm_node}: restricted to G-bands = "
                        f"{', '.join(kept_gsm) or '(none)'}"
                    )
                node_bands[gsm_node] = kept_gsm

                # If the commands file has NO G-prefixed band entries at
                # all, the GSM node ends up with zero bands and Phase 1
                # silently skips it. That's the single biggest reason a
                # "3-node run" only produces 2 nodes of output. Make it
                # loud and obvious so the user knows *why*.
                available_g = [b for b in all_band_keys if b.upper().startswith("G")]
                if not kept_gsm and not available_g:
                    self._log_message(
                        f"[gsm-guard] {gsm_node}: no G-prefixed band section "
                        f"(e.g. 'FOR G900') was found in the commands file — "
                        f"the GSM node will be skipped in Phase 1 (SSH moshell). "
                        f"Phase 0 (sdir) and Phase 2 (ENM alarm/SHM/GSM-Cell) "
                        f"still run."
                    )
                elif not kept_gsm and available_g:
                    self._log_message(
                        f"[gsm-guard] {gsm_node}: detection returned no G-bands. "
                        f"Falling back to commands-file G-bands = "
                        f"{', '.join(available_g)}"
                    )
                    node_bands[gsm_node] = list(available_g)
                    for band in available_g:
                        self._update_band_status(band, "running")

            # Dedupe bands
            _claimed_bands: set[str] = set()
            for node_name in sdir_nodes:
                kept = []
                for b in node_bands.get(node_name, []):
                    key = b.upper()
                    if key in _claimed_bands:
                        self._log_message(f"[dedupe] {node_name}: dropping {b} (already owned by earlier node)")
                        continue
                    _claimed_bands.add(key)
                    kept.append(b)
                node_bands[node_name] = kept

            # Filter PIM for GSM
            from command_parser import filter_gsm_pim
            for node_name in sdir_nodes:
                gsm_bands = [b for b in node_bands.get(node_name, []) if b.upper().startswith("G")]
                for gb in gsm_bands:
                    commands = all_commands.get(gb, {})
                    if commands:
                        all_commands[gb] = filter_gsm_pim(gb, commands)
                        self._log_message(f"[{gb}] PIM skipped (GSM requires BSC access)")
                        self._update_band_status(gb, "running")

            # ── Phase 1: ONE SSH session, switch_node() between nodes ──
            # Exactly the flow the user asked for:
            #   • Open one SSH session, log into node 1 (amos + lt all)
            #   • Run every band for node 1
            #   • session.switch_node(node 2) → inside the SAME SSH channel
            #     it sends `exit` (drop out of AMOS back to gateway shell),
            #     then `amos <node 2>`, then `lt all`. No new SSH handshake,
            #     no gateway re-auth, no risk of the gateway pooling a stale
            #     backend shell between sessions.
            #   • Run every band for node 2
            #   • Repeat for any further nodes.
            self._log_queue.put_nowait("__STATUS__:Phase 1: moshell SSH commands")
            self._log_queue.put_nowait(
                "__MODE__:One SSH session; switching AMOS node between bands."
            )

            phase1_results: dict[str, tuple[str, dict]] = {}
            phase1_nodes = [n for n in sdir_nodes if node_bands.get(n)]

            if phase1_nodes:
                # Seed the session on the first node.
                first_node = phase1_nodes[0]
                first_config = create_node_config(config, first_node)
                self._log_message(
                    f"[{first_node}] Opening Phase 1 SSH session "
                    f"({len(node_bands.get(first_node, []))} band(s))"
                )
                self._log_queue.put_nowait(f"__STATUS__:Phase 1: {first_node}")

                try:
                    with MoshellSession(first_config) as session:
                        for idx, node_name in enumerate(phase1_nodes):
                            if self.cancelled:
                                break
                            bands_for_node = node_bands.get(node_name, [])
                            if not bands_for_node:
                                continue

                            # Switch the live session onto this node (skip
                            # on idx 0 — we entered the with-block already
                            # logged into phase1_nodes[0]).
                            if idx > 0:
                                self._log_message(
                                    f"[{node_name}] Switching AMOS session "
                                    f"(exit → amos {node_name} → lt all)"
                                )
                                self._log_queue.put_nowait(
                                    f"__STATUS__:Phase 1: {node_name}"
                                )
                                try:
                                    session.switch_node(node_name)
                                except Exception as exc:
                                    self._log_message(
                                        f"[{node_name}] switch_node failed: {exc}"
                                    )
                                    for band in bands_for_node:
                                        self._update_band_status(band, "error")
                                    continue

                            node_config = create_node_config(config, node_name)
                            shared_sdir = sdir_screenshots.get(node_name)

                            for i, band in enumerate(bands_for_node):
                                if self.cancelled:
                                    break
                                commands = all_commands.get(band, {})
                                if not commands:
                                    continue
                                try:
                                    excel_path, moshell = run_moshell_for_band(
                                        session, node_config, band, commands,
                                        is_first_band=(i == 0),
                                        shared_sdir_screenshot=shared_sdir,
                                        node_name=node_name,
                                    )
                                    phase1_results[f"{node_name}::{band}"] = (
                                        excel_path, moshell
                                    )
                                    self._log_message(
                                        f"[{node_name}/{band}] Moshell phase done"
                                    )
                                except Exception as exc:
                                    self._log_message(
                                        f"[{node_name}/{band}] Moshell error: {exc}"
                                    )
                                    self._update_band_status(band, "error")
                except Exception as exc:
                    self._log_message(
                        f"[phase1] SSH session failed: {exc} — no bands captured"
                    )
                    for node_name in phase1_nodes:
                        for band in node_bands.get(node_name, []):
                            self._update_band_status(band, "error")

            if self.cancelled:
                return

            # Phase 2: ENM capture per node
            self._log_queue.put_nowait("__STATUS__:Phase 2: ENM capture")
            self._log_queue.put_nowait("__MODE__:Capturing ENM Cell Management, SHM, and Alarm screenshots.")

            from enm_capture import ensure_chromium_installed
            chromium_ok = ensure_chromium_installed()

            enm_artifacts_per_node: dict[str, dict] = {}
            empty_enm = {
                "shm": None, "alarm": None,
                "cellmgmt_nr": None, "cellmgmt_lte": None, "cellmgmt_gsm": None,
            }

            def _phase2_one_node(node_name: str) -> tuple[str, dict]:
                # NOTE: do NOT skip when node_bands[node_name] is empty.
                # GSM nodes often have zero G-band entries in the commands
                # file, which would otherwise strand their alarm/SHM/GSM-Cell
                # screenshots. ENM outputs are per-node (not per-band), so
                # every sdir node that reached Phase 0 deserves an ENM pass.
                if not chromium_ok:
                    return node_name, dict(empty_enm)
                if not node_bands.get(node_name):
                    self._log_message(
                        f"[ENM] {node_name}: no matching bands — running ENM "
                        f"anyway (alarm/SHM/cell-mgmt are per-node artifacts)"
                    )
                node_config = create_node_config(config, node_name)
                try:
                    shared_enm = capture_enm_shared_artifacts(node_config)
                    parts = []
                    if shared_enm.get("cellmgmt_nr") is not None:
                        parts.append(f"NR={'ok' if shared_enm['cellmgmt_nr'] else 'FAIL'}")
                    if shared_enm.get("cellmgmt_lte") is not None:
                        parts.append(f"LTE={'ok' if shared_enm['cellmgmt_lte'] else 'FAIL'}")
                    if shared_enm.get("cellmgmt_gsm") is not None:
                        parts.append(f"GSM={'ok' if shared_enm['cellmgmt_gsm'] else 'FAIL'}")
                    parts.append(f"ALARM={'ok' if shared_enm.get('alarm') else 'FAIL'}")
                    parts.append(f"SHM={'ok' if shared_enm.get('shm') else 'FAIL'}")
                    self._log_message(f"[ENM] {node_name}: " + ", ".join(parts))
                    return node_name, shared_enm
                except Exception as exc:
                    self._log_message(f"[ENM] {node_name}: capture failed — {exc}")
                    return node_name, dict(empty_enm)

            # Each node gets its own Playwright browser session; run them in
            # parallel so total ENM wall time is driven by the slowest node.
            if sdir_nodes:
                from concurrent.futures import ThreadPoolExecutor as _P2Pool
                with _P2Pool(max_workers=max(1, len(sdir_nodes))) as _pool:
                    for n, art in _pool.map(_phase2_one_node, sdir_nodes):
                        enm_artifacts_per_node[n] = art

            if self.cancelled:
                return

            # Phase 3: Insert screenshots into Excel
            self._log_queue.put_nowait("__STATUS__:Phase 3: building Excel reports")
            self._log_queue.put_nowait("__MODE__:Merging moshell + ENM screenshots per band.")

            for key, (excel_path, moshell) in phase1_results.items():
                if self.cancelled:
                    break
                node_name, band = key.split("::", 1)
                categories = all_commands.get(band, {})
                shared_enm = enm_artifacts_per_node.get(node_name, empty_enm)
                try:
                    enm_for_band = build_enm_screenshots_for_band(
                        band, categories, shared_enm, config
                    )
                    all_screenshots = {**moshell, **enm_for_band}

                    if not os.path.exists(excel_path):
                        self._log_message(f"[{band}] Excel missing — re-creating")
                        excel_path = create_band_excel(config, band)

                    try:
                        insert_screenshots_for_band(excel_path, all_screenshots)
                    except FileNotFoundError:
                        self._log_message(f"[{band}] Excel missing at insert — re-creating")
                        excel_path = create_band_excel(config, band)
                        insert_screenshots_for_band(excel_path, all_screenshots)

                    generated_files.append(excel_path)
                    self._update_band_status(band, "done")
                    self._band_results.setdefault(node_name, {})[band] = "ok"
                    self._log_message(f"[{band}] Complete -> {excel_path}")
                except BaseException as exc:
                    tb = traceback.format_exc()
                    self._log_message(f"[{band}] Excel phase error: {exc}")
                    for line in tb.splitlines():
                        self._log_message(f"  {line}")
                    self._update_band_status(band, "error")
                    self._band_results.setdefault(node_name, {})[band] = "fail"
                finally:
                    gc.collect()

            # Snapshot the node→bands assignment for _emit_summary.
            self._node_bands_snapshot = {n: list(bs) for n, bs in node_bands.items()}

    def _run_live(self, config, all_commands, generated_files):
        """
        Three-phase live run:
          1. Moshell SSH in parallel (each band = one dedicated SSH session)
          2. Shared ENM browser capture (one browser, four artifacts max)
          3. Distribute ENM artifacts per-band + insert everything into Excel

        When a second node (node2_name) is configured, runs the multi-node
        flow with automatic band detection from sdir.
        """
        # ── Multi-node path ──────────────────────────────────────────
        # Diagnostic — prove exactly which branch we take and why.
        self._log_message(
            f"[dispatch] node_name={config.site.node_name!r} "
            f"node2_name={config.site.node2_name!r} "
            f"gsm_node_name={getattr(config.site, 'gsm_node_name', '')!r}"
        )
        if config.site.node2_name or getattr(config.site, "gsm_node_name", ""):
            self._log_message("[dispatch] -> MULTI-NODE path (node2 or GSM present)")
            self._run_live_multi_node(config, all_commands, generated_files)
            return
        self._log_message("[dispatch] -> SINGLE-NODE path (no node2/GSM configured)")

        # ── Single-node path (original flow) ───────────────────────────
        # Include all bands (LTE, NR, GSM). PIM is filtered for GSM bands.
        from command_parser import filter_gsm_pim
        all_items = list(all_commands.items())
        band_items = []
        for b, c in all_items:
            if b.upper().startswith("G"):
                c = filter_gsm_pim(b, c)
                self._log_message(f"[{b}] PIM skipped (GSM requires BSC access)")
            band_items.append((b, c))
        # Snapshot the (only) node's band assignment for _emit_summary.
        self._node_bands_snapshot = {
            config.site.node_name: [b for b, _ in band_items]
        }
        # Cap parallel SSH sessions to 3 — running all 8 bands at once has
        # caused instability in the past. Extra bands queue and start as
        # slots free up.
        PARALLEL = max(1, min(3, len(band_items)))

        # Phase 0 — shared sdir capture (one SSH session, reused by all bands)
        self._log_queue.put_nowait("__STATUS__:Phase 0/4: take sdir command")
        self._log_queue.put_nowait("__MODE__:Running sdir once (up to 15 min) — result is shared across all bands.")
        self._log_message("[sdir] Taking sdir command (shared across all bands)...")
        try:
            shared_sdir_path = capture_shared_sdir_screenshot(config)
        except Exception as exc:
            self._log_message(f"[sdir] Shared capture raised: {exc}")
            logging.getLogger(__name__).exception("Shared sdir capture failed")
            shared_sdir_path = None
        if shared_sdir_path:
            self._log_message(f"[sdir] Shared capture OK -> {shared_sdir_path}")
        else:
            self._log_message("[sdir] Shared capture FAILED — VSWR sdir entries will be skipped")

        if self.cancelled:
            return

        # Phase 1 — SSH parallel (all bands concurrently)
        self._log_queue.put_nowait(f"__STATUS__:Phase 1/4: moshell SSH (parallel x{PARALLEL})")
        self._log_queue.put_nowait(f"__MODE__:Processing {len(band_items)} bands — each opens its own SSH session.")
        for band, _ in band_items:
            self._update_band_status(band, "running")

        phase1: dict = {}  # band -> (excel_path, moshell_screenshots)
        lock = threading.Lock()

        def _moshell_worker(band: str, categories: dict):
            self._log_message(f"[{band}] Opening SSH session...")
            with MoshellSession(config) as session:
                self._log_message(f"[{band}] SSH connected, running moshell commands.")
                return run_moshell_for_band(
                    session, config, band, categories, is_first_band=True,
                    shared_sdir_screenshot=shared_sdir_path,
                )

        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = {
                pool.submit(_moshell_worker, band, cats): band
                for band, cats in band_items
            }
            for fut in as_completed(futures):
                if self.cancelled:
                    break
                band = futures[fut]
                try:
                    excel_path, moshell = fut.result()
                    with lock:
                        phase1[band] = (excel_path, moshell)
                    self._log_message(f"[{band}] Moshell phase done")
                except Exception as exc:
                    self._log_message(f"[{band}] Moshell phase error: {exc}")
                    self._update_band_status(band, "error")

        if self.cancelled:
            return

        # Phase 2 — shared ENM (one browser session, four captures max)
        # Wrap in a thread with a hard timeout so a Playwright hang never
        # blocks the entire run indefinitely.
        ENM_TIMEOUT_SECONDS = 180  # 3 minutes max for all ENM captures
        self._log_queue.put_nowait("__STATUS__:Phase 2/4: shared ENM capture")
        self._log_queue.put_nowait("__MODE__:Checking Chromium browser...")
        # Auto-install Chromium if missing (first run on a new machine)
        from enm_capture import ensure_chromium_installed
        chromium_ok = ensure_chromium_installed()
        if chromium_ok:
            self._log_message("[ENM] Chromium browser is ready.")
        else:
            self._log_message("[ENM] Chromium not available — ENM screenshots will be skipped.")
        self._log_queue.put_nowait("__MODE__:Opening one Chromium session — SHM, Alarm, Cell Management (NR + LTE).")
        self._log_message("[ENM] Starting shared ENM capture phase.")
        debug_msg = (
            f"[ENM][debug] config.enm is {'set' if config.enm else 'None'}"
            + (f", enabled={config.enm.enabled}, url={config.enm.url!r}" if config.enm else "")
        )
        self._log_message(debug_msg)
        logging.getLogger(__name__).info(debug_msg)
        shared_enm = {
            "shm": None, "alarm": None,
            "cellmgmt_nr": None, "cellmgmt_lte": None, "cellmgmt_gsm": None,
        }
        if not chromium_ok:
            self._log_message("[ENM] Skipping browser capture (no Chromium).")
        else:
            try:
                enm_future = ThreadPoolExecutor(max_workers=1).submit(
                    capture_enm_shared_artifacts, config
                )
                shared_enm = enm_future.result(timeout=ENM_TIMEOUT_SECONDS)
            except TimeoutError:
                self._log_message(f"[ENM] Timed out after {ENM_TIMEOUT_SECONDS}s — skipping ENM screenshots")
                logging.getLogger(__name__).warning("ENM capture timed out after %ds", ENM_TIMEOUT_SECONDS)
            except Exception as exc:
                self._log_message(f"[ENM] Shared capture raised: {exc}")
                logging.getLogger(__name__).exception("Shared ENM capture failed")
        self._log_message(
            f"[ENM] Shared capture complete: "
            f"SHM={'ok' if shared_enm.get('shm') else 'FAIL'}, "
            f"Alarm={'ok' if shared_enm.get('alarm') else 'FAIL'}, "
            f"CellNR={'ok' if shared_enm.get('cellmgmt_nr') else 'FAIL'}, "
            f"CellLTE={'ok' if shared_enm.get('cellmgmt_lte') else 'FAIL'}, "
            f"CellGSM={'ok' if shared_enm.get('cellmgmt_gsm') else 'FAIL'}"
        )

        if self.cancelled:
            return

        # Phase 2b — SRS (placeholder: feature still in development)
        srs_screenshots: dict[str, str] = {}  # category -> path
        self._log_queue.put_nowait("__STATUS__:Phase 2b: SRS screenshot")
        self._log_queue.put_nowait("__MODE__:SRS screenshot is being developed, not finish yet.")
        self._log_message("[SRS] SRS screenshot is being developed, not finish yet — skipping.")

        if self.cancelled:
            return

        # Phase 3 — insert everything into Excel per-band
        self._log_queue.put_nowait("__STATUS__:Phase 3/4: building Excel reports")
        self._log_queue.put_nowait("__MODE__:Merging moshell + ENM + SRS screenshots per band.")

        for band, (excel_path, moshell) in phase1.items():
            if self.cancelled:
                break
            self._log_message(f"[Phase3] starting band {band}")
            try:
                categories = all_commands[band]
                enm_for_band = build_enm_screenshots_for_band(
                    band, categories, shared_enm, config
                )
                all_screenshots = {**moshell, **enm_for_band, **srs_screenshots}

                if not os.path.exists(excel_path):
                    self._log_message(f"[{band}] Excel missing — re-creating from template")
                    excel_path = create_band_excel(config, band)

                try:
                    insert_screenshots_for_band(excel_path, all_screenshots)
                except FileNotFoundError:
                    self._log_message(f"[{band}] Excel missing at insert — re-creating")
                    excel_path = create_band_excel(config, band)
                    insert_screenshots_for_band(excel_path, all_screenshots)

                generated_files.append(excel_path)
                self._update_band_status(band, "done")
                self._band_results.setdefault(config.site.node_name, {})[band] = "ok"
                self._log_message(f"[{band}] Complete -> {excel_path}")
            except BaseException as exc:
                tb = traceback.format_exc()
                self._log_message(f"[{band}] Excel phase error: {exc}")
                for line in tb.splitlines():
                    self._log_message(f"  {line}")
                logging.getLogger(__name__).exception(
                    "[%s] Excel phase crashed", band
                )
                self._update_band_status(band, "error")
                self._band_results.setdefault(config.site.node_name, {})[band] = "fail"
                if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
                    raise
            finally:
                # Release openpyxl/PIL image buffers between bands so we
                # don't accumulate hundreds of PNGs in memory across 8 bands.
                gc.collect()
