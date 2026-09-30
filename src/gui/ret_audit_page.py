"""
ret_audit_page.py — RET before/after audit for a Nokia → Ericsson swap.

Before = the Nokia site's IM snapshot (.ims2, one file per site).
After  = Ericsson moshell log(s) of ``lhgetc AntennaUnitGroup=.*,AntennaNearUnit=``
         (one log may hold several nodes).

Each RET subunit is paired on controller serial + subunit number and graded
Match / Improved / Mismatch / Missing (see audit/ret_audit.py). The result is
shown on the page and exported to LOG/<SiteID>/AUDIT/<site>_RET_audit_<ts>.xlsx.
"""
import asyncio
import os
import re
import threading
import time
from datetime import datetime

import flet as ft

from gui.theme import (
    ACCENT, ACCENT_WARM, BG_BOTTOM, BG_TOP, BORDER, DANGER, PANEL,
    SUCCESS, TEXT, TEXT_MUTED, background_gradient, panel,
    primary_button_style, secondary_button_style,
)

_STATUS_COLOR = {
    "OK": SUCCESS,
    "Not OK": DANGER,
}


class RetAuditPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.file_picker = ft.FilePicker()
        self._form = getattr(page, "integration_form", {}) or {}
        self._result_path = None
        self._running = False
        try:
            from session import load_page_state
            self._state = load_page_state("ret_audit")
        except Exception:
            self._state = {}

    # ── UI ───────────────────────────────────────────────────────
    def build(self) -> ft.View:
        from audit.ret_audit import ERICSSON_COMMAND
        try:
            self.page.title = "NodeCraft — RET Audit"
            self.page.update()
        except Exception:
            pass

        st = self._state
        self.site_field = self._tf(
            "Site ID", st.get("site") or self._form.get("shortcode", ""))
        self.site_field.width = 260
        self.ims2_field = self._tf(
            "Nokia IM snapshot (.ims2) — before", st.get("ims2", ""), expand=True)
        self.log_field = self._tf(
            "Ericsson log(s) — after; several files allowed, separated by |",
            st.get("logs", ""), expand=True)

        self.status_text = ft.Text("", size=13, color=TEXT_MUTED)
        self.summary_row = ft.Row([], spacing=10, wrap=True)
        self.site_banner = ft.Container(visible=False, border_radius=12,
                                        padding=ft.Padding.symmetric(
                                            horizontal=14, vertical=10))
        self.table_col = ft.Column([], spacing=0)
        self.log_col = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO,
                                 height=160, auto_scroll=True)
        self.run_btn = ft.ElevatedButton(
            "Run RET Audit", icon=ft.Icons.SETTINGS_INPUT_ANTENNA,
            style=primary_button_style(), on_click=self._run)
        self.table_panel = panel(self.table_col, bgcolor=PANEL, padding=12)
        self.table_panel.visible = False
        self.clear_btn = ft.OutlinedButton(
            "Clear Data", icon=ft.Icons.DELETE_SWEEP,
            tooltip="Clear the inputs and results on this page "
                    "(report files on disk are kept)",
            style=secondary_button_style(), on_click=self._clear_data)
        self.open_btn = ft.ElevatedButton(
            "Open Report", icon=ft.Icons.OPEN_IN_NEW, visible=False,
            style=secondary_button_style(), on_click=self._open_result)

        def browse_row(field, handler, label):
            return ft.Row([
                field,
                ft.ElevatedButton(label, icon=ft.Icons.FOLDER_OPEN,
                                  style=secondary_button_style(),
                                  on_click=handler),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        command_box = ft.Container(
            content=ft.Row([
                ft.Text("Ericsson command", size=11, color=TEXT_MUTED,
                        weight=ft.FontWeight.BOLD),
                ft.Text(ERICSSON_COMMAND, size=13, color=ACCENT,
                        font_family="Consolas", selectable=True),
                ft.IconButton(icon=ft.Icons.CONTENT_COPY, icon_size=16,
                              icon_color=TEXT_MUTED,
                              tooltip="Copy the command",
                              on_click=self._copy_command),
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border=ft.Border.all(1, BORDER), border_radius=12,
            padding=ft.Padding.symmetric(horizontal=14, vertical=6),
        )

        body = ft.Container(
            expand=True, gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=24, vertical=16),
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.SETTINGS_INPUT_ANTENNA, size=26,
                            color=ACCENT),
                    ft.Text("RET Audit", size=22, weight=ft.FontWeight.BOLD,
                            color=TEXT),
                    ft.Container(expand=True),
                    ft.TextButton("← Back", on_click=self._go_back),
                ]),
                ft.Text("Before = Nokia IM snapshot (.ims2). After = Ericsson "
                        "log of the command below, taken on every node of the "
                        "site. OK = the RET is ENABLED on Ericsson in the same "
                        "sector as on Nokia; anything else is Not OK. The "
                        "remark says why (Ericsson state, mismatching fields, "
                        "tilt as a note).",
                        size=12, color=TEXT_MUTED),
                panel(ft.Column([
                    ft.Row([self.site_field], spacing=10),
                    browse_row(self.ims2_field, self._browse_ims2,
                               "Nokia .ims2"),
                    browse_row(self.log_field, self._browse_logs,
                               "Ericsson log(s)"),
                    command_box,
                    ft.Row([self.run_btn, self.clear_btn, self.open_btn,
                            self.status_text],
                           spacing=14, wrap=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=12), bgcolor=PANEL, padding=18),
                self.site_banner,
                self.summary_row,
                self.table_panel,
                panel(ft.Column([
                    ft.Text("LOG", size=11, color=TEXT_MUTED,
                            weight=ft.FontWeight.BOLD),
                    self.log_col,
                ], spacing=6), bgcolor=PANEL, padding=12),
            ], spacing=12, scroll=ft.ScrollMode.AUTO,
               horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        )
        return ft.View(route="/ret_audit", padding=0, spacing=0,
                       bgcolor=BG_TOP, controls=[body],
                       services=[self.file_picker])

    def _tf(self, label, value, expand=None):
        return ft.TextField(
            label=label, value=value, expand=expand, filled=True,
            border_radius=14, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED),
            text_style=ft.TextStyle(color=TEXT, size=13))

    # ── state / navigation ───────────────────────────────────────
    def _save_state(self):
        try:
            from session import save_page_state
            save_page_state("ret_audit", {
                "site": self.site_field.value,
                "ims2": self.ims2_field.value,
                "logs": self.log_field.value,
            })
        except Exception:
            pass

    def _go_back(self, e):
        self._save_state()
        self.page.go("/form")

    async def _browse_ims2(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select the Nokia IM snapshot",
            allowed_extensions=["ims2"],
            file_type=ft.FilePickerFileType.CUSTOM, allow_multiple=False)
        if files:
            self.ims2_field.value = files[0].path
            if not self.site_field.value.strip():
                from audit.ret_audit import mrbts_from_filename
                self.site_field.value = mrbts_from_filename(files[0].path)
            self._save_state()
            self.page.update()

    async def _browse_logs(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select the Ericsson lhgetc log(s)",
            allowed_extensions=["log", "txt"],
            file_type=ft.FilePickerFileType.CUSTOM, allow_multiple=True)
        if files:
            self.log_field.value = " | ".join(f.path for f in files)
            self._save_state()
            self.page.update()

    def _copy_command(self, e):
        from audit.ret_audit import ERICSSON_COMMAND

        async def _copy():
            try:
                await self.page.clipboard.set(ERICSSON_COMMAND)
                self._set_status("Command copied.", TEXT_MUTED)
            except Exception as exc:
                self._set_status(f"Copy failed: {exc}", DANGER)
        try:
            self.page.run_task(_copy)
        except Exception as exc:
            self._set_status(f"Copy failed: {exc}", DANGER)

    def _clear_data(self, e):
        """Reset the page's inputs and results. Files on disk (the .ims2,
        the Ericsson logs, earlier reports) are not touched."""
        if self._running:
            self._set_status("RET Audit is running; wait until it finishes "
                             "before clearing.", ACCENT_WARM)
            return
        for field in (self.site_field, self.ims2_field, self.log_field):
            field.value = ""
        self._result_path = None
        self.open_btn.visible = False
        self.site_banner.visible = False
        self.summary_row.controls.clear()
        self.table_col.controls.clear()
        self.table_panel.visible = False
        self.log_col.controls.clear()
        self._save_state()
        self._set_status("Cleared.", TEXT_MUTED)

    def _open_result(self, e):
        if self._result_path and os.path.isfile(self._result_path):
            try:
                os.startfile(self._result_path)   # Windows
            except Exception as exc:
                self._log(f"Could not open file: {exc}")

    # ── run ──────────────────────────────────────────────────────
    def _run(self, e):
        if self._running:
            return
        self._save_state()
        ims2 = self.ims2_field.value.strip().strip('"')
        logs = [p.strip().strip('"') for p in
                re.split(r"\s*\|\s*", self.log_field.value or "") if p.strip()]
        errors = []
        if not ims2 or not os.path.isfile(ims2):
            errors.append("Pick the Nokia .ims2 file.")
        missing = [p for p in logs if not os.path.isfile(p)]
        if not logs:
            errors.append("Pick at least one Ericsson log.")
        elif missing:
            errors.append("Log not found: " + ", ".join(missing))
        if errors:
            self._set_status(" ".join(errors), DANGER)
            return
        site = (self.site_field.value.strip()
                or os.path.splitext(os.path.basename(ims2))[0])
        self._running = True
        self.run_btn.disabled = True
        self.open_btn.visible = False
        self.summary_row.controls.clear()
        self.site_banner.visible = False
        self.table_col.controls.clear()
        self.table_panel.visible = False
        self._t0 = time.time()
        self._set_status("Running…", ACCENT)
        threading.Thread(target=self._worker, args=(site, ims2, logs),
                         daemon=True).start()
        try:
            self.page.run_task(self._ui_loop)
        except Exception:
            pass

    async def _ui_loop(self):
        """Repaint from the UI side while the worker runs — Flet 0.84 must
        not be updated from the worker thread."""
        while self._running:
            if self.status_text.value.startswith("Running"):
                self.status_text.value = (
                    f"Running… {int(time.time() - self._t0)}s")
            self._refresh()
            await asyncio.sleep(0.4)
        self._refresh()

    def _worker(self, site, ims2, logs):
        try:
            from app_path import get_app_dir
            from audit import ret_audit
            from ims2_reader import Snapshot

            t0 = time.time()
            self._log(f"Reading Nokia snapshot {os.path.basename(ims2)} …")
            snap = Snapshot(ims2)
            nokia = ret_audit.nokia_rets(snap)
            alarms = ret_audit.nokia_alarms(snap)
            from audit import nokia_radio
            radio = nokia_radio.radio_rows(snap)
            flagged = [r for r in radio
                       if r["VSWR status"] in ("Warning", "Minor", "Major")]
            self._log(f"  Radio view: {len({r['Radio'] for r in radio})} radio(s), "
                      f"{len(radio)} port/band/cell row(s)"
                      + (f", ⚠ {len(flagged)} row(s) with VSWR ≥ 1.4" if flagged
                         else ", VSWR all below 1.4"))
            self._log(f"  {len(snap.raw)} MOs decoded in {time.time() - t0:.1f}s"
                      f" → {len(nokia)} RET subunit(s), {len(alarms[0])} "
                      f"active alarm(s), {len(alarms[1])} in history")
            wrong = [r for r in nokia
                     if r.sector_check not in ("", "OK")]
            for r in wrong:
                self._log(f"  ⚠ Nokia {r.base_station_id}: {r.sector_check}")
            ericsson = []
            for p in logs:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    found = ret_audit.ericsson_rets(fh.read())
                nodes = sorted({r.node for r in found if r.node})
                self._log(f"Ericsson {os.path.basename(p)}: {len(found)} "
                          f"RetSubUnit(s)"
                          + (f" on {', '.join(nodes)}" if nodes else ""))
                ericsson.extend(found)
            if not nokia:
                self._log("⚠ No RET found in the Nokia snapshot.")
            if not ericsson:
                self._log("⚠ No RetSubUnit found in the Ericsson log(s) — "
                          "was the lhgetc command run?")

            rows = ret_audit.compare(nokia, ericsson)
            check = ret_audit.site_check(ret_audit.nokia_bts_name(snap),
                                         [r.node for r in ericsson], rows)
            self._log(("✓ Same site: " if check["ok"]
                       else "⚠ SITE CHECK FAILED: ") + check["message"])
            self._show_site_check(check)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", site)
            out_dir = os.path.join(get_app_dir(), "LOG", safe, "AUDIT")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(out_dir, f"{safe}_RET_audit_{ts}.xlsx")
            ret_audit.write_excel(rows, nokia, ericsson, path,
                                  ret_audit.default_meta(ims2, logs, site,
                                                         check),
                                  alarms=alarms, radio=radio)
            self._result_path = path
            counts = ret_audit.summarize(rows)
            self._log(f"✓ Report: {path}")
            self._show_result(rows, counts)
            ns, es = ret_audit.sector_counts(nokia, ericsson)
            self._log(f"Sectors with RET — Nokia {len(ns)} ({' '.join(ns) or '-'})"
                      f", Ericsson {len(es)} ({' '.join(es) or '-'})"
                      + ("" if len(ns) == len(es) else "  ⚠ count changed"))
            bad = sum(counts[s] for s in ret_audit.ISSUES)
            self._set_status(
                f"Done — {len(rows)} RET(s): {counts['OK']} OK, "
                f"{counts['Not OK']} Not OK"
                + ("" if check["ok"] else " — ⚠ check the site names"),
                DANGER if bad or not check["ok"] else SUCCESS)
            self.open_btn.visible = True
            self.table_panel.visible = True
        except Exception as exc:
            self._log(f"✗ {type(exc).__name__}: {exc}")
            self._set_status(f"Failed: {exc}", DANGER)
        finally:
            self.run_btn.disabled = False
            self._running = False       # the UI loop does the final repaint

    # ── result table ─────────────────────────────────────────────
    def _show_result(self, rows, counts):
        from audit.ret_audit import display, ericsson_unique_id, fmt_tilt

        self.summary_row.controls = [
            self._chip(f"{k}  {v}", _STATUS_COLOR[k])
            for k, v in counts.items()
        ]
        cols = [("Status", 100), ("Sector N→E", 84), ("Base station ID", 210),
                ("Controller", 150), ("Sub", 36), ("Tilt N→E", 92),
                ("State N→E", 150), ("Unique ID N→E", 250), ("Remark", 0)]

        def cell(text, width, color=TEXT, bold=False):
            t = ft.Text(text, size=12, color=color, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, tooltip=text,
                        weight=ft.FontWeight.BOLD if bold else None,
                        font_family="Consolas")
            return (ft.Container(t, width=width) if width
                    else ft.Container(t, expand=True))

        header = ft.Container(
            ft.Row([cell(name, w, TEXT_MUTED, True) for name, w in cols],
                   spacing=10),
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)))
        lines = [header]
        for r in rows:
            a = r.any
            n, e = r.nokia, r.ericsson
            arrow = lambda x, y: f"{x or '—'} → {y or '—'}"
            tilt = arrow(fmt_tilt(n.tilt) if n else "",
                         fmt_tilt(e.tilt) if e else "")
            state = arrow(n.state if n else "", e.state if e else "")
            uid = arrow(display(n, "unique_id"),
                        ericsson_unique_id(n, e))
            diff_color = lambda label: DANGER if label in r.diffs else TEXT
            lines.append(ft.Container(
                ft.Row([
                    cell(r.status, 100, _STATUS_COLOR.get(r.status, TEXT), True),
                    cell(arrow(n.sector if n else "", e.sector if e else ""),
                         84, ACCENT_WARM if (
                             (n and e and n.sector and e.sector
                              and n.sector != e.sector)
                             or any(x and x.sector_check not in ("", "OK")
                                    for x in (n, e)))
                         else TEXT),
                    cell(a.base_station_id, 210),
                    cell(a.serial, 150),
                    cell("" if a.subunit is None else str(a.subunit), 36),
                    cell(tilt, 92, diff_color("Tilt (deg)")),
                    cell(state, 150, diff_color("State")),
                    cell(uid, 250, diff_color("Unique ID")),
                    cell(r.remark, 0, TEXT_MUTED),
                ], spacing=10),
                padding=ft.Padding.symmetric(horizontal=8, vertical=5),
                border=ft.Border(bottom=ft.BorderSide(
                    1, ft.Colors.with_opacity(0.4, BORDER)))))
        self.table_col.controls = lines

    def _show_site_check(self, check):
        ok = check["ok"]
        color = SUCCESS if ok else DANGER
        title = ("Same site" if ok else
                 "Site names do not match — make sure the .ims2 and the "
                 "Ericsson log are from the same site")
        cores = "   ".join(
            f"{n}: " + (f"'{c}'" if c else "nothing in common")
            for n, c in check["nodes"]) or "no Ericsson node name in the log"
        detail = f"Nokia {check['nokia'] or '?'}  ↔  {cores}"
        if check["same_device"]:
            detail += (f"   ·   {check['same_device']} RET(s) are the same "
                       f"physical device on both sides")
        self.site_banner.content = ft.Row([
            ft.Icon(ft.Icons.VERIFIED if ok else ft.Icons.WARNING_AMBER,
                    color=color, size=22),
            ft.Column([
                ft.Text(title, size=14, weight=ft.FontWeight.BOLD,
                        color=color),
                ft.Text(detail, size=12, color=TEXT, selectable=True,
                        font_family="Consolas"),
            ], spacing=2, expand=True),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.site_banner.bgcolor = ft.Colors.with_opacity(0.10, color)
        self.site_banner.border = ft.Border.all(
            1, ft.Colors.with_opacity(0.6, color))
        self.site_banner.visible = True

    @staticmethod
    def _chip(text, color):
        return ft.Container(
            ft.Text(text, size=13, color=color, weight=ft.FontWeight.BOLD),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.6, color)),
            bgcolor=ft.Colors.with_opacity(0.10, color),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6))

    # ── helpers ──────────────────────────────────────────────────
    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_col.controls.append(
            ft.Text(f"[{ts}] {msg}", size=12, color=TEXT_MUTED,
                    selectable=True, font_family="Consolas"))
        if not self._running:
            self._refresh()

    def _set_status(self, msg, color):
        self.status_text.value = msg
        self.status_text.color = color
        if not self._running:
            self._refresh()

    def _refresh(self):
        try:
            self.page.update()
        except Exception:
            pass
