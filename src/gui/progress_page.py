"""
TRFS GUI - Live run monitor.
"""
import asyncio
import gc
import logging
import os
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
)
from sdir_capture import capture_shared_sdir_screenshot
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
    def __init__(self, log_callback):
        super().__init__()
        self.log_callback = log_callback

    def emit(self, record):
        self.log_callback(self.format(record))


class ProgressPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.cancelled = False
        self._log_handler = None
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
        band_grid_controls = []
        for band in band_names:
            label = ft.Text(band, size=14, weight=ft.FontWeight.BOLD, color=TEXT)
            pct_text = ft.Text("0%", size=11, color=TEXT_MUTED)
            state = ft.Text("Queued", size=11, color=TEXT_MUTED)
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
                            [label, ft.Row([pct_text, state], spacing=6)],
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
        # Track per-band progress from log messages like "  [L900] VSWR: Running..."
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

    def _update_band_progress(self, band: str):
        """Increment band progress by one step and update the bar."""
        card = self.band_cards.get(band)
        if not card:
            return
        self.band_current_step[band] = min(
            self.band_current_step[band] + 1, self.band_total_steps[band]
        )
        pct = self.band_current_step[band] / self.band_total_steps[band]
        card["pct_text"].value = f"{int(pct * 100)}%"
        card["progress_bar"].value = pct
        try:
            card["progress_bar"].update()
            card["pct_text"].update()
        except Exception:
            pass

    def _update_band_status(self, band: str, status: str):
        card = self.band_cards.get(band)
        if not card:
            return

        container: ft.Container = card["container"]
        state_text: ft.Text = card["state"]
        pct_text: ft.Text = card["pct_text"]
        progress_bar: ft.ProgressBar = card["progress_bar"]

        if status == "running":
            container.bgcolor = ft.Colors.with_opacity(0.16, INFO)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.38, INFO))
            state_text.value = "Running"
            state_text.color = INFO
            pct_text.color = INFO
            pct_text.value = "0%"
            progress_bar.color = INFO
            progress_bar.value = 0
        elif status == "done":
            container.bgcolor = ft.Colors.with_opacity(0.16, SUCCESS)
            container.border = ft.Border.all(1, ft.Colors.with_opacity(0.38, SUCCESS))
            state_text.value = "Complete"
            state_text.color = SUCCESS
            pct_text.value = "100%"
            pct_text.color = SUCCESS
            progress_bar.color = SUCCESS
            progress_bar.value = 1.0
        elif status == "error":
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
            progress_bar.color = TEXT_MUTED
            progress_bar.value = 0

        try:
            container.update()
        except Exception:
            pass

    def _start_run(self, config, demo_mode, all_commands):
        handler = FletLogHandler(self._log_message)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

        # Non-daemon so Python waits for this thread on shutdown instead of
        # killing it mid-Excel-write when the Flet session is GC'd.
        thread = threading.Thread(
            target=self._run_worker,
            args=(config, demo_mode, all_commands),
            daemon=False,
        )
        thread.start()
        self.page.trfs_run_thread = thread

    def _run_worker(self, config, demo_mode, all_commands):
        generated_files = []
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
        self.status_text.value = "Automation complete"
        self.mode_text.value = f"Run finished in {duration}."
        self.elapsed_text.value = ""
        self.back_button.disabled = False

        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)

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

    def _run_demo(self, config, all_commands, generated_files):
        for idx, (band, categories) in enumerate(all_commands.items()):
            if self.cancelled:
                break
            if band.upper().startswith("G"):
                self._log_message(f"[{band}] Skipped — GSM flow not implemented yet")
                self._update_band_status(band, "skipped")
                continue
            self._update_band_status(band, "running")
            self.status_text.value = f"Simulating {band}"
            self.mode_text.value = "Generating sample screenshots and Excel output."
            self.elapsed_text.value = f"Elapsed {datetime.now() - self.start_time}"
            try:
                self.page.update()
            except Exception:
                pass
            try:
                excel_path = process_band_demo(config, band, categories, is_first_band=(idx == 0))
                generated_files.append(excel_path)
                self._update_band_status(band, "done")
            except Exception as exc:
                self._log_message(f"Error processing band {band}: {exc}")
                self._update_band_status(band, "error")

    def _run_live(self, config, all_commands, generated_files):
        """
        Three-phase live run:
          1. Moshell SSH in parallel (each band = one dedicated SSH session)
          2. Shared ENM browser capture (one browser, four artifacts max)
          3. Distribute ENM artifacts per-band + insert everything into Excel
        """
        # Filter out GSM bands — GSM flow is not implemented yet.
        all_items = list(all_commands.items())
        gsm_items = [(b, c) for b, c in all_items if b.upper().startswith("G")]
        band_items = [(b, c) for b, c in all_items if not b.upper().startswith("G")]
        for gsm_band, _ in gsm_items:
            self._log_message(f"[{gsm_band}] Skipped — GSM flow not implemented yet")
            self._update_band_status(gsm_band, "skipped")
        # Cap parallel SSH sessions to 3 — running all 8 bands at once has
        # caused instability in the past. Extra bands queue and start as
        # slots free up.
        PARALLEL = max(1, min(3, len(band_items)))

        # Phase 0 — shared sdir capture (one SSH session, reused by all bands)
        self.status_text.value = "Phase 0/3: take sdir command"
        self.mode_text.value = "Running sdir once (up to 15 min) — result is shared across all bands."
        try:
            self.page.update()
        except Exception:
            pass
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
        self.status_text.value = f"Phase 1/3: moshell SSH (parallel x{PARALLEL})"
        self.mode_text.value = f"Processing {len(band_items)} bands — each opens its own SSH session."
        for band, _ in band_items:
            self._update_band_status(band, "running")
        try:
            self.page.update()
        except Exception:
            pass

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

                self.elapsed_text.value = f"Elapsed {datetime.now() - self.start_time}"
                try:
                    self.page.update()
                except Exception:
                    pass

        if self.cancelled:
            return

        # Phase 2 — shared ENM (one browser session, four captures max)
        self.status_text.value = "Phase 2/3: shared ENM capture"
        self.mode_text.value = "Opening one Chromium session — SHM, Alarm, Cell Management (NR + LTE)."
        try:
            self.page.update()
        except Exception:
            pass
        self._log_message("[ENM] Starting shared ENM capture phase.")
        debug_msg = (
            f"[ENM][debug] config.enm is {'set' if config.enm else 'None'}"
            + (f", enabled={config.enm.enabled}, url={config.enm.url!r}" if config.enm else "")
        )
        self._log_message(debug_msg)
        logging.getLogger(__name__).info(debug_msg)
        try:
            shared_enm = capture_enm_shared_artifacts(config)
        except Exception as exc:
            self._log_message(f"[ENM] Shared capture raised: {exc}")
            logging.getLogger(__name__).exception("Shared ENM capture failed")
            shared_enm = {"shm": None, "alarm": None, "cellmgmt_nr": None, "cellmgmt_lte": None}
        self._log_message(
            f"[ENM] Shared capture complete: "
            f"SHM={'ok' if shared_enm['shm'] else 'FAIL'}, "
            f"Alarm={'ok' if shared_enm['alarm'] else 'FAIL'}, "
            f"CellNR={'ok' if shared_enm['cellmgmt_nr'] else 'FAIL'}, "
            f"CellLTE={'ok' if shared_enm['cellmgmt_lte'] else 'FAIL'}"
        )

        if self.cancelled:
            return

        # Phase 3 — insert everything into Excel per-band
        self.status_text.value = "Phase 3/3: building Excel reports"
        self.mode_text.value = "Merging moshell + ENM screenshots per band."
        try:
            self.page.update()
        except Exception:
            pass

        for band, (excel_path, moshell) in phase1.items():
            if self.cancelled:
                break
            self._log_message(f"[Phase3] starting band {band}")
            try:
                categories = all_commands[band]
                enm_for_band = build_enm_screenshots_for_band(
                    band, categories, shared_enm, config
                )
                all_screenshots = {**moshell, **enm_for_band}

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
                if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
                    raise
            finally:
                # Release openpyxl/PIL image buffers between bands so we
                # don't accumulate hundreds of PNGs in memory across 8 bands.
                gc.collect()

            self.elapsed_text.value = f"Elapsed {datetime.now() - self.start_time}"
            try:
                self.page.update()
            except Exception:
                pass
