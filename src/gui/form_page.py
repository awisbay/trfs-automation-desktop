"""
NodeCraft GUI - Elevated configuration workspace.
"""
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from command_parser import parse_commands_file
from license_manager import get_license_info


def _get_license_user() -> str:
    """Best-effort lookup of the licensed user's name; falls back to 'User'."""
    try:
        info = get_license_info() or {}
        return str(info.get("user") or info.get("name") or "User").strip() or "User"
    except Exception:
        return "User"


def _app_version() -> str:
    """App version from the single source of truth (src/version.py)."""
    try:
        from version import __version__
        return __version__
    except Exception:
        return "?"
from config_loader import build_config_from_form, load_config
from session import load_session, save_session
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
    primary_button_style,
    secondary_button_style,
)


class FormPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self._try_load_defaults()

    def _try_load_defaults(self):
        self.defaults = {
            "shortcode": "",
            "node_name": "",
            "node_ip": "",
            "subnetwork": "",
            "node2_name": "",
            "node2_ip": "",
            "node2_subnetwork": "",
            "gsm_node_name": "",
            "bsc_name": "",
            "gsm_node_ip": "",
            "gsm_subnetwork": "",
            "host": "",
            "port": 5023,
            "username": "",
            "password": "",
            "commands_file": "",
            "config_path": "",
        }
        # form_page.py lives at src/gui/ — config.yaml is normally at the
        # repo root. Walk upward from this file so we find it regardless of
        # how the app was launched (CWD, frozen bundle, etc.).
        import logging as _logging
        from app_path import get_app_dir
        _log = _logging.getLogger(__name__)
        config_path = ""
        # First check the app root (exe directory when frozen)
        app_root = get_app_dir()
        candidate = os.path.join(app_root, "config.yaml")
        if os.path.isfile(candidate):
            config_path = candidate
        else:
            # Fallback: walk upward from this source file
            here = os.path.dirname(os.path.abspath(__file__))
            for up in range(4):
                candidate = os.path.normpath(
                    os.path.join(here, *([".."] * up), "config.yaml")
                )
                if os.path.isfile(candidate):
                    config_path = candidate
                    break
        _log.info("[form] __file__=%s", os.path.abspath(__file__))
        _log.info("[form] resolved config_path=%r (exists=%s)",
                  config_path, bool(config_path))

        # Check if this is a returning user (session file exists)
        from session import _SESSION_FILE
        _has_session = os.path.isfile(_SESSION_FILE)

        if config_path:
            # Always remember the path so downstream build_config_from_form
            # loads enm/moshell/paths defaults — even if prefill below fails.
            self.defaults["config_path"] = config_path
            if _has_session:
                try:
                    config = load_config(config_path)
                    self.defaults["shortcode"] = config.site.shortcode
                    self.defaults["node_name"] = config.site.node_name
                    self.defaults["gsm_node_name"] = getattr(config.site, "gsm_node_name", "") or ""
                    self.defaults["bsc_name"] = getattr(config.site, "bsc_name", "") or ""
                    self.defaults["host"] = config.ssh.host
                    self.defaults["port"] = config.ssh.port
                    self.defaults["username"] = config.ssh.username
                    self.defaults["password"] = config.ssh.password
                    self.defaults["commands_file"] = os.path.join(config.base_dir, config.paths.commands)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).exception(
                        "Failed to prefill form defaults from config.yaml: %s", exc
                    )

        # Session overrides config.yaml defaults for every persisted field,
        # so whatever the user last typed comes back on re-open.
        session = load_session()
        for k in (
            "host", "port", "username", "password",
            "shortcode",
            "node_name", "node_ip", "subnetwork",
            "node2_name", "node2_ip", "node2_subnetwork",
            "gsm_node_name", "bsc_name", "gsm_node_ip", "gsm_subnetwork",
            "commands_file", "lkf_file", "relation_file",
        ):
            v = session.get(k)
            if v:
                self.defaults[k] = v
        # Ensure keys exist even if not in _try_load_defaults' initial dict
        self.defaults.setdefault("lkf_file", "")
        self.defaults.setdefault("relation_file", "")

    def build(self) -> ft.View:
        # Reset the OS window title when returning to the form (clears
        # any per-shortcode title set during integration/TRFS).
        try:
            _sc = self.defaults.get("shortcode", "")
            self.page.title = (
                f"NodeCraft — {_sc}" if _sc else "NodeCraft"
            )
            self.page.update()
        except Exception:
            pass

        self.shortcode_field = self._text_field(
            "Site ID ex. MIN3117",
            self.defaults["shortcode"],
            expand=1,
            autofocus=True,
        )
        # --- Node 1: LTE/NR ---
        self.node_name_field = self._text_field(
            "Node Name (LTE/NR)",
            self.defaults["node_name"],
            expand=2,
        )
        self.node_ip_field = self._text_field(
            "Node IP",
            self.defaults["node_ip"],
            expand=1,
        )
        self.subnetwork_field = self._text_field(
            "Subnetwork",
            self.defaults["subnetwork"],
            expand=1,
        )
        # --- Node 2: LTE/NR #2 ---
        self.node2_name_field = self._text_field(
            "Node Name (LTE/NR #2)",
            self.defaults["node2_name"],
            expand=2,
        )
        self.node2_ip_field = self._text_field(
            "Node IP (#2)",
            self.defaults["node2_ip"],
            expand=1,
        )
        self.node2_subnetwork_field = self._text_field(
            "Subnetwork (#2)",
            self.defaults["node2_subnetwork"],
            expand=1,
        )
        # --- Node 3: GSM ---
        self.gsm_node_name_field = self._text_field(
            "Node Name (GSM)",
            self.defaults["gsm_node_name"],
            expand=2,
        )
        self.bsc_name_field = self._text_field(
            "BSC Name",
            self.defaults["bsc_name"],
            expand=1,
        )
        self.gsm_node_ip_field = self._text_field(
            "Node IP (GSM)",
            self.defaults["gsm_node_ip"],
            expand=1,
        )
        self.gsm_subnetwork_field = self._text_field(
            "Subnetwork (GSM)",
            self.defaults["gsm_subnetwork"],
            expand=1,
        )
        self.host_field = self._text_field("ENM IP", self.defaults["host"], expand=2)
        self.port_field = self._text_field("Port", str(self.defaults["port"]), expand=1)
        self.username_field = self._text_field("Username", self.defaults["username"], expand=1)
        self.password_field = self._text_field(
            "Password",
            self.defaults["password"],
            expand=1,
            password=True,
            can_reveal_password=True,
        )
        self.commands_field = self._text_field(
            "Commands File",
            self.defaults["commands_file"],
            expand=True,
            on_change=lambda _: self._validate_commands(),
        )
        self.file_picker = ft.FilePicker()
        self.browse_button = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for commands file",
            on_click=self._on_browse,
        )

        # --- Integration files ---
        self.lkf_field = self._text_field(
            "LKF File(s)",
            self.defaults.get("lkf_file", ""),
            expand=True,
        )
        self.lkf_browse = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for LKF file(s)",
            on_click=self._on_browse_lkf,
        )
        self.relation_field = self._text_field(
            "Relation File(s)",
            self.defaults.get("relation_file", ""),
            expand=True,
        )
        self.relation_browse = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for Relation file(s)",
            on_click=self._on_browse_relation,
        )

        self.error_text = ft.Text("", color="#FFB4B4", visible=False, size=12)
        self.error_box = ft.Container(
            content=self.error_text,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            bgcolor=ft.Colors.with_opacity(0.1, "#FF6B6B"),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25, "#FF6B6B")),
            border_radius=14,
            visible=False,
        )
        self.parse_info = ft.Text("", color=SUCCESS, visible=False, size=12)
        self.band_count_text = ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.category_count_text = ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.commands_name_text = ft.Text(
            "No command map loaded",
            size=12,
            color=TEXT_MUTED,
        )

        if self.commands_field.value.strip():
            self._validate_commands()

        # --- Relation zip browse in hero panel ---
        self.relation_zip_field = self._text_field(
            "Relation Zip File",
            "",
            expand=True,
        )
        self.relation_zip_browse = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for Relation ZIP file",
            on_click=self._on_browse_relation_zip,
        )
        self.relation_zip_status = ft.Text("", size=12, color=TEXT_MUTED, visible=False)

        hero = panel(
            ft.Column(
                [
                    badge("Desktop Workflow", ACCENT, ft.Icons.DESKTOP_WINDOWS),
                    ft.Text(
                        "NodeCraft",
                        size=42,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT,
                    ),
                    ft.Text(
                        f"v{_app_version()} — by ewisbay",
                        size=14,
                        color=ACCENT,
                        weight=ft.FontWeight.W_500,
                    ),
                    ft.Text(
                        f"Welcome, {_get_license_user()}",
                        size=15,
                        color=TEXT,
                        weight=ft.FontWeight.W_600,
                    ),
                    ft.Text(
                        "Control room for SSH execution, ENM capture, Excel reports, and Integration testing.",
                        size=15,
                        color=TEXT_MUTED,
                    ),
                    ft.Container(height=10),
                    panel(
                        ft.Column(
                            [
                                ft.Text(
                                    "Relation ZIP → XML Converter",
                                    size=13,
                                    color=TEXT,
                                    weight=ft.FontWeight.W_600,
                                ),
                                ft.Text(
                                    "Browse a relation ZIP file to auto-convert to XML format.",
                                    size=11,
                                    color=TEXT_MUTED,
                                ),
                                ft.Row(
                                    [self.relation_zip_field, self.relation_zip_browse],
                                    spacing=8,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                self.relation_zip_status,
                            ],
                            spacing=8,
                        ),
                        bgcolor=PANEL_RAISED,
                        padding=18,
                        border_color="#3A5F7C",
                    ),
                    ft.Container(height=6),
                    panel(
                        ft.Column(
                            [
                                ft.Text(
                                    "Command profile",
                                    size=12,
                                    color=TEXT_MUTED,
                                    weight=ft.FontWeight.W_600,
                                ),
                                self.commands_name_text,
                                self.parse_info,
                            ],
                            spacing=8,
                        ),
                        bgcolor=PANEL_RAISED,
                        padding=18,
                        border_color="#3A5F7C",
                    ),
                    ft.Container(expand=True),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.COPYRIGHT, size=12, color=TEXT_MUTED),
                            ft.Text(
                                "Crafted by ewisbay",
                                size=11,
                                color=TEXT_MUTED,
                                weight=ft.FontWeight.W_500,
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=18,
            ),
            bgcolor=PANEL,
            expand=1,
            padding=28,
        )

        form = panel(
            ft.Column(
                [
                    ft.Text("Run Configuration", size=26, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Control room for SSH execution, ENM capture, Excel reports, and Integration testing.",
                        size=14,
                        color=TEXT_MUTED,
                    ),
                    self._section_label("Site Identity"),
                    ft.Row(
                        [self.shortcode_field, ft.Container(width=16)],
                        spacing=14,
                    ),
                    self._section_label("Node 1 — LTE / NR"),
                    ft.Row(
                        [self.node_name_field, self.node_ip_field, self.subnetwork_field, ft.Container(width=16)],
                        spacing=14,
                    ),
                    self._section_label("Node 2 — LTE / NR #2"),
                    ft.Row(
                        [self.node2_name_field, self.node2_ip_field, self.node2_subnetwork_field, ft.Container(width=16)],
                        spacing=14,
                    ),
                    self._section_label("Node 3 — GSM"),
                    ft.Row(
                        [self.gsm_node_name_field, self.bsc_name_field, self.gsm_node_ip_field, self.gsm_subnetwork_field, ft.Container(width=16)],
                        spacing=14,
                    ),
                    self._section_label("Access"),
                    ft.Row(
                        [self.host_field, self.port_field, self.username_field, self.password_field, ft.Container(width=16)],
                        spacing=14,
                    ),
                    self._section_label("TRFS Script"),
                    ft.Row(
                        [self.commands_field, self.browse_button, ft.Container(width=16)],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "Enter the full path to your commands file, for example: C:\\dev\\TRFS\\TRFS commands.txt",
                        size=11,
                        color=TEXT_MUTED,
                    ),
                    self._section_label("Integration Files"),
                    ft.Row(
                        [self.lkf_field, self.lkf_browse, ft.Container(width=16)],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [self.relation_field, self.relation_browse, ft.Container(width=16)],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.error_box,
                    ft.Container(height=12),
                    ft.Row(
                        [
                            ft.ElevatedButton(
                                "Integration Launch",
                                icon=ft.Icons.INTEGRATION_INSTRUCTIONS,
                                style=ft.ButtonStyle(
                                    bgcolor=ACCENT_WARM,
                                    color="#06242A",
                                    mouse_cursor=ft.MouseCursor.CLICK,
                                    padding=ft.Padding.symmetric(horizontal=20, vertical=18),
                                    shape=ft.RoundedRectangleBorder(radius=16),
                                ),
                                on_click=self._on_integration,
                            ),
                            ft.Container(width=12),
                            ft.OutlinedButton(
                                "Terminal",
                                icon=ft.Icons.TERMINAL,
                                tooltip="Open interactive SSH terminal (multi-tab)",
                                style=ft.ButtonStyle(
                                    color=ACCENT,
                                    side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT)),
                                    mouse_cursor=ft.MouseCursor.CLICK,
                                    padding=ft.Padding.symmetric(horizontal=18, vertical=18),
                                    shape=ft.RoundedRectangleBorder(radius=16),
                                ),
                                on_click=self._on_terminal,
                            ),
                            ft.Container(width=12),
                            ft.OutlinedButton(
                                "CDD Audit",
                                icon=ft.Icons.FACT_CHECK,
                                tooltip="Compare node dump (modump/cmdump) against CDD",
                                style=ft.ButtonStyle(
                                    color=ACCENT_WARM,
                                    side=ft.BorderSide(1, ft.Colors.with_opacity(0.6, ACCENT_WARM)),
                                    mouse_cursor=ft.MouseCursor.CLICK,
                                    padding=ft.Padding.symmetric(horizontal=18, vertical=18),
                                    shape=ft.RoundedRectangleBorder(radius=16),
                                ),
                                on_click=self._on_audit,
                            ),
                            ft.Container(width=12),
                            ft.OutlinedButton(
                                "Clear Data",
                                icon=ft.Icons.CLEANING_SERVICES_OUTLINED,
                                icon_color="#FF8A8A",
                                tooltip="Clear all site fields (SSH credentials are kept)",
                                style=ft.ButtonStyle(
                                    color="#FF8A8A",
                                    bgcolor=ft.Colors.with_opacity(0.08, "#FF6B6B"),
                                    mouse_cursor=ft.MouseCursor.CLICK,
                                    side=ft.BorderSide(1, ft.Colors.with_opacity(0.55, "#FF6B6B")),
                                    padding=ft.Padding.symmetric(horizontal=18, vertical=18),
                                    shape=ft.RoundedRectangleBorder(radius=16),
                                ),
                                on_click=self._on_clear_data,
                            ),
                            ft.Container(expand=True),
                            # TRFS Launch — disabled in this build to
                            # shrink the distribution (no Playwright /
                            # Chromium dependency needed). The code path
                            # (``_on_start`` and the entire Progress /
                            # Result / Capture pipeline) is intentionally
                            # kept intact so we can re-enable in a future
                            # build by removing ``disabled=True`` and
                            # putting playwright back in requirements.
                            ft.ElevatedButton(
                                "TRFS Launch",
                                icon=ft.Icons.PLAY_CIRCLE_FILL_ROUNDED,
                                disabled=True,
                                tooltip=(
                                    "TRFS Launch is disabled in this build. "
                                    "Re-enable by removing 'disabled=True' "
                                    "in form_page.py and re-installing "
                                    "Playwright + Chromium."
                                ),
                                style=ft.ButtonStyle(
                                    bgcolor=ACCENT,
                                    color="#06242A",
                                    mouse_cursor=ft.MouseCursor.CLICK,
                                    padding=ft.Padding.symmetric(horizontal=20, vertical=18),
                                    shape=ft.RoundedRectangleBorder(radius=16),
                                ),
                                on_click=self._on_start,
                            ),
                            ft.Container(width=16),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=16,
                scroll=ft.ScrollMode.AUTO,
            ),
            bgcolor="#0F2132",
            padding=ft.Padding(left=28, top=28, right=4, bottom=28),
            expand=2,
        )

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            padding=ft.Padding.symmetric(horizontal=28, vertical=26),
            content=ft.Row(
                [hero, form],
                spacing=20,
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

        return ft.View(route="/form", padding=0, spacing=0, bgcolor=BG_TOP, controls=[body], services=[self.file_picker])

    def _text_field(self, label: str, value: str, expand=None, width=None, **kwargs) -> ft.TextField:
        return ft.TextField(
            label=label,
            value=value,
            width=width,
            expand=expand,
            dense=False,
            border_radius=16,
            filled=True,
            bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER,
            focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED),
            text_style=ft.TextStyle(color=TEXT, size=14),
            cursor_color=ACCENT,
            **kwargs,
        )

    def _metric_card(self, label: str, value_text: ft.Text, accent: str) -> ft.Container:
        return ft.Container(
            width=190,
            padding=18,
            bgcolor=ft.Colors.with_opacity(0.18, accent),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.3, accent)),
            border_radius=20,
            content=ft.Column(
                [
                    ft.Text(label, size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                    value_text,
                ],
                spacing=8,
            ),
        )

    def _section_label(self, text: str) -> ft.Text:
        return ft.Text(
            text.upper(),
            size=11,
            color=ACCENT_WARM,
            weight=ft.FontWeight.BOLD,
        )


    async def _on_browse(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select Commands File",
            allowed_extensions=["txt"],
            file_type=ft.FilePickerFileType.CUSTOM,
            allow_multiple=False,
        )
        if files:
            self.commands_field.value = files[0].path
            self.page.update()
            self._validate_commands()

    async def _on_browse_lkf(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select LKF File(s)",
            allow_multiple=True,
        )
        if not files:
            return
        if len(files) == 1:
            self.lkf_field.value = files[0].path
            self.page.update()
            return
        self.lkf_field.value = "; ".join(f.path for f in files)
        self.page.update()
        self._combine_lkf_files([f.path for f in files])

    def _combine_lkf_files(self, paths: list[str]):
        import io
        import zipfile
        from datetime import datetime
        from app_path import get_app_dir

        tmp_dir = os.path.join(get_app_dir(), "LKF_COMBINED")
        os.makedirs(tmp_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        combined_name = f"LKF_Combined_{ts}.zip"
        combined_path = os.path.join(tmp_dir, combined_name)

        try:
            seen_names = set()
            with zipfile.ZipFile(combined_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
                for p in paths:
                    if not os.path.isfile(p):
                        continue
                    if p.lower().endswith(".zip"):
                        with zipfile.ZipFile(p, "r") as in_zip:
                            for member in in_zip.namelist():
                                if member in seen_names:
                                    continue
                                seen_names.add(member)
                                data = in_zip.read(member)
                                out_zip.writestr(member, data)
                    else:
                        name = os.path.basename(p)
                        if name in seen_names:
                            continue
                        seen_names.add(name)
                        out_zip.write(p, name)

            self.lkf_field.value = combined_path
            self.page.update()
        except Exception as exc:
            self.error_text.value = f"Failed to combine LKF files: {exc}"
            self.error_text.visible = True
            self.error_box.visible = True
            self.page.update()

    async def _on_browse_relation(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select Relation File(s)",
            allow_multiple=True,
        )
        if not files:
            return
        if len(files) == 1:
            self.relation_field.value = files[0].path
            self.page.update()
            return
        self.relation_field.value = "; ".join(f.path for f in files)
        self.page.update()
        self._combine_relation_files([f.path for f in files])

    def _combine_relation_files(self, paths: list[str]):
        import zipfile
        from datetime import datetime
        from app_path import get_app_dir

        tmp_dir = os.path.join(get_app_dir(), "RELATION_COMBINED")
        os.makedirs(tmp_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        combined_name = f"Relation_Combined_{ts}.zip"
        combined_path = os.path.join(tmp_dir, combined_name)

        try:
            seen_names = set()
            with zipfile.ZipFile(combined_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
                for p in paths:
                    if not os.path.isfile(p):
                        continue
                    if p.lower().endswith(".zip"):
                        with zipfile.ZipFile(p, "r") as in_zip:
                            for member in in_zip.namelist():
                                if member in seen_names:
                                    continue
                                seen_names.add(member)
                                data = in_zip.read(member)
                                out_zip.writestr(member, data)
                    else:
                        name = os.path.basename(p)
                        if name in seen_names:
                            continue
                        seen_names.add(name)
                        out_zip.write(p, name)

            self.relation_field.value = combined_path
            self.page.update()
        except Exception as exc:
            self.error_text.value = f"Failed to combine Relation files: {exc}"
            self.error_text.visible = True
            self.error_box.visible = True
            self.page.update()

    async def _on_browse_relation_zip(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select Relation ZIP File",
            allowed_extensions=["zip"],
            file_type=ft.FilePickerFileType.CUSTOM,
            allow_multiple=False,
        )
        if files:
            zip_path = files[0].path
            self.relation_zip_field.value = zip_path
            self.relation_zip_status.value = "Converting..."
            self.relation_zip_status.color = ACCENT
            self.relation_zip_status.visible = True
            self.page.update()

            node_name = self.node_name_field.value.strip()
            if not node_name:
                self.relation_zip_status.value = "⚠ Please fill Node Name (LTE/NR) first."
                self.relation_zip_status.color = "#FFB4B4"
                self.page.update()
                return

            try:
                # Locate template and converter. In a PyInstaller build the
                # bundled resources live in ``_internal/`` next to the exe,
                # whereas in dev they sit at the repo root. Check both.
                src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                repo_root = os.path.dirname(src_dir)
                candidates = [
                    src_dir,                          # _internal/  (PyInstaller one-folder)
                    repo_root,                        # dev repo root
                    os.path.dirname(zip_path),        # next to the uploaded zip
                ]
                # Also honour sys._MEIPASS if present (PyInstaller onefile)
                meipass = getattr(sys, "_MEIPASS", None)
                if meipass:
                    candidates.insert(0, meipass)

                def _find(name: str) -> str | None:
                    for base in candidates:
                        p = os.path.join(base, name)
                        if os.path.isfile(p):
                            return p
                    return None

                template_path = _find("Relation.txt")
                if not template_path:
                    self.relation_zip_status.value = "⚠ Template Relation.txt not found."
                    self.relation_zip_status.color = "#FFB4B4"
                    self.page.update()
                    return

                converter_path = _find("txt_to_xml_converter.py")
                if not converter_path:
                    self.relation_zip_status.value = "⚠ txt_to_xml_converter.py not found."
                    self.relation_zip_status.color = "#FFB4B4"
                    self.page.update()
                    return

                import importlib.util
                spec = importlib.util.spec_from_file_location("txt_to_xml_converter", converter_path)
                converter_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(converter_mod)

                output_name = f"LTE_{node_name}_Relation.xml"

                # Ask user where to save the XML
                save_path = await self.file_picker.save_file(
                    dialog_title="Save Relation XML",
                    file_name=output_name,
                    allowed_extensions=["xml"],
                    file_type=ft.FilePickerFileType.CUSTOM,
                )
                if not save_path:
                    self.relation_zip_status.value = "Cancelled."
                    self.relation_zip_status.color = TEXT_MUTED
                    self.page.update()
                    return

                converter_mod.convert_txt_to_xml(
                    template_path=template_path,
                    zip_path=zip_path,
                    output_path=save_path,
                    site_name=node_name,
                )

                self.relation_field.value = save_path
                self.relation_zip_status.value = f"✓ Saved → {os.path.basename(save_path)}"
                self.relation_zip_status.color = SUCCESS
                self.page.update()

            except Exception as ex:
                self.relation_zip_status.value = f"✗ Conversion failed: {ex}"
                self.relation_zip_status.color = "#FFB4B4"
                self.page.update()

    def _validate_commands(self):
        path = self.commands_field.value.strip()
        if not path or not os.path.isfile(path):
            self.parse_info.visible = False
            self.commands_name_text.value = "No command map loaded"
            self.band_count_text.value = "0"
            self.category_count_text.value = "0"
            try:
                self.page.update()
            except Exception:
                pass
            return
        try:
            all_cmds = parse_commands_file(path)
            band_names = list(all_cmds.keys())
            total_categories = sum(len(cats) for cats in all_cmds.values())
            self.parse_info.value = f"Loaded {len(band_names)} bands: {', '.join(band_names)}"
            self.parse_info.color = SUCCESS
            self.parse_info.visible = True
            self.commands_name_text.value = os.path.basename(path)
            self.band_count_text.value = str(len(band_names))
            self.category_count_text.value = str(total_categories)
        except Exception as ex:
            self.parse_info.value = f"Parser error: {ex}"
            self.parse_info.color = "#FFB4B4"
            self.parse_info.visible = True
            self.commands_name_text.value = os.path.basename(path)
            self.band_count_text.value = "-"
            self.category_count_text.value = "-"
        try:
            self.page.update()
        except Exception:
            pass

    def _collect_form(self):
        """Gather all form values and return as dict."""
        return {
            "shortcode": self.shortcode_field.value.strip(),
            "node_name": self.node_name_field.value.strip(),
            "node_ip": self.node_ip_field.value.strip(),
            "subnetwork": self.subnetwork_field.value.strip(),
            "node2_name": self.node2_name_field.value.strip(),
            "node2_ip": self.node2_ip_field.value.strip(),
            "node2_subnetwork": self.node2_subnetwork_field.value.strip(),
            "gsm_node_name": self.gsm_node_name_field.value.strip(),
            "bsc_name": self.bsc_name_field.value.strip(),
            "gsm_node_ip": self.gsm_node_ip_field.value.strip(),
            "gsm_subnetwork": self.gsm_subnetwork_field.value.strip(),
            "host": self.host_field.value.strip(),
            "port": int(self.port_field.value.strip()) if self.port_field.value.strip() else 5023,
            "username": self.username_field.value.strip(),
            "password": self.password_field.value.strip(),
            "commands_file": self.commands_field.value.strip(),
            "lkf_file": self.lkf_field.value.strip(),
            "relation_file": self.relation_field.value.strip(),
        }

    def _show_errors(self, errors: list[str]):
        self.error_text.value = " ".join(errors)
        self.error_text.visible = True
        self.error_box.visible = True
        self.page.update()

    def _show_alert(self, title: str, message: str):
        """Blocking modal alert (Flet 0.84 dialog stack)."""
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title, color=DANGER, weight=ft.FontWeight.BOLD),
            content=ft.Text(message, size=13, color=TEXT, selectable=True),
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=PANEL,
        )

        def _close(e):
            try:
                self.page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    self.page.update()
                except Exception:
                    pass

        dlg.actions = [ft.TextButton("OK", on_click=_close)]
        try:
            self.page.show_dialog(dlg)
        except Exception:
            try:
                self.page.overlay.append(dlg)
                dlg.open = True
                self.page.update()
            except Exception:
                pass

    def _validate_shortcode_prefix(self, f: dict) -> bool:
        """Site ID and EVERY node name must belong to the same site, so
        each site gets one consistent LOG/<SITE_ID>/ folder.

        A node name matches when it is exactly the Site ID or starts with
        ``<SITE_ID>_``. The underscore boundary matters: without it a Site
        ID of ``MIN3117`` would wrongly accept ``MIN31170_...``, which is a
        DIFFERENT site.

        Returns False (and shows a popup) when any node name doesn't match.
        """
        sc = (f.get("shortcode") or "").strip()
        if not sc:
            return True                       # emptiness handled by caller
        scu = sc.upper()

        def _matches(nn: str) -> bool:
            u = nn.strip().upper()
            return u == scu or u.startswith(scu + "_")

        checks = (
            ("LTE/NR", f.get("node_name", "")),
            ("LTE/NR #2", f.get("node2_name", "")),
            ("GSM", f.get("gsm_node_name", "")),
        )
        bad = [f"  • {lbl}: {nn.strip()}" for lbl, nn in checks
               if nn.strip() and not _matches(nn)]
        if not bad:
            return True
        self._show_alert(
            "Site ID and Node Name do not match",
            f"Site ID '{sc}' must be the prefix of EVERY node name — the "
            "Site ID, Node 1, Node 2 and GSM must all belong to the same "
            "site.\n\nMismatched:\n" + "\n".join(bad) +
            f"\n\nExpected format:  {sc}_SITENAME...\n\n"
            "Please correct the Site ID or the node name(s)."
        )
        return False

    def _on_clear_data(self, e):
        """Clear all site-related fields. SSH credentials (host, port,
        username, password) are intentionally preserved so the operator
        doesn't have to retype them for the next site."""
        site_fields = [
            self.shortcode_field,
            self.node_name_field, self.node_ip_field, self.subnetwork_field,
            self.node2_name_field, self.node2_ip_field, self.node2_subnetwork_field,
            self.gsm_node_name_field, self.bsc_name_field,
            self.gsm_node_ip_field, self.gsm_subnetwork_field,
            # Integration file paths belong to the current site — clearing
            # them prevents accidentally re-using the previous site's files.
            self.lkf_field, self.relation_field, self.relation_zip_field,
        ]
        for fld in site_fields:
            try:
                fld.value = ""
            except Exception:
                pass

        # Hide any lingering validation banner.
        self.error_text.value = ""
        self.error_text.visible = False
        self.error_box.visible = False
        # Reset command-file parse summary (commands_field itself is kept
        # since it's not site-specific).
        try:
            self.parse_info.visible = False
            self.band_count_text.value = "0"
            self.category_count_text.value = "0"
        except Exception:
            pass

        self.page.update()

    def _on_start(self, e):
        f = self._collect_form()

        errors = []
        if not f["shortcode"]:
            errors.append("Site ID is required.")
        if not f["node_name"]:
            errors.append("Node name is required.")
        if not f["host"]:
            errors.append("ENM IP is required.")
        if not f["username"]:
            errors.append("Username is required.")
        if not f["password"]:
            errors.append("Password is required.")
        if not f["commands_file"]:
            errors.append("Commands file is required.")
        elif not os.path.isfile(f["commands_file"]):
            errors.append(f"Commands file not found: {f['commands_file']}")

        if errors:
            self._show_errors(errors)
            return

        # Shortcode must prefix every node name.
        if not self._validate_shortcode_prefix(f):
            return

        self.error_text.visible = False
        self.error_box.visible = False

        self._persist_session(f)

        config = build_config_from_form(
            shortcode=f["shortcode"],
            node_name=f["node_name"],
            host=f["host"],
            port=f["port"],
            username=f["username"],
            password=f["password"],
            commands_file=f["commands_file"],
            config_path=self.defaults.get("config_path") or None,
            gsm_node_name=f["gsm_node_name"],
            bsc_name=f["bsc_name"],
            node2_name=f["node2_name"],
        )

        self.page.trfs_config = config
        self.page.trfs_demo_mode = False
        self.page.trfs_commands_file = f["commands_file"]

        self.page.go("/progress")

    def _on_terminal(self, e):
        # Open the multi-tab SSH terminal. Pre-fills the first tab with
        # the SSH credentials currently in the form (so the operator
        # gets a connected session to the gateway in one click).
        # Site fields aren't required — only host/user/password matter
        # for SSH.
        f = self._collect_form()
        # Hand off only what the terminal cares about — host, port,
        # username, password — under the same key the integration form
        # uses, so the terminal page can read it without knowing which
        # workflow opened it.
        self.page.integration_form = {
            "host": f.get("host", ""),
            "port": f.get("port", 22),
            "username": f.get("username", ""),
            "password": f.get("password", ""),
        }
        self.page.go("/terminal")

    def _on_audit(self, e):
        # Standalone CDD audit — no field validation required. Hand off the
        # form so the audit page can prefill Site ID + Node Name.
        self.page.integration_form = self._collect_form()
        self.page.go("/audit")

    def _on_integration(self, e):
        f = self._collect_form()

        errors = []
        # Shortcode is mandatory — every site must land in its own
        # LOG/<SHORTCODE>/ folder.
        if not f["shortcode"]:
            errors.append("Site ID is required.")
        if not f["node_name"]:
            errors.append("Node name (LTE/NR) is required.")
        if not f["host"]:
            errors.append("ENM IP is required.")
        if not f["username"]:
            errors.append("Username is required.")
        if not f["password"]:
            errors.append("Password is required.")

        if errors:
            self._show_errors(errors)
            return

        # Shortcode must prefix every node name.
        if not self._validate_shortcode_prefix(f):
            return

        self.error_text.visible = False
        self.error_box.visible = False

        self._persist_session(f)

        # Store integration form data for the integration workflow
        self.page.integration_form = f
        self.page.go("/integration")

    def _persist_session(self, f: dict) -> None:
        """Save every persisted form field so it survives app restarts."""
        save_session(
            host=f.get("host", ""),
            port=f.get("port"),
            username=f.get("username", ""),
            password=f.get("password", ""),
            shortcode=f.get("shortcode", ""),
            node_name=f.get("node_name", ""),
            node_ip=f.get("node_ip", ""),
            subnetwork=f.get("subnetwork", ""),
            node2_name=f.get("node2_name", ""),
            node2_ip=f.get("node2_ip", ""),
            node2_subnetwork=f.get("node2_subnetwork", ""),
            gsm_node_name=f.get("gsm_node_name", ""),
            bsc_name=f.get("bsc_name", ""),
            gsm_node_ip=f.get("gsm_node_ip", ""),
            gsm_subnetwork=f.get("gsm_subnetwork", ""),
            commands_file=f.get("commands_file", ""),
            lkf_file=f.get("lkf_file", ""),
            relation_file=f.get("relation_file", ""),
        )
