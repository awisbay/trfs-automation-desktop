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
import asyncio
import glob
import json
import os
import re
import threading
import time
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
        # Restore the page's own inputs (CDD/dump/cluster/batch) from the last
        # visit so navigating away and back doesn't wipe them.
        try:
            from session import load_page_state
            self._state = load_page_state("audit")
        except Exception:
            self._state = {}

    # ── UI ───────────────────────────────────────────────────────
    def build(self) -> ft.View:
        try:
            self.page.title = "NodeCraft — CDD Audit"
            self.page.update()
        except Exception:
            pass

        st = self._state
        self.site_field = self._tf(
            "Site ID",
            st.get("site") or self._form.get("shortcode", ""), expand=1)
        self.lte_field = self._tf("LTE CDD file", st.get("lte", ""), expand=3)
        self.nr_field = self._tf("NR CDD file", st.get("nr", ""), expand=3)
        self.gsm_field = self._tf(
            "GSM CDD file (audited live via cmedit)", st.get("gsm", ""), expand=3)
        self.lld_field = self._tf(
            "LLD file — optional; baseband type + RiLink/CPRI port allocation",
            st.get("lld", ""), expand=3)
        self.dump_field = self._tf(
            "Dump file(s) — optional; cm/modump. Blank = auto-find in "
            "LOG/<SiteID>/DUMP/", st.get("dump", ""), expand=3)
        self.cluster_field = self._tf(
            "Cluster name", st.get("cluster", ""), expand=None)
        self.cluster_field.width = 260
        self.batch_count_text = ft.Text("", size=12, color=ACCENT,
                                        weight=ft.FontWeight.BOLD)
        self.batch_field = ft.TextField(
            label="Batch nodes — node1;node2;node3;…  (live cmedit export). "
                  "Leave blank to use the single-site Site ID above.",
            value=st.get("batch", ""),
            multiline=True, min_lines=2, max_lines=5, expand=True,
            filled=True, border_radius=14, on_change=self._on_batch_change,
            bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED),
            text_style=ft.TextStyle(color=TEXT, size=13))

        self.status_text = ft.Text("", size=13, color=TEXT_MUTED)
        self.timer_text = ft.Text("", size=13, color=ACCENT,
                                  weight=ft.FontWeight.BOLD)
        # Fixed, generous height so the LOG is spacious; the whole page
        # scrolls if the window is short. Toggle button grows it further.
        self.log_col = ft.Column(
            [], spacing=2, scroll=ft.ScrollMode.AUTO, height=460,
            auto_scroll=True)
        self.run_btn = ft.ElevatedButton(
            "Run Audit", icon=ft.Icons.FACT_CHECK,
            style=primary_button_style(), on_click=self._run_audit)
        self.open_btn = ft.ElevatedButton(
            "Open Report", icon=ft.Icons.OPEN_IN_NEW, visible=False,
            style=secondary_button_style(), on_click=self._open_result)
        self.gen_btn = ft.ElevatedButton(
            "Generate Scripts", icon=ft.Icons.TERMINAL, visible=False,
            style=secondary_button_style(), on_click=self._generate_scripts)
        self.runscript_btn = ft.ElevatedButton(
            "Run Scripts", icon=ft.Icons.PLAY_ARROW, visible=False,
            style=secondary_button_style(), on_click=self._open_run_scripts)
        self.apply_btn = ft.ElevatedButton(
            "Apply (cmedit)", icon=ft.Icons.BOLT, visible=False,
            style=secondary_button_style(), on_click=self._apply_cmedit)
        self.editmap_btn = ft.OutlinedButton(
            "Edit Map", icon=ft.Icons.EDIT_NOTE, on_click=self._open_map_editor)

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
                    self.editmap_btn,
                    ft.TextButton(
                        ("← Back to Summary"
                         if getattr(self.page, "integration_has_summary", False)
                         else "← Back"),
                        on_click=self._go_back),
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
                    browse_row(self.lld_field, self._browse_lld, "LLD"),
                    browse_row(self.dump_field, self._browse_dump, "Dump(s)"),
                    ft.Divider(height=1, color=BORDER),
                    ft.Text("Batch / Cluster audit — export many nodes live "
                            "into one dump, then audit all at once:",
                            size=11, color=TEXT_MUTED),
                    ft.Row([self.cluster_field, self.batch_count_text,
                            ft.Container(expand=True)],
                           spacing=12,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Row([self.batch_field], spacing=10),
                    ft.Text(creds_line, size=11,
                            color=(TEXT_MUTED if creds_ok else ACCENT_WARM)),
                    ft.Row([self.run_btn, self.open_btn, self.gen_btn,
                            self.runscript_btn, self.apply_btn,
                            self.timer_text, self.status_text],
                           spacing=14, wrap=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=12), bgcolor=PANEL, padding=18),
                panel(ft.Column([
                    ft.Row([
                        ft.Text("LOG", size=11, color=TEXT_MUTED,
                                weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        ft.IconButton(
                            icon=ft.Icons.OPEN_IN_FULL, icon_size=16,
                            tooltip="Expand / shrink the log",
                            on_click=self._toggle_log_height),
                    ]),
                    self.log_col,
                ], spacing=6), bgcolor=PANEL, padding=12),
            ], spacing=12, scroll=ft.ScrollMode.AUTO),
        )
        return ft.View(route="/audit", padding=0, spacing=0, bgcolor=BG_TOP,
                       controls=[body], services=[self.file_picker])

    def _save_state(self):
        """Persist the page's own inputs so they survive navigating away."""
        try:
            from session import save_page_state
            save_page_state("audit", {
                "site": self.site_field.value,
                "lte": self.lte_field.value,
                "nr": self.nr_field.value,
                "gsm": self.gsm_field.value,
                "lld": self.lld_field.value,
                "dump": self.dump_field.value,
                "cluster": self.cluster_field.value,
                "batch": self.batch_field.value,
            })
        except Exception:
            pass

    def _go_back(self, e):
        """Return to the Integration Summary if the audit was opened from it
        (re-shows the summary without re-running); otherwise go to the form."""
        self._save_state()
        if getattr(self.page, "integration_has_summary", False):
            self.page.integration_show_summary_only = True
            self.page.go("/integration_run")
        else:
            self.page.go("/form")

    def _toggle_log_height(self, e):
        self._log_big = not getattr(self, "_log_big", False)
        self.log_col.height = 900 if self._log_big else 460
        try:
            self.page.update()
        except Exception:
            pass

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
            self._save_state()
            self.page.update()

    async def _browse_lte(self, e):
        await self._pick_cdd(self.lte_field, "Select LTE CDD")

    async def _browse_nr(self, e):
        await self._pick_cdd(self.nr_field, "Select NR CDD")

    async def _browse_gsm(self, e):
        await self._pick_cdd(self.gsm_field, "Select GSM CDD")

    async def _browse_lld(self, e):
        await self._pick_cdd(self.lld_field, "Select LLD (Tx/CPRI) workbook")

    def _on_batch_change(self, e):
        nodes = [n for n in re.split(r"[;\n]+", self.batch_field.value)
                 if n.strip()]
        self.batch_count_text.value = (
            f"→ {len(nodes)} node(s) to audit" if nodes else "")
        try:
            self.page.update()
        except Exception:
            pass

    async def _browse_dump(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select node dump(s) — cmdump/modump",
            allowed_extensions=["zip", "gz", "log", "xml"],
            file_type=ft.FilePickerFileType.CUSTOM, allow_multiple=True)
        if files:
            self.dump_field.value = " | ".join(f.path for f in files)
            self._save_state()
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
        self._save_state()
        site = self.site_field.value.strip()
        lte = self.lte_field.value.strip()
        nr = self.nr_field.value.strip()
        gsm = self.gsm_field.value.strip()
        lld = self.lld_field.value.strip()
        dumps = [p.strip() for p in self.dump_field.value.split("|") if p.strip()]
        cluster = self.cluster_field.value.strip()
        batch_nodes = [n.strip() for n in re.split(r"[;\n]+", self.batch_field.value)
                       if n.strip()]
        is_batch = bool(batch_nodes)
        if not is_batch and not site:
            self._set_status("Site ID (or batch nodes) is required.", DANGER)
            return
        if is_batch and not cluster:
            self._set_status("Cluster name is required for batch audit.", DANGER)
            return
        if not (lte or nr or gsm or lld):
            self._set_status("Pick at least one CDD (LTE / NR / GSM / LLD).", DANGER)
            return
        self.run_btn.disabled = True
        self.open_btn.visible = False
        self.gen_btn.visible = False
        self.status_text.value = ("Running batch audit..." if is_batch
                                  else "Running audit...")
        self.status_text.color = ACCENT
        self._start_timer()
        self.page.update()
        threading.Thread(
            target=self._worker,
            args=(site, lte, nr, gsm, dumps, cluster, batch_nodes, lld),
            daemon=True).start()

    # ── Elapsed timer ────────────────────────────────────────────
    # Cross-thread page.update() is unreliable in this Flet build, so — like
    # the integration progress page — the worker/timer threads only mutate
    # control values and a single async loop on the event loop repaints.
    def _start_timer(self):
        self._t0 = time.time()
        self._timer_on = True
        threading.Thread(target=self._tick, daemon=True).start()
        try:
            self.page.run_task(self._ui_loop)
        except Exception:
            pass

    def _tick(self):
        while getattr(self, "_timer_on", False):
            self.timer_text.value = f"⏱ {self._fmt_elapsed(time.time() - self._t0)}"
            time.sleep(1)

    async def _ui_loop(self):
        while getattr(self, "_timer_on", False):
            try:
                self.page.update()
            except Exception:
                pass
            await asyncio.sleep(0.5)
        try:
            self.page.update()
        except Exception:
            pass

    def _stop_timer(self):
        self._timer_on = False
        if hasattr(self, "_t0"):
            self.timer_text.value = f"⏱ {self._fmt_elapsed(time.time() - self._t0)}"

    @staticmethod
    def _fmt_elapsed(sec: float) -> str:
        sec = int(sec)
        m, s = divmod(sec, 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def _set_status(self, msg, color):
        self.status_text.value = msg
        self.status_text.color = color
        self.page.update()

    def _worker(self, site, lte, nr, gsm, dumps=None, cluster="", batch_nodes=None,
                lld=""):
        ssh = None
        dumps = dumps or []
        batch_nodes = batch_nodes or []
        is_batch = bool(batch_nodes)
        try:
            from audit import dump_parser, cdd_reader, audit_core, cmedit_source
            from app_path import get_app_dir

            label = cluster if is_batch else site
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)
            records = {}
            nodes = []

            # ── 0. BATCH: export all nodes live into one combined dump ─────
            if is_batch:
                from integration_runner import IntegrationSSH, run_take_cm_dump_batch
                host = self._form.get("host", "")
                user = self._form.get("username", "")
                pwd = self._form.get("password", "")
                if not (host and user and pwd):
                    self._fail("Batch audit needs SSH credentials from the main "
                               "form (host/username/password).")
                    return
                try:
                    port = int(self._form.get("port", 5023) or 5023)
                except (ValueError, TypeError):
                    port = 5023
                ssh = IntegrationSSH(host=host, port=port, username=user,
                                     password=pwd, log_callback=self._log)
                ssh.connect(timeout=30)
                if lte or nr or lld:
                    self._log(f"Batch: {len(batch_nodes)} node(s) → live cmedit "
                              f"export (cluster '{cluster}').")
                    zip_path = os.path.join(get_app_dir(), "LOG", safe, "DUMP",
                                            f"{safe}_batch_cmdump.zip")
                    ok, _out = run_take_cm_dump_batch(
                        ssh, batch_nodes, cluster, zip_path, self._log)
                    if not ok:
                        self._fail("Batch CM export failed — see log.")
                        return
                    self._log(f"Parsing combined dump: "
                              f"{os.path.basename(zip_path)} ...")
                    records.update(dump_parser.parse_dump(zip_path))
                    seen = set()
                    for ldn in records:
                        m = re.search(r"ManagedElement=([^,]+)", ldn)
                        if m:
                            seen.add(m.group(1))
                    nodes = sorted(seen)
                    self._log(f"  → {len(records)} MO(s) across {len(nodes)} "
                              f"node(s): {', '.join(nodes[:6])}"
                              f"{' …' if len(nodes) > 6 else ''}")
                else:
                    # GSM-only batch — no dump needed; use the node names for
                    # CDD row matching (GSM values come live via cmedit).
                    nodes = list(dict.fromkeys(n.strip() for n in batch_nodes
                                               if n.strip()))
                    self._log(f"GSM-only batch: {len(nodes)} node(s), no dump "
                              "export.")

            # ── 1. Parse node dumps — browsed file(s) first, then auto-find ──
            # Browsed dumps let you audit a node whose dump isn't in the site
            # folder, or point at a specific cm/modump. Both formats are
            # auto-detected by the parser. Auto-find still runs for LTE/NR.
            found = list(dumps)
            if not is_batch and (lte or nr or lld):
                dump_dir = os.path.join(get_app_dir(), "LOG", safe, "DUMP")
                root_cmdumps = sorted(
                    glob.glob(os.path.join(dump_dir, "*_cmdump.zip")),
                    key=os.path.getmtime, reverse=True,
                )
                root_modumps = glob.glob(os.path.join(dump_dir, "*_modump.zip"))
                cutover_modumps = glob.glob(os.path.join(
                    get_app_dir(), "LOG", safe, "PRE_CUTOVER", "DUMP",
                    "*_modump_*.zip",
                ))
                modumps = sorted(
                    root_modumps + cutover_modumps,
                    key=os.path.getmtime, reverse=True,
                )
                auto = root_cmdumps + modumps
                if not auto and not dumps:
                    self._log(f"⚠ No dumps in {dump_dir} and none browsed — "
                              "LTE/NR audit will find nothing.")
                found += auto
            for path in found:
                if not os.path.isfile(path):
                    self._log(f"  ✗ dump not found: {path}")
                    continue
                base = os.path.basename(path)
                node = re.sub(
                    r"_(cm|mo)dump(?:_\d{8}_\d{6})?\.(zip|gz|log|xml)$",
                    "", base,
                )
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
            gsm_cmedit = [p for p in audit_map.get("profiles", [])
                          if p.get("tech") == "gsm" and p.get("source") == "cmedit"]
            # Batch: derive Site IDs from the node list (part before '_',
            # de-duplicated), then fetch GSM per site — reusing the batch SSH.
            if gsm and is_batch and gsm_cmedit and ssh is not None:
                sites = sorted({n.split("_")[0].strip()
                                for n in batch_nodes if n.strip()})
                self._log(f"GSM batch: {len(sites)} site(s) from node list → "
                          f"{', '.join(sites)}")
                for s in sites:
                    self._log(f"  GSM cmedit for site {s} ...")
                    try:
                        recs = cmedit_source.fetch_cmedit_records(
                            ssh, "", gsm_cmedit, site_id=s, log=self._log)
                        records.update(recs)
                    except Exception as exc:
                        self._log(f"  ✗ GSM cmedit {s}: {exc}")
            if gsm and not is_batch:
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
            if not items and not lld:
                self._fail("No CDD rows matched these nodes "
                           "(check node names vs eNodeBName/gsmNodeName in CDD).")
                return

            # ── 4. Compare + Excel ─────────────────────────────────
            from collections import Counter
            # Fold Trx (RadioNode) up onto GsmSector so arfcnMin/Max come from
            # the dump — no live cmedit needed.
            try:
                audit_core.aggregate_trx(records)
            except Exception as exc:
                self._log(f"trx aggregation failed: {exc}")
            # TRX COUNT rows use a per-sector aggregate compare, not the normal
            # per-attribute one — pull them out and audit them separately.
            trx_items = [it for it in items if it.parameter == "__trxcount__"]
            # IP-broker rows: expected value comes from config.json (broker IP
            # for the CDD's BSC), not a CDD column — audited by broker_check.
            broker_items = [it for it in items if it.parameter == "__bscbroker__"]
            items = [it for it in items
                     if it.parameter not in ("__trxcount__", "__bscbroker__")]
            results = audit_core.compare(items, records)
            try:
                tc = audit_core.trx_count_check(trx_items, records)
                if tc:
                    results += tc
                    tcc = Counter(r.status for r in tc)
                    self._log(f"TRX count: {tcc.get('Match',0)} sector(s) match, "
                              f"{tcc.get('Mismatch',0)} mismatch.")
            except Exception as exc:
                self._log(f"trx count check failed: {exc}")
            try:
                if broker_items:
                    try:
                        from integration_runner import get_config
                        bsc_broker_map = (get_config() or {}).get(
                            "bsc_broker_map", {})
                    except Exception:
                        bsc_broker_map = {}
                    bk = audit_core.broker_check(
                        broker_items, records, bsc_broker_map)
                    if bk:
                        results += bk
                        bkc = Counter(r.status for r in bk)
                        self._log(
                            f"IP broker: {bkc.get('Match',0)} node(s) match, "
                            f"{bkc.get('Mismatch',0)} wrong broker, "
                            f"{bkc.get('NotFound',0)} unresolved.")
            except Exception as exc:
                self._log(f"ip broker check failed: {exc}")

            # Cell inventory → its OWN sheet (per-cell CDD vs node), not mixed
            # into the parameter Detail.
            cell_rows = []
            try:
                cell_rows = audit_core.cell_inventory_rows(items, records)
                if cell_rows:
                    ic = Counter(r.status for r in cell_rows)
                    self._log(f"Cell inventory: {ic.get('Match',0)} match, "
                              f"{ic.get('Missing',0)} missing, "
                              f"{ic.get('Extra',0)} extra.")
            except Exception as exc:
                self._log(f"cell inventory failed: {exc}")

            # Reverse pass: nodes present in the data but with NO CDD rows (e.g.
            # a batch node the CDD doesn't cover) — surface their ACTUAL audited
            # params with the CDD column blank and status "CDD missing", instead
            # of the node silently vanishing from the report.
            covered = {(it.node or "").strip().lower() for it in items}
            uncovered = [n for n in nodes
                         if n and n.strip().lower() not in covered]
            if uncovered:
                gsm_attrs, lte_ca, ref_attrs = audit_core.audited_attr_sets(
                    audit_map)
                try:
                    from integration_runner import gsm_cell_id_re
                except Exception:
                    gsm_cell_id_re = None
                for n in uncovered:
                    cell_rx = None
                    if gsm_cell_id_re:
                        try:
                            cell_rx = gsm_cell_id_re(n.split("_")[0])
                        except Exception:
                            cell_rx = None
                    rev = audit_core.build_reverse_rows(
                        n, records, gsm_attrs, lte_ca, ref_attrs, cell_rx)
                    if rev:
                        results += rev
                        self._log(f"CDD missing: {n} not in CDD → "
                                  f"{len(rev)} node param(s) listed.")

            # LLD (baseband type + RiLink/CPRI port allocation) — driven off the
            # node dump (FieldReplaceableUnit / RiLink MOs), per dump-node. Kept
            # in its own list → its own styled 'LLD' sheet (layout columns), not
            # mixed into the logical CDD Detail rows.
            lld_results = []
            if lld:
                if os.path.isfile(lld):
                    from audit import lld_audit
                    self._log(f"LLD audit ({os.path.basename(lld)}) for "
                              f"{len(nodes)} node(s)...")
                    for node in nodes:
                        try:
                            lld_results += lld_audit.audit_lld(
                                lld, node, records, log=self._log)
                        except Exception as exc:
                            self._log(f"  ✗ LLD audit {node}: {exc}")
                    lc = Counter(x.status for x in lld_results)
                    self._log(f"LLD: Match={lc['Match']} Mismatch={lc['Mismatch']} "
                              f"NotFound={lc['NotFound']} Extra={lc['Extra']}")
                else:
                    self._log(f"⚠ LLD file not found: {lld}")

            c = Counter(r.status for r in results)
            self._log(f"Result: Match={c['Match']} Mismatch={c['Mismatch']} "
                      f"ParamNotFound={c['NotFound']} MO_NotFound={c['MO_NotFound']}")

            out_dir = os.path.join(get_app_dir(), "LOG", safe, "AUDIT")
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = os.path.join(out_dir, f"{safe}_audit_{ts}.xlsx")
            audit_core.write_excel(results, out, {
                ("Cluster" if is_batch else "Site ID"): label,
                "Nodes": f"{len(nodes)} node(s): " + ", ".join(nodes),
                "LTE CDD": os.path.basename(lte) if lte else "-",
                "NR CDD": os.path.basename(nr) if nr else "-",
                "GSM CDD": ("(n/a in batch)" if is_batch
                            else (os.path.basename(gsm) if gsm else "-")),
                "LLD": os.path.basename(lld) if lld else "-",
                "Mode": "Batch / Cluster" if is_batch else "Single site",
                "Generated": ts,
            }, lld_results=lld_results, cell_rows=cell_rows)
            self._result_path = out
            self._results = results
            self._gen_ctx = {"site": label, "out_dir": out_dir, "xlsx": out}
            self._log(f"✓ Report saved: {out}")
            lc = Counter(x.status for x in lld_results)
            lld_tail = (f" · LLD {lc['Mismatch']} mismatch/"
                        f"{lc['NotFound']} not-found/{lc['Extra']} extra"
                        if lld_results else "")
            self.status_text.value = (
                f"Done — {c['Mismatch']} mismatch, "
                f"{c['NotFound']+c['MO_NotFound']} not found.{lld_tail}")
            clean = c["Mismatch"] == 0 and lc["Mismatch"] == 0 and lc["NotFound"] == 0
            self.status_text.color = SUCCESS if clean else ACCENT_WARM
            self.open_btn.visible = True
            self.gen_btn.visible = c["Mismatch"] > 0
            self.apply_btn.visible = c["Mismatch"] > 0
        except Exception as exc:
            self._fail(f"Audit failed: {exc}")
        finally:
            self._stop_timer()
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
            fdn_prefix = (self._form.get("subnetwork") or "").strip()
            # GSM GeranCell lives on the BSC (not the RadioNode): build its FDN
            # from the configurable audit_map template, filling {bsc} from the
            # main form's BSC name (left as <BSC> to edit if unknown).
            gsm_fdn_prefix = ""
            gsm_child_map = {}
            try:
                from audit import cdd_reader
                amap = cdd_reader.load_map()
                tmpl = amap.get("gsm_fdn_prefix", "")
                bsc = (self._form.get("bsc_name") or "").strip() or "<BSC>"
                gsm_fdn_prefix = tmpl.replace("{bsc}", bsc)
                gsm_child_map = audit_core.build_gsm_child_map(amap)
            except Exception:
                pass
            # Three formats from the same Mismatch rows: moshell (.mos, the only
            # one the Run button executes) + ENM CMEdit + CM Bulk (separate .txt
            # files — different command syntax, review-and-apply manually).
            paths = audit_core.generate_moshell_scripts(
                self._results, script_dir, ctx["site"], ctx["xlsx"],
                generated_by=by, statuses=("Mismatch",))
            if not paths:
                self._log("No Mismatch rows — nothing to generate.")
                self._set_status("No mismatches to script.", ACCENT_WARM)
                return
            cmedit_paths = audit_core.generate_cmedit_scripts(
                self._results, script_dir, ctx["site"], ctx["xlsx"],
                generated_by=by, statuses=("Mismatch",), fdn_prefix=fdn_prefix,
                gsm_fdn_prefix=gsm_fdn_prefix, gsm_child_map=gsm_child_map)
            cmbulk_paths = audit_core.generate_cmbulk_scripts(
                self._results, script_dir, ctx["site"], ctx["xlsx"],
                generated_by=by, statuses=("Mismatch",), fdn_prefix=fdn_prefix,
                gsm_fdn_prefix=gsm_fdn_prefix, gsm_child_map=gsm_child_map)
            allp = paths + cmedit_paths + cmbulk_paths
            self._log(f"✓ Generated {len(allp)} script(s) in {script_dir} "
                      f"(.mos runnable; cmedit/cmbulk are separate files):")
            for p in allp:
                self._log("   " + os.path.basename(p))
            self._set_status(
                f"Generated {len(paths)} .mos + {len(cmedit_paths)} cmedit + "
                f"{len(cmbulk_paths)} cmbulk.", SUCCESS)
            self._script_dir = script_dir
            self.runscript_btn.visible = True
            self.page.update()
            try:
                os.startfile(script_dir)   # open the folder (Windows) to edit
            except Exception:
                pass
        except Exception as exc:
            self._log(f"✗ Script generation failed: {exc}")
            self._set_status("Script generation failed.", DANGER)

    # ── Apply mismatches via cmedit (close the loop) ─────────────
    def _apply_cmedit(self, e):
        if not self._results:
            self._log("Run an audit first."); return
        from audit import audit_core, cdd_reader
        fdn_prefix = (self._form.get("subnetwork") or "").strip()
        gsm_fdn_prefix, gsm_child_map = "", {}
        try:
            amap = cdd_reader.load_map()
            bsc = (self._form.get("bsc_name") or "").strip() or "<BSC>"
            gsm_fdn_prefix = amap.get("gsm_fdn_prefix", "").replace("{bsc}", bsc)
            gsm_child_map = audit_core.build_gsm_child_map(amap)
        except Exception:
            pass
        site = (self._gen_ctx or {}).get("site") or self.site_field.value.strip()
        cmds = audit_core.build_cmedit_commands(
            self._results, site, fdn_prefix=fdn_prefix,
            gsm_fdn_prefix=gsm_fdn_prefix, gsm_child_map=gsm_child_map)
        if not cmds:
            self._set_status("No mismatch commands to apply.", ACCENT_WARM); return
        if not (self._form.get("host") and self._form.get("username")
                and self._form.get("password")):
            self._set_status("SSH creds from the main form are required to "
                             "apply.", DANGER); return

        dry = ft.Checkbox(label="Dry run — log the commands, don't send",
                          value=True)
        preview = "\n".join(c for _n, c in cmds[:250])
        if len(cmds) > 250:
            preview += f"\n… (+{len(cmds) - 250} more)"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Apply {len(cmds)} cmedit set command(s)?",
                          weight=ft.FontWeight.BOLD, color=ACCENT_WARM),
            content=ft.Container(width=860, content=ft.Column([
                ft.Text("Each runs via cli.py over SSH and CHANGES the node. "
                        "Review carefully — leave 'Dry run' on for a safe "
                        "preview first.", color=TEXT_MUTED, size=12),
                dry,
                ft.Container(
                    height=360, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
                    border_radius=8, padding=10,
                    content=ft.Column([ft.Text(
                        preview, size=11, selectable=True,
                        font_family="Consolas", color=TEXT)],
                        scroll=ft.ScrollMode.AUTO)),
            ], tight=True, spacing=10)),
            actions=[
                ft.TextButton("Cancel",
                              on_click=lambda e2: self._close_dialog(dlg)),
                ft.ElevatedButton(
                    "Apply", icon=ft.Icons.BOLT,
                    style=primary_button_style(),
                    on_click=lambda e2: (self._close_dialog(dlg),
                                         threading.Thread(
                        target=self._apply_cmedit_worker,
                        args=(cmds, dry.value), daemon=True).start())),
            ],
        )
        self._show_dialog(dlg)

    def _apply_cmedit_worker(self, cmds, dry):
        from integration_runner import IntegrationSSH, CLI_PY
        ssh = None
        ok = fail = 0
        self.run_btn.disabled = True
        self._start_timer()
        try:
            host = self._form.get("host", "")
            user = self._form.get("username", "")
            pwd = self._form.get("password", "")
            try:
                port = int(self._form.get("port", 5023) or 5023)
            except (ValueError, TypeError):
                port = 5023
            if not dry:
                ssh = IntegrationSSH(host=host, port=port, username=user,
                                     password=pwd, log_callback=self._log)
                ssh.connect(timeout=30)
            self._log(f"{'DRY RUN — ' if dry else ''}Applying "
                      f"{len(cmds)} cmedit command(s)...")
            _ERR = re.compile(r"\berror\b|exception|invalid|not found|"
                              r"no instances|fail(ed|ure)?", re.IGNORECASE)
            for i, (node, cmd) in enumerate(cmds, 1):
                if dry:
                    self._log(f"  [{i}/{len(cmds)}] (dry) {cmd}")
                    continue
                out = ssh.run_command(f'python {CLI_PY} "{cmd}"', timeout=120)
                if _ERR.search(out or ""):
                    fail += 1
                    self._log(f"  ✗ [{i}/{len(cmds)}] {cmd}\n"
                              f"     → {(out or '').strip()[-200:]}")
                else:
                    ok += 1
                    self._log(f"  ✓ [{i}/{len(cmds)}] {cmd}")
            if dry:
                self._set_status(f"Dry run: {len(cmds)} command(s) shown "
                                 "(nothing sent).", ACCENT)
            else:
                self._set_status(f"Applied: {ok} ok, {fail} failed of "
                                 f"{len(cmds)}.",
                                 SUCCESS if fail == 0 else ACCENT_WARM)
        except Exception as exc:
            self._fail(f"Apply failed: {exc}")
        finally:
            self._stop_timer()
            self.run_btn.disabled = False
            if ssh is not None:
                try:
                    ssh.disconnect()
                except Exception:
                    pass

    # ── Dialog helpers (Flet 0.84 managed dialog stack) ──────────
    def _show_dialog(self, dlg):
        if hasattr(self.page, "show_dialog"):
            try:
                self.page.show_dialog(dlg)
                return
            except Exception:
                pass
        try:
            self.page.overlay.append(dlg)
            dlg.open = True
            self.page.update()
        except Exception:
            pass

    def _close_dialog(self, dlg):
        if hasattr(self.page, "pop_dialog"):
            try:
                self.page.pop_dialog()
                return
            except Exception:
                pass
        try:
            dlg.open = False
            if dlg in (self.page.overlay or []):
                self.page.overlay.remove(dlg)
            self.page.update()
        except Exception:
            pass

    # ── #5: Audit-map editor — friendly table (saves as JSON) ────
    def _mini_tf(self, label, value, width, read_only=False):
        return ft.TextField(
            label=label, value=value, width=width, dense=True, read_only=read_only,
            filled=True, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER, focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED, size=11),
            text_style=ft.TextStyle(color=TEXT, size=12))

    def _open_map_editor(self, e):
        from audit import cdd_reader
        path = cdd_reader.resolve_map_path()
        self._map_path = path
        try:
            self._map_data = json.loads(open(path, encoding="utf-8").read())
        except Exception as ex:
            self._set_status(f"Map load failed: {ex}", DANGER)
            return
        profiles = self._map_data.get("profiles", [])
        if not profiles:
            self._set_status("Map has no profiles.", ACCENT_WARM)
            return

        self._map_prof_rows = {}   # profile index -> list of row states
        containers = {}            # profile index -> ft.Column of rows

        def make_row(i, col=None):
            col = col or {}
            is_split = "split" in col
            cdd = self._mini_tf("CDD column", col.get("cdd", ""), 230)
            if is_split:
                lbl = "split → " + "/".join(
                    s.get("attr", "") for s in col.get("split", []))
                attr = self._mini_tf("MO attribute", lbl, 210, read_only=True)
            else:
                attr = self._mini_tf("MO attribute", col.get("attr", ""), 210)
            via = self._mini_tf("via_ref (optional)", col.get("via_ref", ""), 160)
            norm = self._mini_tf("norm (optional)", col.get("norm", ""), 120)
            st = {"orig": col, "cdd": cdd, "attr": attr, "via": via,
                  "norm": norm, "split": is_split, "prof": i}
            st["row"] = ft.Row(
                [cdd, attr, via, norm,
                 ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_color=DANGER,
                               tooltip="Remove parameter",
                               on_click=lambda ev, s=st: remove_row(s))],
                spacing=8, wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER)
            return st

        def render_prof(i):
            containers[i].controls = [s["row"] for s in self._map_prof_rows[i]]
            try:
                self.page.update()
            except Exception:
                pass

        def add_row(i):
            self._map_prof_rows[i].append(make_row(i))
            render_prof(i)

        def remove_row(st):
            i = st["prof"]
            self._map_prof_rows[i] = [s for s in self._map_prof_rows[i]
                                      if s is not st]
            render_prof(i)

        def collect(i):
            cols = []
            for s in self._map_prof_rows[i]:
                cdd = s["cdd"].value.strip()
                if not cdd:
                    continue
                nc = dict(s["orig"]) if s["orig"] else {}
                nc["cdd"] = cdd
                if not s["split"]:
                    attr = s["attr"].value.strip()
                    if not attr:
                        continue
                    nc["attr"] = attr
                v, n = s["via"].value.strip(), s["norm"].value.strip()
                if v:
                    nc["via_ref"] = v
                elif "via_ref" in nc:
                    del nc["via_ref"]
                if n:
                    nc["norm"] = n
                elif "norm" in nc:
                    del nc["norm"]
                cols.append(nc)
            profiles[i]["columns"] = cols

        def save(ev):
            for i in range(len(profiles)):
                collect(i)
            try:
                txt = json.dumps(self._map_data, indent=2, ensure_ascii=False)
                json.loads(txt)
                with open(self._map_path, "w", encoding="utf-8") as f:
                    f.write(txt)
                self._log(f"✓ Saved audit map: {self._map_path}")
                self._set_status("Audit map saved (used on next Run).", SUCCESS)
                self._close_dialog(dlg)
            except Exception as ex:
                self._set_status(f"Save failed: {ex}", DANGER)

        def open_raw(ev):
            self._close_dialog(dlg)
            self._open_map_editor_raw()

        # Build a section per profile — all shown at once.
        sections = []
        for i, p in enumerate(profiles):
            self._map_prof_rows[i] = [make_row(i, c)
                                      for c in p.get("columns", [])]
            containers[i] = ft.Column(
                [s["row"] for s in self._map_prof_rows[i]], spacing=6)
            header = ft.Row([
                ft.Text(p.get("name", "?"), weight=ft.FontWeight.BOLD,
                        color=ACCENT, size=13),
                ft.Text(f"[{p.get('tech','')}/{p.get('category','')}] "
                        f"sheet={p.get('sheet','')}", size=10, color=TEXT_MUTED),
                ft.Container(expand=True),
                ft.TextButton("+ Add parameter",
                              on_click=lambda ev, i=i: add_row(i)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER)
            sections.append(ft.Container(
                content=ft.Column([header, containers[i]], spacing=6),
                padding=10, border_radius=10,
                bgcolor=ft.Colors.with_opacity(0.15, BG_BOTTOM)))
        body_col = ft.Column(sections, spacing=14, scroll=ft.ScrollMode.AUTO,
                             height=520)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Audit Map — all profiles ({len(profiles)})"),
            content=ft.Container(width=1000, height=560, content=ft.Column([
                ft.Text("All profiles and their parameters (CDD column ↔ MO "
                        "attribute). Edit inline, Add/remove rows. Saved as "
                        "audit_map.json; applies on the next Run.",
                        size=11, color=TEXT_MUTED),
                body_col,
            ], spacing=10, tight=True)),
            actions=[
                ft.ElevatedButton("Save", icon=ft.Icons.SAVE,
                                  style=primary_button_style(), on_click=save),
                ft.TextButton("Raw JSON", on_click=open_raw),
                ft.TextButton("Close",
                              on_click=lambda ev: self._close_dialog(dlg)),
            ],
        )
        self._show_dialog(dlg)

    def _open_map_editor_raw(self):
        from audit import cdd_reader
        path = cdd_reader.resolve_map_path()
        self._map_path = path
        try:
            content = open(path, encoding="utf-8").read()
        except Exception:
            content = "{}"
        editor = ft.TextField(
            value=content, multiline=True, min_lines=24, max_lines=24,
            text_style=ft.TextStyle(font_family="Consolas", size=12, color=TEXT),
            filled=True, bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER, focused_border_color=ACCENT)
        status = ft.Text("", size=12)
        save_btn = ft.ElevatedButton("Save", icon=ft.Icons.SAVE,
                                     style=primary_button_style())

        def validate(_=None):
            try:
                json.loads(editor.value)
                status.value, status.color = "✓ Valid JSON", SUCCESS
                save_btn.disabled = False
            except json.JSONDecodeError as ex:
                status.value = f"✗ line {ex.lineno}, col {ex.colno}: {ex.msg}"
                status.color = DANGER
                save_btn.disabled = True
            self.page.update()

        def save(_):
            try:
                json.loads(editor.value)
                with open(self._map_path, "w", encoding="utf-8") as f:
                    f.write(editor.value)
                self._log(f"✓ Saved audit map: {self._map_path}")
                self._set_status("Audit map saved (used on next Run).", SUCCESS)
                self._close_dialog(dlg)
            except Exception as ex:
                status.value, status.color = f"✗ {ex}", DANGER
                self.page.update()

        editor.on_change = validate
        save_btn.on_click = save
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Audit Map — raw JSON ({os.path.basename(path)})"),
            content=ft.Container(width=920, height=560, content=ft.Column([
                ft.Text("Advanced: full JSON (profiles, cell_expand, split, …). "
                        "Save is blocked while JSON is invalid.",
                        size=11, color=TEXT_MUTED),
                editor, status,
            ], spacing=8, tight=True)),
            actions=[save_btn,
                     ft.TextButton("Close",
                                   on_click=lambda e: self._close_dialog(dlg))],
        )
        validate()
        self._show_dialog(dlg)

    # ── #3: Run generated scripts (review/edit first, then run) ──
    def _open_run_scripts(self, e):
        sd = getattr(self, "_script_dir", "")
        files = sorted(glob.glob(os.path.join(sd, "*.mos"))) if sd else []
        if not files:
            self._set_status("No scripts found — Generate first.", ACCENT_WARM)
            return
        checks = [ft.Checkbox(label=os.path.basename(p), value=True)
                  for p in files]
        rows = ft.Column(checks, spacing=2, scroll=ft.ScrollMode.AUTO,
                         height=min(260, 40 + 24 * len(files)))
        parallel_cb = ft.Checkbox(
            label="Run in parallel via mobatch (-p up to 30) — recommended "
                  "for many nodes", value=len(files) > 1)

        def do_run(_):
            selected = [files[i] for i, c in enumerate(checks) if c.value]
            if not selected:
                return
            parallel = bool(parallel_cb.value)
            self._close_dialog(dlg)
            threading.Thread(target=self._run_scripts_worker,
                             args=(selected, parallel), daemon=True).start()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Run Scripts on live node(s)"),
            content=ft.Container(width=640, content=ft.Column([
                ft.Text("⚠ This SETS parameters on the LIVE node(s). Review/edit "
                        "the .mos files first (Generate opened the folder).",
                        size=12, color=ACCENT_WARM),
                ft.Text("Parallel = one mobatch job (light on the app, runs on "
                        "the gateway). Sequential = amos, one node at a time.",
                        size=11, color=TEXT_MUTED),
                parallel_cb,
                ft.Text("Select scripts to run:", size=12, color=TEXT_MUTED),
                rows,
            ], spacing=10, tight=True)),
            actions=[
                ft.ElevatedButton("Open folder", icon=ft.Icons.FOLDER_OPEN,
                                  on_click=lambda e: os.startfile(sd)),
                ft.ElevatedButton("Confirm & Run", icon=ft.Icons.PLAY_ARROW,
                                  style=primary_button_style(), on_click=do_run),
                ft.TextButton("Cancel", on_click=lambda e: self._close_dialog(dlg)),
            ],
        )
        self._show_dialog(dlg)

    @staticmethod
    def _node_and_stamp(basename):
        m = re.search(r"^(.*)_SetParameter_(\d{8}_\d{6})\.mos$", basename)
        if m:
            return m.group(1), m.group(2)
        return re.sub(r"\.mos$", "", basename), ""

    def _run_scripts_worker(self, files, parallel=False):
        ssh = None
        self._start_timer()
        try:
            from integration_runner import (IntegrationSSH, run_moshell_script,
                                            run_mobatch_scripts,
                                            download_remote_dir)
            host = self._form.get("host", "")
            user = self._form.get("username", "")
            pwd = self._form.get("password", "")
            if not (host and user and pwd):
                self._fail("Run Scripts needs SSH credentials from the main form.")
                return
            try:
                port = int(self._form.get("port", 5023) or 5023)
            except (ValueError, TypeError):
                port = 5023
            label = (self._gen_ctx or {}).get("site", "SCRIPTS")
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)
            self._log(f"SSH → {host}:{port} as {user} to run {len(files)} script(s)"
                      f"{' via mobatch (parallel)' if parallel else ' (sequential)'}...")
            ssh = IntegrationSSH(host=host, port=port, username=user,
                                 password=pwd, log_callback=self._log)
            ssh.connect(timeout=30)

            if parallel and len(files) > 1:
                node_files = []
                stamp = ""
                for p in files:
                    n, s = self._node_and_stamp(os.path.basename(p))
                    node_files.append((n, p))
                    stamp = stamp or s
                out, logdir = run_mobatch_scripts(
                    ssh, node_files, stamp, safe, self._log, parallel_cap=30)
                for line in out.splitlines()[-60:]:
                    self._log("   " + line)
                self._log(f"✓ mobatch done. Per-node logs on gateway: {logdir}")

                # Download the mobatch logs into one local folder, then parse
                # them (same engine as the relation logs) → a result Excel.
                out_dir = (self._gen_ctx or {}).get("out_dir", "")
                local_logs_dir = os.path.join(out_dir, "MOBATCH_LOGS", stamp or safe)
                self._log(f"Downloading mobatch logs → {local_logs_dir} ...")
                local_files = download_remote_dir(ssh, logdir, local_logs_dir,
                                                  self._log)
                logs = [p for p in local_files if p.lower().endswith(".log")]
                if logs:
                    try:
                        from relation_log_parser import build_relation_log_excel
                        xlsx = build_relation_log_excel(logs, local_logs_dir,
                                                        safe, self._log)
                        self._result_path = xlsx
                        self.open_btn.visible = True
                        self._log(f"✓ Parsed {len(logs)} log(s) → {xlsx}")
                        self._set_status(
                            f"mobatch done, {len(logs)} log(s) parsed — "
                            "Open Report.", SUCCESS)
                    except Exception as ex:
                        self._log(f"✗ Log parse failed: {ex}")
                        self._set_status("mobatch ran; log parse failed.",
                                         ACCENT_WARM)
                else:
                    self._log("⚠ No .log files downloaded to parse.")
                    self._set_status(f"mobatch ran {len(files)} node(s); "
                                     "no logs found.", ACCENT_WARM)
            else:
                ok = 0
                for path in files:
                    node, _ = self._node_and_stamp(os.path.basename(path))
                    self._log(f"── Running {os.path.basename(path)} on {node} ──")
                    try:
                        out = run_moshell_script(ssh, node, path, self._log)
                        for line in out.splitlines()[-40:]:
                            self._log("   " + line)
                        ok += 1
                    except Exception as ex:
                        self._log(f"   ✗ failed: {ex}")
                self._set_status(f"Ran {ok}/{len(files)} script(s) — check log.",
                                 SUCCESS if ok == len(files) else ACCENT_WARM)
        except Exception as exc:
            self._fail(f"Run scripts failed: {exc}")
        finally:
            self._stop_timer()
            if ssh is not None:
                try:
                    ssh.disconnect()
                except Exception:
                    pass
            try:
                self.page.update()
            except Exception:
                pass
