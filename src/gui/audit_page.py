"""
audit_page.py — standalone CDD Audit page.

Just enter the Site ID + browse the CDD(s). The page then:
  * auto-finds the node dumps in LOG/<SITEID>/DUMP/ (*_modump.zip /
    *_cmdump.zip), identifies each node from the filename, and parses ALL
    of them (multi-node),
  * for GSM, queries live via cmedit (BSC level) using the SSH credentials
    entered on the MAIN form (auto-filtered by Site ID, like the GSM checks
    in the integration flow),
  * compares everything against the config-driven mapping (audit_map.json)
    and exports a single Excel report.
"""
import glob
import os
import re
import threading
from datetime import datetime

import flet as ft

from gui.theme import (
    ACCENT, ACCENT_WARM, BG_BOTTOM, BG_TOP, BORDER, DANGER, INFO, PANEL,
    SUCCESS, TEXT, TEXT_MUTED, background_gradient, panel,
    primary_button_style, secondary_button_style,
)


class AuditPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self.file_picker = ft.FilePicker()
        self._result_path = None
        self._form = getattr(page, "integration_form", {}) or {}
        self._results = None        # last audit results (for script generation)
        self._gen_ctx = None        # {site, safe, out_dir, xlsx}

    # ── UI ───────────────────────────────────────────────────────
    def build(self) -> ft.View:
        try:
            self.page.title = "NodeCraft — CDD Audit"
            self.page.update()
        except Exception:
            pass

        self.site_field = self._tf("Site ID", self._form.get("shortcode", ""), expand=1)
        self.lte_field = self._tf("LTE CDD file", "", expand=3)
        self.nr_field = self._tf("NR CDD file", "", expand=3)
        self.gsm_field = self._tf("GSM CDD file (audited live via cmedit)", "", expand=3)
        self.dump_field = self._tf(
            "Dump file(s) — optional; cm/modump. Blank = auto-find in "
            "LOG/<SiteID>/DUMP/", "", expand=3)

        self.status_text = ft.Text("", size=13, color=TEXT_MUTED)
        self.log_col = ft.Column(
            [], spacing=2, scroll=ft.ScrollMode.AUTO, expand=True, auto_scroll=True)
        self.run_btn = ft.ElevatedButton(
            "Run Audit", icon=ft.Icons.FACT_CHECK,
            style=primary_button_style(), on_click=self._run_audit)
        self.open_btn = ft.ElevatedButton(
            "Open Report", icon=ft.Icons.OPEN_IN_NEW, visible=False,
            style=secondary_button_style(), on_click=self._open_result)
        self.gen_btn = ft.ElevatedButton(
            "Generate Scripts", icon=ft.Icons.TERMINAL, visible=False,
            style=secondary_button_style(), on_click=self._generate_scripts)

        def browse_row(field, handler, label):
            return ft.Row([
                field,
                ft.ElevatedButton(label, icon=ft.Icons.FOLDER_OPEN,
                                  style=secondary_button_style(),
                                  on_click=handler),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        creds_ok = bool(self._form.get("host") and self._form.get("username"))
        creds_line = (f"SSH from main form: {self._form.get('username','?')}@"
                      f"{self._form.get('host','?')}  (used for GSM cmedit)"
                      if creds_ok else
                      "⚠ No SSH credentials from the main form — GSM (cmedit) "
                      "audit will be skipped. Fill the form first.")

        body = ft.Container(
            expand=True, gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=24, vertical=16),
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.FACT_CHECK, size=26, color=ACCENT),
                    ft.Text("CDD Audit", size=22, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Container(expand=True),
                    ft.TextButton("← Back", on_click=lambda e: self.page.go("/form")),
                ]),
                ft.Text("Enter the Site ID and pick the CDD(s). Node dumps are "
                        "auto-found in LOG/<SiteID>/DUMP/ (all nodes). GSM is "
                        "queried live via cmedit. Mapping is config-driven "
                        "(audit_map.json).", size=12, color=TEXT_MUTED),
                panel(ft.Column([
                    ft.Row([self.site_field], spacing=10),
                    browse_row(self.lte_field, self._browse_lte, "LTE CDD"),
                    browse_row(self.nr_field, self._browse_nr, "NR CDD"),
                    browse_row(self.gsm_field, self._browse_gsm, "GSM CDD"),
                    browse_row(self.dump_field, self._browse_dump, "Dump(s)"),
                    ft.Text(creds_line, size=11,
                            color=(TEXT_MUTED if creds_ok else ACCENT_WARM)),
                    ft.Row([self.run_btn, self.open_btn, self.gen_btn,
                            self.status_text],
                           spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=12), bgcolor=PANEL, padding=18),
                panel(ft.Column([
                    ft.Text("LOG", size=11, color=TEXT_MUTED, weight=ft.FontWeight.BOLD),
                    self.log_col,
                ], spacing=6, expand=True), bgcolor=PANEL, padding=12, expand=True),
            ], spacing=12, expand=True),
        )
        return ft.View(route="/audit", padding=0, spacing=0, bgcolor=BG_TOP,
                       controls=[body], services=[self.file_picker])

    def _tf(self, label, value, expand=None):
        return ft.TextField(
            label=label, value=value, expand=expand, filled=True,
            border_radius=14, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED),
            text_style=ft.TextStyle(color=TEXT, size=13))

    # ── Browse handlers ──────────────────────────────────────────
    async def _pick_cdd(self, field, title):
        files = await self.file_picker.pick_files(
            dialog_title=title, allowed_extensions=["xlsx", "xlsm"],
            file_type=ft.FilePickerFileType.CUSTOM, allow_multiple=False)
        if files:
            field.value = files[0].path
            self.page.update()

    async def _browse_lte(self, e):
        await self._pick_cdd(self.lte_field, "Select LTE CDD")

    async def _browse_nr(self, e):
        await self._pick_cdd(self.nr_field, "Select NR CDD")

    async def _browse_gsm(self, e):
        await self._pick_cdd(self.gsm_field, "Select GSM CDD")

    async def _browse_dump(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select node dump(s) — cmdump/modump",
            allowed_extensions=["zip", "gz", "log", "xml"],
            file_type=ft.FilePickerFileType.CUSTOM, allow_multiple=True)
        if files:
            self.dump_field.value = " | ".join(f.path for f in files)
            self.page.update()

    # ── Run ──────────────────────────────────────────────────────
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_col.controls.append(
            ft.Text(f"[{ts}] {msg}", size=12, color=TEXT_MUTED,
                    selectable=True, font_family="Consolas"))
        if len(self.log_col.controls) > 400:
            self.log_col.controls = self.log_col.controls[-350:]
        try:
            self.page.update()
        except Exception:
            pass

    def _run_audit(self, e):
        site = self.site_field.value.strip()
        lte = self.lte_field.value.strip()
        nr = self.nr_field.value.strip()
        gsm = self.gsm_field.value.strip()
        dumps = [p.strip() for p in self.dump_field.value.split("|") if p.strip()]
        if not site:
            self._set_status("Site ID is required.", DANGER)
            return
        if not (lte or nr or gsm):
            self._set_status("Pick at least one CDD (LTE / NR / GSM).", DANGER)
            return
        self.run_btn.disabled = True
        self.open_btn.visible = False
        self.gen_btn.visible = False
        self.status_text.value = "Running audit..."
        self.status_text.color = ACCENT
        self.page.update()
        threading.Thread(target=self._worker, args=(site, lte, nr, gsm, dumps),
                         daemon=True).start()

    def _set_status(self, msg, color):
        self.status_text.value = msg
        self.status_text.color = color
        self.page.update()

    def _worker(self, site, lte, nr, gsm, dumps=None):
        ssh = None
        dumps = dumps or []
        try:
            from audit import dump_parser, cdd_reader, audit_core, cmedit_source
            from app_path import get_app_dir

            safe = re.sub(r"[^A-Za-z0-9._-]", "_", site)
            records = {}
            nodes = []

            # ── 1. Parse node dumps — browsed file(s) first, then auto-find ──
            # Browsed dumps let you audit a node whose dump isn't in the site
            # folder, or point at a specific cm/modump. Both formats are
            # auto-detected by the parser. Auto-find still runs for LTE/NR.
            found = list(dumps)
            if lte or nr:
                dump_dir = os.path.join(get_app_dir(), "LOG", safe, "DUMP")
                auto = (sorted(glob.glob(os.path.join(dump_dir, "*_cmdump.zip")))
                        + sorted(glob.glob(os.path.join(dump_dir, "*_modump.zip"))))
                if not auto and not dumps:
                    self._log(f"⚠ No dumps in {dump_dir} and none browsed — "
                              "LTE/NR audit will find nothing.")
                found += auto
            for path in found:
                if not os.path.isfile(path):
                    self._log(f"  ✗ dump not found: {path}")
                    continue
                base = os.path.basename(path)
                node = re.sub(r"_(cm|mo)dump\.(zip|gz|log|xml)$", "", base)
                node = re.sub(r"\.(zip|gz|log|xml)$", "", node)
                # Scope to the entered Site ID — a browsed dump left over from a
                # previous site (the field isn't cleared when Site ID changes)
                # must not leak another site's nodes into this report.
                if not node.lower().startswith(site.lower()):
                    self._log(f"  ⚠ {base}: node '{node}' ≠ Site ID '{site}' — "
                              "skipped (wrong site).")
                    continue
                if node in nodes:
                    continue          # already parsed this node's dump (cmdump wins)
                self._log(f"Parsing dump for {node}: {base} ...")
                try:
                    recs = dump_parser.parse_dump(path)
                    records.update(recs)
                    nodes.append(node)
                    self._log(f"  → {len(recs)} MO(s).")
                except Exception as exc:
                    self._log(f"  ✗ parse failed: {exc}")
            self._log(f"Nodes from dumps: {', '.join(nodes) or '(none)'}")

            cdd_paths = {}
            if lte:
                cdd_paths["lte_nr"] = lte
            if nr:
                cdd_paths["nr"] = nr
            if gsm:
                cdd_paths["gsm"] = gsm

            audit_map = cdd_reader.load_map()

            # ── 2. GSM: live cmedit (BSC), SSH creds from main form ──
            if gsm:
                gsm_cmedit = [p for p in audit_map.get("profiles", [])
                              if p.get("tech") == "gsm" and p.get("source") == "cmedit"]
                host = self._form.get("host", "")
                user = self._form.get("username", "")
                pwd = self._form.get("password", "")
                if gsm_cmedit and host and user and pwd:
                    try:
                        port = int(self._form.get("port", 5023) or 5023)
                    except (ValueError, TypeError):
                        port = 5023
                    from integration_runner import IntegrationSSH
                    # cli.py hits ENM directly — no AMOS/node attach needed;
                    # cells are scoped by Site ID (gerancellid==M<digits>*). We
                    # deliberately do NOT pull a node from the main form here —
                    # doing so leaked that node (e.g. a leftover MIN4893) into
                    # the CDD read keys and produced foreign rows in the report.
                    self._log(f"SSH → {host}:{port} as {user} for GSM cmedit "
                              f"(scope Site ID {site})...")
                    ssh = IntegrationSSH(host=host, port=port, username=user,
                                         password=pwd, log_callback=self._log)
                    ssh.connect(timeout=30)
                    gsm_recs = cmedit_source.fetch_cmedit_records(
                        ssh, "", gsm_cmedit, site_id=site, log=self._log)
                    records.update(gsm_recs)
                elif gsm_cmedit and not (host and user and pwd):
                    self._log("⚠ GSM CDD given but no SSH from the main form — "
                              "GSM (cmedit) audit skipped.")

            if not records:
                self._fail("No node data (no dumps found and no GSM cmedit).")
                return
            if not nodes:
                # last resort: derive nodes from record LDNs
                seen = set()
                for ldn in records:
                    m = re.search(r"ManagedElement=([^,]+)", ldn)
                    if m:
                        seen.add(m.group(1))
                nodes = sorted(seen)

            # ── 3. Read CDD items for every node at the site ───────
            # LTE/NR rows match per dump-node (eNodeBName/gNBName); GSM rows
            # match by Site ID (gsmNodeName starts with it). So read CDD for
            # each dump node AND the Site ID itself, then de-dup.
            self._log("Reading CDD (config-driven) for all nodes...")
            read_keys = list(nodes)
            if site and site not in read_keys:
                read_keys.append(site)
            items = []
            seen_items = set()
            for node in read_keys:
                for it in cdd_reader.read_audit_items(cdd_paths, node, audit_map,
                                                      log=self._log):
                    sig = (it.tech, it.category, it.mo_local,
                           it.parameter, it.key, it.expected)
                    if sig in seen_items:
                        continue
                    seen_items.add(sig)
                    items.append(it)
            self._log(f"Total expected params: {len(items)} across "
                      f"{len(read_keys)} key(s).")
            if not items:
                self._fail("No CDD rows matched these nodes "
                           "(check node names vs eNodeBName/gsmNodeName in CDD).")
                return

            # ── 4. Compare + Excel ─────────────────────────────────
            results = audit_core.compare(items, records)
            from collections import Counter
            c = Counter(r.status for r in results)
            self._log(f"Result: Match={c['Match']} Mismatch={c['Mismatch']} "
                      f"ParamNotFound={c['NotFound']} MO_NotFound={c['MO_NotFound']}")

            out_dir = os.path.join(get_app_dir(), "LOG", safe, "AUDIT")
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = os.path.join(out_dir, f"{safe}_audit_{ts}.xlsx")
            audit_core.write_excel(results, out, {
                "Site ID": site,
                "Nodes": ", ".join(nodes),
                "LTE CDD": os.path.basename(lte) if lte else "-",
                "NR CDD": os.path.basename(nr) if nr else "-",
                "GSM CDD": os.path.basename(gsm) if gsm else "-",
                "Generated": ts,
            })
            self._result_path = out
            self._results = results
            self._gen_ctx = {"site": site, "out_dir": out_dir, "xlsx": out}
            self._log(f"✓ Report saved: {out}")
            self.status_text.value = (
                f"Done — {c['Mismatch']} mismatch, "
                f"{c['NotFound']+c['MO_NotFound']} not found.")
            self.status_text.color = SUCCESS if c["Mismatch"] == 0 else ACCENT_WARM
            self.open_btn.visible = True
            self.gen_btn.visible = c["Mismatch"] > 0
        except Exception as exc:
            self._fail(f"Audit failed: {exc}")
        finally:
            if ssh is not None:
                try:
                    ssh.exit_amos()
                except Exception:
                    pass
                try:
                    ssh.disconnect()
                except Exception:
                    pass
            self.run_btn.disabled = False
            try:
                self.page.update()
            except Exception:
                pass

    def _fail(self, msg):
        self._log("✗ " + msg)
        self.status_text.value = msg
        self.status_text.color = DANGER

    def _open_result(self, e):
        if self._result_path and os.path.isfile(self._result_path):
            try:
                os.startfile(self._result_path)   # Windows
            except Exception as exc:
                self._log(f"Could not open file: {exc}")

    def _generate_scripts(self, e):
        if not self._results or not self._gen_ctx:
            self._log("Nothing to generate — run an audit first.")
            return
        try:
            from audit import audit_core
            ctx = self._gen_ctx
            script_dir = os.path.join(ctx["out_dir"], "SCRIPTS")
            by = (self._form.get("username") or "").strip() or "NodeCraft"
            paths = audit_core.generate_moshell_scripts(
                self._results, script_dir, ctx["site"], ctx["xlsx"],
                generated_by=by, statuses=("Mismatch",))
            if not paths:
                self._log("No Mismatch rows — nothing to generate.")
                self._set_status("No mismatches to script.", ACCENT_WARM)
                return
            self._log(f"✓ Generated {len(paths)} script(s) in {script_dir}:")
            for p in paths:
                self._log("   " + os.path.basename(p))
            self._set_status(f"Generated {len(paths)} script(s).", SUCCESS)
            try:
                os.startfile(script_dir)   # open the folder (Windows)
            except Exception:
                pass
        except Exception as exc:
            self._log(f"✗ Script generation failed: {exc}")
            self._set_status("Script generation failed.", DANGER)
