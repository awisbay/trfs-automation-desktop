"""
Integration workflow — step selection checklist + two-column progress for LTE/NR and GSM nodes.

Two phases:
  /integration      — Step selector (checkboxes, Select/Deselect All, Run button)
  /integration_run  — Progress page (two node columns, high-level log, timer)
"""
import asyncio
import logging
import os
import queue
import sys
import threading
from datetime import datetime

import flet as ft

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
    badge,
    panel,
    secondary_button_style,
)

logger = logging.getLogger(__name__)

# ── Step definitions ─────────────────────────────────────────────
# Each tuple: (key, display_label, applies_to, log_suffix)
#   applies_to: "both" | "lte_nr" | "gsm"
#   log_suffix: short name used in the log filename
INTEGRATION_STEPS = [
    ("create_arne",      "Create ARNE",              "both",    "ARNE"),
    ("enrollment",       "Enrollment",               "both",    "ENROLLMENT"),
    ("install_lkf",      "Install LKF",              "both",    "LKF"),
    ("relation",         "Relation",                 "lte_nr",  "RELATION"),
    ("baseline",         "Baseline Running",         "lte_nr",  "BASELINE"),
    ("ret_scripts",      "RET Scripts",              "both",    "RET"),
    ("uri_setting",      "URI Setting",              "lte_nr",  "URI"),
    ("verify_mme",       "Verify MME",               "both",    "MME"),
    ("gsm_cell_define",  "GSM Cell Define in BSC",   "gsm",     "GSM_CELL_DEFINE"),
    ("pm_measurement",   "PM Measurement",           "both",    "PM"),
    ("take_dump",        "Take Dump",                "both",    "DUMP"),
    ("take_cm_dump",     "Take CM Dump",             "both",    "CM_DUMP"),
]


