"""
TRFS GUI - Elevated configuration workspace.
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from command_parser import parse_commands_file
from config_loader import build_config_from_form, load_config
from session import load_session, save_session
from gui.theme import (
    ACCENT,
    ACCENT_WARM,
    BG_BOTTOM,
    BG_TOP,
    BORDER,
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
        if config_path:
            # Always remember the path so downstream build_config_from_form
            # loads enm/moshell/paths defaults — even if prefill below fails.
            self.defaults["config_path"] = config_path
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

        session = load_session()
        if session.get("host"):
            self.defaults["host"] = session["host"]
        if session.get("port"):
            self.defaults["port"] = session["port"]
        if session.get("username"):
            self.defaults["username"] = session["username"]
        if session.get("password"):
            self.defaults["password"] = session["password"]

    def build(self) -> ft.View:
        self.shortcode_field = self._text_field(
            "Shortcode",
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
        # --- Node 2: GSM ---
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
            "LKF File",
            self.defaults.get("lkf_file", ""),
            expand=True,
        )
        self.lkf_browse = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for LKF file",
            on_click=self._on_browse_lkf,
        )
        self.relation_field = self._text_field(
            "Relation File",
            self.defaults.get("relation_file", ""),
            expand=True,
        )
        self.relation_browse = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN,
            icon_color=ACCENT,
            tooltip="Browse for Relation file",
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

        hero = panel(
            ft.Column(
                [
                    badge("Desktop Workflow", ACCENT, ft.Icons.DESKTOP_WINDOWS),
                    ft.Text(
                        "TRFS Generator",
                        size=42,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT,
                    ),
                    ft.Text(
                        "Control room for SSH execution, ENM capture, Excel reports, and Integration testing.",
                        size=15,
                        color=TEXT_MUTED,
                    ),
                    ft.Row(
                        [
                            badge("SSH + Moshell", INFO, ft.Icons.TERMINAL),
                            badge("ENM Browser Capture", ACCENT_WARM, ft.Icons.CAMERA_ALT),
                            badge("Excel Output", SUCCESS, ft.Icons.GRID_ON),
                            badge("Integration", "#9C7CFF", ft.Icons.INTEGRATION_INSTRUCTIONS),
                        ],
                        wrap=True,
                        spacing=10,
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            self._metric_card("Bands Ready", self.band_count_text, INFO),
                            self._metric_card("Categories", self.category_count_text, ACCENT),
                        ],
                        spacing=14,
                        wrap=True,
                    ),
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
                        "Set the site, access, and source file once, then launch the full TRFS flow from one place.",
                        size=14,
                        color=TEXT_MUTED,
                    ),
                    self._section_label("Site Identity"),
                    ft.Row(
                        [self.shortcode_field],
                        spacing=14,
                    ),
                    self._section_label("Node 1 — LTE / NR"),
                    ft.Row(
                        [self.node_name_field, self.node_ip_field, self.subnetwork_field],
                        spacing=14,
                    ),
                    self._section_label("Node 2 — GSM"),
                    ft.Row(
                        [self.gsm_node_name_field, self.bsc_name_field, self.gsm_node_ip_field, self.gsm_subnetwork_field],
                        spacing=14,
                    ),
                    self._section_label("Access"),
                    ft.Row(
                        [self.host_field, self.port_field, self.username_field, self.password_field],
                        spacing=14,
                    ),
                    self._section_label("Command Source"),
                    ft.Row(
                        [self.commands_field, self.browse_button],
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
                        [self.lkf_field, self.lkf_browse],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [self.relation_field, self.relation_browse],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.error_box,
                    ft.Container(height=8),
                    ft.Row(
                        [
                            ft.Container(expand=True),
                            ft.ElevatedButton(
                                "Integration Launch",
                                icon=ft.Icons.INTEGRATION_INSTRUCTIONS,
                                style=secondary_button_style(),
                                on_click=self._on_integration,
                            ),
                            ft.ElevatedButton(
                                "TRFS Launch",
                                icon=ft.Icons.PLAY_CIRCLE_FILL_ROUNDED,
                                style=primary_button_style(),
                                on_click=self._on_start,
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=16,
            ),
            bgcolor="#0F2132",
            padding=28,
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
            dialog_title="Select LKF File",
            allow_multiple=False,
        )
        if files:
            self.lkf_field.value = files[0].path
            self.page.update()

    async def _on_browse_relation(self, e):
        files = await self.file_picker.pick_files(
            dialog_title="Select Relation File",
            allow_multiple=False,
        )
        if files:
            self.relation_field.value = files[0].path
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

    def _on_start(self, e):
        f = self._collect_form()

        errors = []
        if not f["shortcode"]:
            errors.append("Shortcode is required.")
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

        self.error_text.visible = False
        self.error_box.visible = False

        save_session(host=f["host"], port=f["port"],
                     username=f["username"], password=f["password"])

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
        )

        self.page.trfs_config = config
        self.page.trfs_demo_mode = False
        self.page.trfs_commands_file = f["commands_file"]

        asyncio.create_task(self.page.push_route("/progress"))

    def _on_integration(self, e):
        f = self._collect_form()

        errors = []
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

        self.error_text.visible = False
        self.error_box.visible = False

        save_session(host=f["host"], port=f["port"],
                     username=f["username"], password=f["password"])

        # Store integration form data for the integration workflow
        self.page.integration_form = f
        asyncio.create_task(self.page.push_route("/integration"))
