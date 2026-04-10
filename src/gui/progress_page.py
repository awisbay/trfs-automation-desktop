"""
TRFS GUI - Progress Page with live log viewer and band status indicators.
"""
import asyncio
import os
import sys
import logging
import threading
from datetime import datetime

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config_loader import get_full_path
from command_parser import parse_commands_file
from main import process_band, process_band_demo
from ssh_runner import MoshellSession


class FletLogHandler(logging.Handler):
    def __init__(self, log_callback):
        super().__init__()
        self.log_callback = log_callback

    def emit(self, record):
        msg = self.format(record)
        self.log_callback(msg)


class ProgressPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.cancelled = False
        self.log_lines = []
        self.band_status = {}
        self._log_handler = None

    def build(self) -> ft.View:
        config = getattr(self.page, 'trfs_config', None)
        demo_mode = getattr(self.page, 'trfs_demo_mode', False)
        commands_file = getattr(self.page, 'trfs_commands_file', '')

        all_commands = parse_commands_file(commands_file)
        band_names = list(all_commands.keys())

        self.band_indicators = {}
        for band in band_names:
            indicator = ft.Container(
                content=ft.Text(band, size=11, weight=ft.FontWeight.BOLD),
                bgcolor=ft.Colors.GREY_800,
                border=ft.border.all(1, ft.Colors.GREY_600),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                alignment=ft.Alignment(0, 0),
            )
            self.band_indicators[band] = indicator

        self.log_list = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True)
        self.status_text = ft.Text("Initializing...", size=13, italic=True)
        self.elapsed_text = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self.start_time = datetime.now()

        band_row = ft.Row(
            [self.band_indicators[b] for b in band_names],
            wrap=True,
            spacing=6,
        )

        self._start_run(config, demo_mode, commands_file, all_commands)

        return ft.View(
            route="/progress",
            appbar=ft.AppBar(
                title=ft.Text("TRFS - Running", size=18, weight=ft.FontWeight.BOLD),
                bgcolor=ft.Colors.BLUE_900,
                automatically_imply_leading=False,
            ),
            controls=[
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text("Band Progress", size=14, weight=ft.FontWeight.W_600),
                            band_row,
                            ft.Divider(),
                            self.status_text,
                            self.elapsed_text,
                            ft.Divider(),
                            ft.Text("Log Output", size=13, weight=ft.FontWeight.W_500),
                            self.log_list,
                        ],
                        spacing=8,
                        expand=True,
                    ),
                    padding=20,
                    expand=True,
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
        )

    def _log_message(self, msg: str):
        self.log_lines.append(msg)

        color = ft.Colors.GREY_300
        prefix = ""
        if "[WARNING]" in msg:
            color = ft.Colors.YELLOW_300
        elif "[ERROR]" in msg:
            color = ft.Colors.RED_400
        elif "[INFO]" in msg:
            prefix = "● "
            color = ft.Colors.BLUE_200
        elif "Completed" in msg or "OK" in msg:
            color = ft.Colors.GREEN_300

        self.log_list.controls.append(
            ft.Text(f"{prefix}{msg}", size=11, color=color, selectable=True)
        )

        if len(self.log_list.controls) > 500:
            self.log_list.controls = self.log_list.controls[-500:]

        try:
            self.page.update()
        except Exception:
            pass

    def _update_band_status(self, band: str, status: str):
        indicator = self.band_indicators.get(band)
        if not indicator:
            return
        if status == "running":
            indicator.bgcolor = ft.Colors.BLUE_700
            indicator.border = ft.border.all(1, ft.Colors.BLUE_300)
        elif status == "done":
            indicator.bgcolor = ft.Colors.GREEN_700
            indicator.border = ft.border.all(1, ft.Colors.GREEN_300)
        elif status == "error":
            indicator.bgcolor = ft.Colors.RED_700
            indicator.border = ft.border.all(1, ft.Colors.RED_300)
        try:
            self.page.update()
        except Exception:
            pass

    def _start_run(self, config, demo_mode, commands_file, all_commands):
        log_handler = FletLogHandler(self._log_message)
        log_handler.setLevel(logging.INFO)
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(log_handler)
        self._log_handler = log_handler

        thread = threading.Thread(
            target=self._run_worker,
            args=(config, demo_mode, all_commands),
            daemon=True,
        )
        thread.start()

        self.page.trfs_run_thread = thread

    def _run_worker(self, config, demo_mode, all_commands):
        generated_files = []
        self.status_text.value = "Running..."

        try:
            if demo_mode:
                self._log_message("Starting in DEMO mode (no SSH connection)")
                self._run_demo(config, all_commands, generated_files)
            else:
                self._log_message(f"Connecting to {config.ssh.host}:{config.ssh.port}...")
                self._run_live(config, all_commands, generated_files)
        except Exception as e:
            self._log_message(f"FATAL ERROR: {e}")
            logging.getLogger(__name__).error(f"Fatal error: {e}", exc_info=True)

        self.page.trfs_generated_files = generated_files
        duration = datetime.now() - self.start_time
        self.status_text.value = f"Complete! ({duration})"
        self.elapsed_text.value = ""

        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)

        try:
            asyncio.create_task(self.page.push_route("/result"))
        except Exception:
            pass

    def _run_demo(self, config, all_commands, generated_files):
        for i, (band, categories) in enumerate(all_commands.items()):
            if self.cancelled:
                break
            self._update_band_status(band, "running")
            self.status_text.value = f"Processing band: {band} (DEMO)"
            remaining = len(all_commands) - i - 1
            self.elapsed_text.value = f"Elapsed: {datetime.now() - self.start_time} | Remaining bands: {remaining}"
            try:
                self.page.update()
            except Exception:
                pass
            try:
                excel_path = process_band_demo(config, band, categories, is_first_band=(i == 0))
                generated_files.append(excel_path)
                self._update_band_status(band, "done")
            except Exception as e:
                self._log_message(f"Error processing band {band}: {e}")
                self._update_band_status(band, "error")

    def _run_live(self, config, all_commands, generated_files):
        try:
            with MoshellSession(config) as session:
                self._log_message("SSH connection established. Loading MO tree...")
                for i, (band, categories) in enumerate(all_commands.items()):
                    if self.cancelled:
                        break
                    self._update_band_status(band, "running")
                    self.status_text.value = f"Processing band: {band}"
                    remaining = len(all_commands) - i - 1
                    self.elapsed_text.value = f"Elapsed: {datetime.now() - self.start_time} | Remaining bands: {remaining}"
                    try:
                        self.page.update()
                    except Exception:
                        pass
                    try:
                        from main import process_band
                        excel_path = process_band(session, config, band, categories, is_first_band=(i == 0))
                        generated_files.append(excel_path)
                        self._update_band_status(band, "done")
                    except Exception as e:
                        self._log_message(f"Error processing band {band}: {e}")
                        self._update_band_status(band, "error")
        except Exception as e:
            self._log_message(f"Connection error: {e}")
            for band in all_commands:
                if self.band_indicators.get(band, {}).bgcolor != ft.Colors.GREEN_700:
                    self._update_band_status(band, "error")