# ═════════════════════════════════════════════════════════════════
#  Phase 1 — Step Selection Checklist
# ═════════════════════════════════════════════════════════════════
class IntegrationPage:
    """Checklist page where user picks which integration steps to run."""

    def __init__(self, page: ft.Page):
        self.page = page
        self.form = getattr(page, "integration_form", {})

        self.lte_name = self.form.get("node_name", "LTE/NR Node")
        self.lte_ip = self.form.get("node_ip", "")
        self.lte_subnet = self.form.get("subnetwork", "")
        self.gsm_name = self.form.get("gsm_node_name", "")
        self.gsm_ip = self.form.get("gsm_node_ip", "")
        self.gsm_subnet = self.form.get("gsm_subnetwork", "")
        self.has_gsm = bool(self.gsm_name)
        self.shortcode = self.form.get("shortcode", "UNKNOWN")

        # Checkboxes keyed by step key
        self.checkboxes: dict[str, ft.Checkbox] = {}

    def build(self) -> ft.View:
        # ── Build checkboxes ────────────────────────────────────
        check_rows = []
        for key, label, applies_to, _ in INTEGRATION_STEPS:
            # Determine if this step is relevant
            relevant = False
            if applies_to == "both":
                relevant = True
            elif applies_to == "lte_nr":
                relevant = True  # always show LTE steps
            elif applies_to == "gsm" and self.has_gsm:
                relevant = True

            if not relevant:
                continue

            # Tag for display
            if applies_to == "lte_nr":
                tag_text = "LTE/NR"
                tag_color = INFO
            elif applies_to == "gsm":
                tag_text = "GSM"
                tag_color = ACCENT_WARM
            else:
                tag_text = "Both"
                tag_color = ACCENT

            cb = ft.Checkbox(
                value=True,
                label=None,
                active_color=ACCENT,
                check_color="#06242A",
                on_change=self._on_checkbox_change,
            )
            self.checkboxes[key] = cb

            tag = ft.Container(
                content=ft.Text(tag_text, size=10, color="#06242A",
                                weight=ft.FontWeight.BOLD),
                bgcolor=tag_color,
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                width=55,
                alignment=ft.alignment.center,
            )

            row = ft.Container(
                padding=ft.Padding.symmetric(horizontal=12, vertical=4),
                border_radius=10,
                ink=True,
                on_click=lambda e, k=key: self._toggle_checkbox(k),
                content=ft.Row(
                    [
                        cb,
                        ft.Text(label, size=14, color=TEXT,
                                weight=ft.FontWeight.W_500),
                        ft.Container(expand=True),
                        tag,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
            check_rows.append(row)

        # ── Counter text ────────────────────────────────────────
        total = len(self.checkboxes)
        self.counter_text = ft.Text(
            f"{total}/{total} steps selected",
            size=12, color=TEXT_MUTED,
        )

        # ── Select All / Deselect All buttons ───────────────────
        select_all_btn = ft.TextButton(
            "Select All",
            icon=ft.Icons.CHECK_BOX,
            style=ft.ButtonStyle(color=ACCENT),
            on_click=self._select_all,
        )
        deselect_all_btn = ft.TextButton(
            "Deselect All",
            icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
            style=ft.ButtonStyle(color=TEXT_MUTED),
            on_click=self._deselect_all,
        )

        # ── Node info summary ───────────────────────────────────
        node_info_parts = [
            ft.Icon(ft.Icons.CELL_TOWER, size=16, color=INFO),
            ft.Text(f"{self.lte_name}", size=13, color=TEXT,
                    weight=ft.FontWeight.W_500),
        ]
        if self.lte_ip:
            node_info_parts.append(
                ft.Text(f"({self.lte_ip})", size=12, color=TEXT_MUTED))
        if self.has_gsm:
            node_info_parts.extend([
                ft.Container(width=16),
                ft.Icon(ft.Icons.SETTINGS_INPUT_ANTENNA, size=16,
                        color=ACCENT_WARM),
                ft.Text(f"{self.gsm_name}", size=13, color=TEXT,
                        weight=ft.FontWeight.W_500),
            ])
            if self.gsm_ip:
                node_info_parts.append(
                    ft.Text(f"({self.gsm_ip})", size=12, color=TEXT_MUTED))

        node_info_row = ft.Row(node_info_parts, spacing=6,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # ── Run button ──────────────────────────────────────────
        self.run_button = ft.ElevatedButton(
            "Run Integration",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            style=ft.ButtonStyle(
                bgcolor=ACCENT, color="#06242A",
                padding=ft.Padding.symmetric(horizontal=28, vertical=16),
                shape=ft.RoundedRectangleBorder(radius=14),
                text_style=ft.TextStyle(
                    size=16, weight=ft.FontWeight.BOLD),
            ),
            on_click=self._on_run,
        )
        back_button = ft.ElevatedButton(
            "Back",
            icon=ft.Icons.ARROW_BACK,
            style=secondary_button_style(),
            on_click=self._go_back,
        )

        # ── Layout ──────────────────────────────────────────────
        checklist_panel = panel(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Select Steps to Run", size=18,
                                    weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Container(expand=True),
                            self.counter_text,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [select_all_btn, deselect_all_btn],
                        spacing=4,
                    ),
                    ft.Divider(height=1, color=BORDER),
                    *check_rows,
                ],
                spacing=6,
                scroll=ft.ScrollMode.AUTO,
            ),
            bgcolor=PANEL,
            padding=24,
        )

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=28, vertical=20),
            content=ft.Column(
                [
                    ft.Text("Integration Setup", size=24,
                            weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(f"Shortcode: {self.shortcode}", size=13,
                            color=TEXT_MUTED),
                    node_info_row,
                    ft.Container(height=8),
                    ft.Container(
                        content=checklist_panel,
                        expand=True,
                    ),
                    ft.Container(height=8),
                    ft.Row(
                        [back_button, ft.Container(expand=True), self.run_button],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=8,
                expand=True,
            ),
        )

        return ft.View(
            route="/integration", padding=0, spacing=0,
            bgcolor=BG_TOP, controls=[body],
        )

    # ── Checkbox helpers ────────────────────────────────────────
    def _update_counter(self):
        selected = sum(1 for cb in self.checkboxes.values() if cb.value)
        total = len(self.checkboxes)
        self.counter_text.value = f"{selected}/{total} steps selected"
        self.run_button.disabled = (selected == 0)
        try:
            self.page.update()
        except Exception:
            pass

    def _on_checkbox_change(self, e):
        self._update_counter()

    def _toggle_checkbox(self, key: str):
        cb = self.checkboxes.get(key)
        if cb:
            cb.value = not cb.value
            self._update_counter()

    def _select_all(self, e):
        for cb in self.checkboxes.values():
            cb.value = True
        self._update_counter()

    def _deselect_all(self, e):
        for cb in self.checkboxes.values():
            cb.value = False
        self._update_counter()

    # ── Navigation ──────────────────────────────────────────────
    def _on_run(self, e):
        selected = {k for k, cb in self.checkboxes.items() if cb.value}
        if not selected:
            return
        # Store selected steps on the page for the progress page
        self.page.integration_selected_steps = selected
        self.page.go("/integration_run")

    def _go_back(self, e):
        self.page.go("/form")


# ═════════════════════════════════════════════════════════════════
#  Phase 2 — Progress Page (runs the selected steps)
# ═════════════════════════════════════════════════════════════════
class IntegrationRunPage:
    """Two-column progress view with high-level log output."""

    def __init__(self, page: ft.Page):
        self.page = page
        self.cancelled = False
        self._log_queue: queue.Queue = queue.Queue()
        self._run_finished = False

        self.form = getattr(page, "integration_form", {})
        self.selected_steps: set = getattr(
            page, "integration_selected_steps", set())

        # Node identifiers
        self.lte_name = self.form.get("node_name", "LTE/NR Node")
        self.lte_ip = self.form.get("node_ip", "")
        self.lte_subnet = self.form.get("subnetwork", "")
        self.gsm_name = self.form.get("gsm_node_name", "")
        self.gsm_ip = self.form.get("gsm_node_ip", "")
        self.gsm_subnet = self.form.get("gsm_subnetwork", "")
        self.has_gsm = bool(self.gsm_name)

        self.shortcode = self.form.get("shortcode", "UNKNOWN")

        from app_path import get_app_dir
        self.log_dir = os.path.join(get_app_dir(), "LOG", self.shortcode)
        os.makedirs(self.log_dir, exist_ok=True)

        # Per-node session log buffers — full detail for file saving
        # Keyed by node_tag ("lte" or "gsm") to avoid cross-contamination
        self._session_logs: dict[str, list[str]] = {"lte": [], "gsm": []}
        self._session_log_lock = threading.Lock()

    # ── Build ────────────────────────────────────────────────────
    def build(self) -> ft.View:
        self.back_button = ft.ElevatedButton(
            "Back to Form",
            icon=ft.Icons.ARROW_BACK,
            style=secondary_button_style(),
            on_click=self._go_back,
            visible=False,
        )
        self.cancel_button = ft.ElevatedButton(
            "Cancel",
            icon=ft.Icons.CANCEL_OUTLINED,
            style=ft.ButtonStyle(
                bgcolor=DANGER, color=TEXT,
                padding=ft.Padding.symmetric(horizontal=18, vertical=14),
                shape=ft.RoundedRectangleBorder(radius=14),
            ),
            on_click=self._on_cancel,
        )
        self.status_text = ft.Text(
            "Integration in progress...",
            size=22, weight=ft.FontWeight.BOLD, color=TEXT,
        )
        self.elapsed_text = ft.Text("00:00", size=13, color=TEXT_MUTED)
        self.log_column = ft.Column(
            spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
        )

        # Build step rows for each node column
        self.lte_steps: dict[str, _StepRow] = {}
        self.gsm_steps: dict[str, _StepRow] = {}

        lte_rows = []
        gsm_rows = []
        for key, label, applies_to, _ in INTEGRATION_STEPS:
            if applies_to in ("both", "lte_nr"):
                row = _StepRow(label)
                self.lte_steps[key] = row
                lte_rows.append(row.control)
                # Mark deselected steps immediately
                if key not in self.selected_steps:
                    row.set_state("skip", "Not selected")
            if applies_to in ("both", "gsm"):
                row = _StepRow(label)
                self.gsm_steps[key] = row
                gsm_rows.append(row.control)
                if key not in self.selected_steps:
                    row.set_state("skip", "Not selected")

        lte_column = self._node_card(
            title=self.lte_name or "LTE/NR Node",
            subtitle=self._node_subtitle(self.lte_ip, self.lte_subnet),
            accent=INFO,
            icon=ft.Icons.CELL_TOWER,
            step_rows=lte_rows,
        )
        gsm_column = self._node_card(
            title=self.gsm_name or "GSM Node",
            subtitle=self._node_subtitle(self.gsm_ip, self.gsm_subnet),
            accent=ACCENT_WARM,
            icon=ft.Icons.SETTINGS_INPUT_ANTENNA,
            step_rows=gsm_rows,
            dimmed=not self.has_gsm,
        )

        header = ft.Row(
            [
                ft.Column(
                    [self.status_text, self.elapsed_text],
                    spacing=4, expand=True,
                ),
                self.cancel_button,
                self.back_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        columns_row = ft.Row(
            [lte_column, gsm_column],
            spacing=16,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.START,
            expand=True,
        )

        log_panel = panel(
            ft.Column(
                [
                    ft.Text("LOG OUTPUT", size=11, color=TEXT_MUTED,
                            weight=ft.FontWeight.BOLD),
                    self.log_column,
                ],
                spacing=8,
                expand=True,
            ),
            bgcolor=PANEL_RAISED,
            padding=14,
            expand=True,
        )

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=28, vertical=20),
            content=ft.Column(
                [header, columns_row, log_panel],
                spacing=16,
                expand=True,
            ),
        )

        # Start timer and workflow
        self._start_time = datetime.now()
        self._timer_running = True
        threading.Thread(target=self._tick_timer, daemon=True).start()
        threading.Thread(target=self._run_workflow, daemon=True).start()
        self.page.run_task(self._flush_loop)

        return ft.View(
            route="/integration_run", padding=0, spacing=0,
            bgcolor=BG_TOP, controls=[body],
        )

    # ── UI helpers ───────────────────────────────────────────────
    def _node_subtitle(self, ip: str, subnet: str) -> str:
        parts = []
        if ip:
            parts.append(f"IP: {ip}")
        if subnet:
            parts.append(f"Subnet: {subnet}")
        return "  |  ".join(parts) if parts else "No IP / Subnetwork configured"

    def _node_card(
        self, title: str, subtitle: str, accent: str, icon: str,
        step_rows: list[ft.Control], dimmed: bool = False,
    ) -> ft.Container:
        opacity = 0.4 if dimmed else 1.0
        return ft.Container(
            expand=1,
            opacity=opacity,
            content=panel(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(icon, size=20, color=accent),
                                ft.Text(title, size=16,
                                        weight=ft.FontWeight.BOLD, color=TEXT),
                            ],
                            spacing=8,
                        ),
                        ft.Text(subtitle, size=11, color=TEXT_MUTED),
                        ft.Divider(height=1, color=BORDER),
                        *step_rows,
                    ],
                    spacing=10,
                ),
                bgcolor=PANEL,
                padding=20,
            ),
        )

    # ── Logging ──────────────────────────────────────────────────
    def _ui_log(self, msg: str):
        """High-level log shown in the UI panel only (not saved to file)."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_queue.put_nowait(line)

    def _detail_log(self, msg: str, node_tag: str = "lte"):
        """Full-detail log saved to step log files only (not shown in UI)."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._session_log_lock:
            self._session_logs[node_tag].append(line)

    def _save_step_log(self, step_number: int, node_name: str,
                       log_suffix: str, node_tag: str = "lte"):
        """Save per-node session log snapshot to LOG/<SHORTCODE>/NN_NODE_SUFFIX.txt."""
        filename = f"{step_number:02d}_{node_name}_{log_suffix}.txt"
        filepath = os.path.join(self.log_dir, filename)
        with self._session_log_lock:
            snapshot = list(self._session_logs.get(node_tag, []))
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Integration Log — {self.shortcode}\n")
                f.write(f"Node: {node_name} | Step: {log_suffix}\n")
                f.write(f"Saved at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 72 + "\n\n")
                f.write("\n".join(snapshot))
                f.write("\n")
            logger.info(f"[Integration] Log saved: {filepath}")
        except Exception as exc:
            logger.warning(f"[Integration] Failed to save log: {exc}")

    async def _flush_loop(self):
        while not self._run_finished or not self._log_queue.empty():
            self._flush_log()
            await asyncio.sleep(0.25)
        self._flush_log()

    def _flush_log(self):
        dirty = False
        while not self._log_queue.empty():
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_column.controls.append(
                ft.Text(msg, size=11, color=TEXT_MUTED, selectable=True,
                        font_family="Consolas")
            )
            if len(self.log_column.controls) > 500:
                self.log_column.controls = self.log_column.controls[-400:]
            dirty = True
        if dirty:
            try:
                self.log_column.scroll_to(offset=-1)
                self.page.update()
            except Exception:
                pass

    def _tick_timer(self):
        import time
        while self._timer_running:
            elapsed = datetime.now() - self._start_time
            mins, secs = divmod(int(elapsed.total_seconds()), 60)
            self.elapsed_text.value = f"{mins:02d}:{secs:02d}"
            try:
                self.page.update()
            except Exception:
                pass
            time.sleep(1)

    # ── Step state updates ───────────────────────────────────────
    def _set_step(self, node: str, key: str, state: str, detail: str = ""):
        steps = self.lte_steps if node == "lte" else self.gsm_steps
        if key in steps:
            steps[key].set_state(state, detail)

    # ── Blocking dialogs (called from worker threads) ────────────
    def _ask_user_retry(self, message: str) -> bool:
        result_event = threading.Event()
        user_choice: list[bool] = [False]

        def _on_retry(e):
            user_choice[0] = True
            dlg.open = False
            self.page.update()
            result_event.set()

        def _on_stop(e):
            user_choice[0] = False
            dlg.open = False
            self.page.update()
            result_event.set()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Verification Failed", color=DANGER,
                          weight=ft.FontWeight.BOLD),
            content=ft.Column(
                [
                    ft.Text(message, size=13, color=TEXT),
                    ft.Container(height=8),
                    ft.Text(
                        "Fix the issue manually (e.g. via ENM CLI), "
                        "then click Retry.",
                        size=12, color=TEXT_MUTED, italic=True,
                    ),
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton("Stop",
                              style=ft.ButtonStyle(color=DANGER),
                              on_click=_on_stop),
                ft.ElevatedButton(
                    "Retry", icon=ft.Icons.REFRESH,
                    style=ft.ButtonStyle(
                        bgcolor=ACCENT, color="#06242A",
                        padding=ft.Padding.symmetric(horizontal=16,
                                                     vertical=12),
                        shape=ft.RoundedRectangleBorder(radius=12)),
                    on_click=_on_retry),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=PANEL,
        )

        self.page.overlay.append(dlg)
        dlg.open = True
        try:
            self.page.update()
        except Exception:
            pass

        result_event.wait()
        if dlg in self.page.overlay:
            self.page.overlay.remove(dlg)
        return user_choice[0]

    def _ask_user_confirm(self, title: str, message: str) -> bool:
        result_event = threading.Event()
        user_choice: list[bool] = [False]

        def _on_ok(e):
            user_choice[0] = True
            dlg.open = False
            self.page.update()
            result_event.set()

        def _on_cancel(e):
            user_choice[0] = False
            dlg.open = False
            self.page.update()
            result_event.set()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title, color=INFO, weight=ft.FontWeight.BOLD),
            content=ft.Text(message, size=13, color=TEXT, selectable=True),
            actions=[
                ft.TextButton("Cancel",
                              style=ft.ButtonStyle(color=DANGER),
                              on_click=_on_cancel),
                ft.ElevatedButton(
                    "OK, Run It", icon=ft.Icons.CHECK,
                    style=ft.ButtonStyle(
                        bgcolor=ACCENT, color="#06242A",
                        padding=ft.Padding.symmetric(horizontal=16,
                                                     vertical=12),
                        shape=ft.RoundedRectangleBorder(radius=12)),
                    on_click=_on_ok),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=PANEL,
        )

        self.page.overlay.append(dlg)
        dlg.open = True
        try:
            self.page.update()
        except Exception:
            pass

        result_event.wait()
        if dlg in self.page.overlay:
            self.page.overlay.remove(dlg)
        return user_choice[0]

    # ── Workflow ───────────────────────────────────────────────────
    def _run_workflow(self):
        from concurrent.futures import ThreadPoolExecutor

        self._ui_log("Starting integration workflow...")
        self._ui_log(f"Shortcode: {self.shortcode}")
        self._ui_log(f"Log directory: {self.log_dir}")
        self._ui_log(
            f"LTE/NR: {self.lte_name}  IP: {self.lte_ip}  "
            f"Subnet: {self.lte_subnet}")

        selected_names = []
        for key, label, _, _ in INTEGRATION_STEPS:
            if key in self.selected_steps:
                selected_names.append(label)
        self._ui_log(f"Selected steps: {', '.join(selected_names)}")

        if self.has_gsm:
            self._ui_log(
                f"GSM: {self.gsm_name}  IP: {self.gsm_ip}  "
                f"Subnet: {self.gsm_subnet}")
        else:
            for key, label, applies_to, _ in INTEGRATION_STEPS:
                if applies_to in ("both", "gsm"):
                    self._set_step("gsm", key, "skip", "No GSM node")
        self._ui_log("")

        futures = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures["lte"] = pool.submit(
                self._run_node_steps,
                node_tag="lte",
                node_name=self.lte_name,
                node_ip=self.lte_ip,
                subnetwork=self.lte_subnet,
                node_type="lte_nr",
            )
            if self.has_gsm:
                futures["gsm"] = pool.submit(
                    self._run_node_steps,
                    node_tag="gsm",
                    node_name=self.gsm_name,
                    node_ip=self.gsm_ip,
                    subnetwork=self.gsm_subnet,
                    node_type="gsm",
                )

            for fkey, future in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    lbl = "LTE/NR" if fkey == "lte" else "GSM"
                    self._ui_log(f"[{lbl}] Node workflow failed: {exc}")
                    logger.exception(f"Integration {lbl} failed")

        self._timer_running = False
        self._run_finished = True
        self.status_text.value = "Integration complete"
        self.cancel_button.visible = False
        self.back_button.visible = True
        self._ui_log("")
        self._ui_log(f"All steps finished. Logs saved to: {self.log_dir}")
        try:
            self.page.update()
        except Exception:
            pass

    def _run_node_steps(
        self,
        node_tag: str,
        node_name: str,
        node_ip: str,
        subnetwork: str,
        node_type: str,
    ):
        """Run selected integration steps for one node."""
        import time
        from integration_runner import (
            IntegrationSSH, run_create_arne, run_enrollment,
            run_install_lkf, run_baseline, run_relation, run_verify_mme,
            run_take_dump, run_gsm_cell_define, run_take_cm_dump,
            run_uri_setting, run_pm_measurement,
        )

        label = "LTE/NR" if node_tag == "lte" else "GSM"
        host = self.form.get("host", "")
        port = self.form.get("port", 5023)
        username = self.form.get("username", "")
        password = self.form.get("password", "")

        def detail_cb(msg: str):
            """Full detail — saved to log file only."""
            self._detail_log(f"[{label}] {msg}", node_tag=node_tag)

        def ui_cb(msg: str):
            """High-level — shown in UI log panel."""
            self._ui_log(f"[{label}] {msg}")

        # Connect SSH
        try:
            ssh = IntegrationSSH(
                host=host, port=port,
                username=username, password=password,
                log_callback=detail_cb,
            )
            ssh.connect(timeout=30)
            ui_cb("SSH connected.")
        except Exception as exc:
            ui_cb(f"SSH connection failed: {exc}")
            for key, _, applies_to, _ in INTEGRATION_STEPS:
                if applies_to in ("both", node_type) and \
                        key in self.selected_steps:
                    self._set_step(node_tag, key, "error", "SSH failed")
            return

        # Steps that need an active AMOS session
        amos_steps = {
            "enrollment", "install_lkf", "baseline", "ret_scripts",
            "relation", "uri_setting", "verify_mme", "take_dump",
            "take_cm_dump", "gsm_cell_define", "pm_measurement",
        }
        in_amos = False

        try:
            step_num = 1
            for key, step_label, applies_to, log_suffix in INTEGRATION_STEPS:
                if self.cancelled:
                    break
                if applies_to not in ("both", node_type):
                    continue

                # Skip if not selected
                if key not in self.selected_steps:
                    continue

                # Enter AMOS lazily before the first step that needs it
                if not in_amos and key in amos_steps:
                    ui_cb(f"Entering AMOS for {node_name}...")
                    try:
                        ssh.enter_amos(node_name, timeout=90)
                        in_amos = True
                        ui_cb("AMOS session ready.")
                    except Exception as exc:
                        ui_cb(f"Failed to enter AMOS: {exc}")
                        # Mark remaining selected steps as error
                        remaining = False
                        for rk, _, ra, _ in INTEGRATION_STEPS:
                            if ra not in ("both", node_type):
                                continue
                            if rk not in self.selected_steps:
                                continue
                            if rk == key:
                                remaining = True
                            if remaining:
                                self._set_step(node_tag, rk, "error",
                                               "AMOS failed")
                        break

                self._set_step(node_tag, key, "running")
                ui_cb(f"Running {step_label}...")

                stopped = False
                try:
                    if key == "create_arne":
                        success, output = run_create_arne(
                            ssh, node_name, node_ip, subnetwork, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done", "Verified")
                            ui_cb(f"{step_label} — verified.")
                        else:
                            self._set_step(node_tag, key, "error",
                                           "Stopped by user")
                            ui_cb(f"{step_label} — stopped by user.")
                            stopped = True

                    elif key == "enrollment":
                        success, output = run_enrollment(
                            ssh, node_name, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "SYNCHRONIZED")
                            ui_cb(f"{step_label} — SYNCHRONIZED.")
                        else:
                            self._set_step(node_tag, key, "error",
                                           "Stopped by user")
                            ui_cb(f"{step_label} — stopped by user.")
                            stopped = True

                    elif key == "install_lkf":
                        lkf_file = self.form.get("lkf_file", "")
                        if not lkf_file:
                            self._set_step(node_tag, key, "skip",
                                           "No LKF file")
                            ui_cb(f"{step_label} — skipped (no file).")
                        else:
                            success, output = run_install_lkf(
                                ssh, node_name, lkf_file, detail_cb,
                                wait_for_user=self._ask_user_retry,
                            )
                            if success:
                                self._set_step(node_tag, key, "done",
                                               "COMPLETED")
                                ui_cb(f"{step_label} — completed.")
                            else:
                                self._set_step(node_tag, key, "error",
                                               "Failed")
                                ui_cb(f"{step_label} — failed.")
                                stopped = True

                    elif key == "baseline":
                        success, output = run_baseline(
                            ssh, node_name, detail_cb,
                            wait_for_user=self._ask_user_retry,
                            confirm_baseline=self._ask_user_confirm,
                        )
                        if success:
                            self._set_step(node_tag, key, "done", "Verified")
                            ui_cb(f"{step_label} — verified.")
                        else:
                            self._set_step(node_tag, key, "error", "Failed")
                            ui_cb(f"{step_label} — failed.")
                            stopped = True

                    elif key == "relation":
                        relation_file = self.form.get("relation_file", "")
                        if not relation_file:
                            self._set_step(node_tag, key, "skip",
                                           "No relation file")
                            ui_cb(f"{step_label} — skipped (no file).")
                        else:
                            success, output = run_relation(
                                ssh, node_name, self.shortcode,
                                relation_file, self.log_dir, detail_cb,
                                wait_for_user=self._ask_user_retry,
                            )
                            if success:
                                self._set_step(node_tag, key, "done",
                                               "Completed")
                                ui_cb(f"{step_label} — completed.")
                            else:
                                self._set_step(node_tag, key, "error",
                                               "Failed")
                                ui_cb(f"{step_label} — failed.")
                                stopped = True

                    elif key == "ret_scripts":
                        self._set_step(node_tag, key, "skip", "Coming soon")
                        ui_cb(f"{step_label} — skipped (coming soon).")

                    elif key == "uri_setting":
                        success, output = run_uri_setting(
                            ssh, node_name, username, password,
                            detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "SUCCESS")
                            ui_cb(f"{step_label} — SUCCESS.")
                        else:
                            self._set_step(node_tag, key, "error",
                                           "Failed")
                            ui_cb(f"{step_label} — failed.")
                            stopped = True

                    elif key == "verify_mme":
                        success, output = run_verify_mme(
                            ssh, node_name, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "All ENABLED")
                            ui_cb(f"{step_label} — all ENABLED.")
                        else:
                            self._set_step(node_tag, key, "error",
                                           "DISABLED found")
                            ui_cb(f"{step_label} — DISABLED found.")
                            stopped = True

                    elif key == "gsm_cell_define":
                        success, output = run_gsm_cell_define(
                            ssh, node_name, self.shortcode, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "Cell & MO OK")
                            ui_cb(f"{step_label} — Cell & MO OK.")
                        else:
                            self._set_step(node_tag, key, "error",
                                           "Not defined")
                            ui_cb(f"{step_label} — not defined.")
                            stopped = True

                    elif key == "take_dump":
                        success, output = run_take_dump(
                            ssh, node_name, self.shortcode,
                            self.log_dir, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "Downloaded")
                            ui_cb(f"{step_label} — downloaded.")
                        else:
                            self._set_step(node_tag, key, "error", "Failed")
                            ui_cb(f"{step_label} — failed.")
                            stopped = True

                    elif key == "take_cm_dump":
                        success, output = run_take_cm_dump(
                            ssh, node_name, self.shortcode,
                            self.log_dir, detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done",
                                           "Downloaded")
                            ui_cb(f"{step_label} — downloaded.")
                        else:
                            self._set_step(node_tag, key, "error", "Failed")
                            ui_cb(f"{step_label} — failed.")
                            stopped = True

                    elif key == "pm_measurement":
                        pm_type = "gsm" if node_tag == "gsm" else "lte_nr"
                        success, output = run_pm_measurement(
                            ssh, node_name, pm_type,
                            detail_cb,
                            wait_for_user=self._ask_user_retry,
                        )
                        if success:
                            self._set_step(node_tag, key, "done", "Active")
                            ui_cb(f"{step_label} — PM active.")
                        else:
                            self._set_step(node_tag, key, "error", "Failed")
                            ui_cb(f"{step_label} — failed.")
                            stopped = True

                    else:
                        time.sleep(0.3)
                        self._set_step(node_tag, key, "skip",
                                       "Not implemented yet")
                        ui_cb(f"{step_label} — not yet implemented.")

                except Exception as exc:
                    self._set_step(node_tag, key, "error",
                                   str(exc)[:40])
                    ui_cb(f"{step_label} — FAILED: {exc}")
                    stopped = True

                self._save_step_log(step_num, node_name, log_suffix,
                                    node_tag=node_tag)
                step_num += 1

                if stopped:
                    ui_cb("Stopping remaining steps due to failure.")
                    remaining = False
                    for rkey, rlabel, rapplies, _ in INTEGRATION_STEPS:
                        if rapplies not in ("both", node_type):
                            continue
                        if rkey not in self.selected_steps:
                            continue
                        if remaining:
                            self._set_step(
                                node_tag, rkey, "skip",
                                "Skipped (previous step failed)")
                        if rkey == key:
                            remaining = True
                    break
        finally:
            if in_amos:
                try:
                    ssh.exit_amos()
                except Exception:
                    pass
            ssh.disconnect()
            ui_cb("SSH disconnected.")

    # ── Navigation ───────────────────────────────────────────────
    def _on_cancel(self, e):
        self.cancelled = True
        self._timer_running = False
        self._run_finished = True
        self.status_text.value = "Cancelled"
        self.cancel_button.visible = False
        self.back_button.visible = True
        self.page.update()

    def _go_back(self, e):
        self.cancelled = True
        self._timer_running = False
        self._run_finished = True
        self.page.go("/form")


# ── Step row widget ──────────────────────────────────────────────
class _StepRow:
    """Single checklist row: icon + label + status detail."""

    _ICONS = {
        "pending":  (ft.Icons.RADIO_BUTTON_UNCHECKED, TEXT_MUTED),
        "running":  (ft.Icons.HOURGLASS_TOP,          ACCENT),
        "done":     (ft.Icons.CHECK_CIRCLE,            SUCCESS),
        "error":    (ft.Icons.ERROR,                   DANGER),
        "skip":     (ft.Icons.REMOVE_CIRCLE_OUTLINE,   TEXT_MUTED),
    }

    def __init__(self, label: str):
        self._icon = ft.Icon(ft.Icons.RADIO_BUTTON_UNCHECKED, size=18,
                             color=TEXT_MUTED)
        self._label = ft.Text(label, size=13, color=TEXT,
                              weight=ft.FontWeight.W_500)
        self._detail = ft.Text("", size=11, color=TEXT_MUTED)
        self._spinner = ft.ProgressRing(
            width=16, height=16, stroke_width=2,
            color=ACCENT, visible=False)
        self.control = ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            border_radius=10,
            content=ft.Row(
                [
                    self._icon,
                    self._spinner,
                    self._label,
                    ft.Container(expand=True),
                    self._detail,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def set_state(self, state: str, detail: str = ""):
        icon_name, color = self._ICONS.get(state, self._ICONS["pending"])
        self._icon.name = icon_name
        self._icon.color = color
        self._label.color = color if state == "error" else TEXT
        self._detail.value = detail
        self._detail.color = SUCCESS if state == "done" else (
            DANGER if state == "error" else TEXT_MUTED
        )
        self._spinner.visible = (state == "running")
        self._icon.visible = (state != "running")
        self.control.bgcolor = (
            ft.Colors.with_opacity(0.08, ACCENT)
            if state == "running" else None
        )
