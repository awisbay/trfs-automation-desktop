"""
TRFS GUI - Configuration Form Page
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config_loader import load_config, build_config_from_form
from command_parser import parse_commands_file


class FormPage:
    def __init__(self, page: ft.Page):
        self.page = page
        self._try_load_defaults()

    def _try_load_defaults(self):
        self.defaults = {
            "shortcode": "",
            "node_name": "",
            "host": "",
            "port": 5023,
            "username": "",
            "password": "",
            "commands_file": "",
            "config_path": "",
        }
        try:
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"
            )
            if os.path.exists(config_path):
                config = load_config(config_path)
                self.defaults["shortcode"] = config.site.shortcode
                self.defaults["node_name"] = config.site.node_name
                self.defaults["host"] = config.ssh.host
                self.defaults["port"] = config.ssh.port
                self.defaults["username"] = config.ssh.username
                self.defaults["password"] = config.ssh.password
                self.defaults["config_path"] = config_path
                base_dir = config.base_dir
                self.defaults["commands_file"] = os.path.join(base_dir, config.paths.commands)
        except Exception:
            pass

    def build(self) -> ft.View:
        self.shortcode_field = ft.TextField(
            label="Shortcode",
            value=self.defaults["shortcode"],
            width=400,
            autofocus=True,
        )
        self.node_name_field = ft.TextField(
            label="Node Name",
            value=self.defaults["node_name"],
            width=600,
        )
        self.host_field = ft.TextField(
            label="SSH Host",
            value=self.defaults["host"],
            width=300,
        )
        self.username_field = ft.TextField(
            label="Username",
            value=self.defaults["username"],
            width=300,
        )
        self.password_field = ft.TextField(
            label="Password",
            value=self.defaults["password"],
            password=True,
            can_reveal_password=True,
            width=300,
        )
        self.commands_field = ft.TextField(
            label="Commands File",
            value=self.defaults["commands_file"],
            width=520,
            read_only=False,
        )
        self.file_picker = ft.FilePicker()

        self.demo_checkbox = ft.Checkbox(label="Demo Mode (no SSH)", value=False)
        self.error_text = ft.Text("", color=ft.Colors.RED_400, visible=False)
        self.parse_info = ft.Text("", color=ft.Colors.GREEN_400, visible=False, size=12)

        return ft.View(
            route="/form",
            appbar=ft.AppBar(
                title=ft.Text("TRFS Automation Tool", size=20, weight=ft.FontWeight.BOLD),
                bgcolor=ft.Colors.BLUE_900,
            ),
            controls=[
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text("Configuration", size=18, weight=ft.FontWeight.W_600),
                            ft.Divider(),
                            self.shortcode_field,
                            self.node_name_field,
                            ft.Row(
                                [
                                    self.host_field,
                                    self.username_field,
                                    self.password_field,
                                ],
                                wrap=False,
                            ),
                            ft.Row(
                                [
                                    self.commands_field,
                                    ft.ElevatedButton(
                                        "Browse",
                                        icon=ft.Icons.FOLDER_OPEN,
                                        on_click=self._browse_commands_file,
                                    ),
                                ],
                            ),
                            self.parse_info,
                            ft.Divider(),
                            ft.Row(
                                [
                                    self.demo_checkbox,
                                    ft.Container(expand=True),
                                    ft.ElevatedButton(
                                        "Start",
                                        icon=ft.Icons.PLAY_ARROW,
                                        style=ft.ButtonStyle(
                                            bgcolor=ft.Colors.GREEN_700,
                                            color=ft.Colors.WHITE,
                                        ),
                                        on_click=self._on_start,
                                    ),
                                ],
                            ),
                            self.error_text,
                        ],
                        spacing=12,
                    ),
                    padding=30,
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
        )

    async def _browse_commands_file(self, e):
        result = await self.file_picker.pick_files(
            dialog_title="Select Commands File",
            file_type=ft.FilePickerFileType.ANY,
        )
        if result and len(result) > 0:
            self.commands_field.value = result[0].path
            self._validate_commands()
            self.page.update()

    def _validate_commands(self):
        path = self.commands_field.value.strip()
        if not path or not os.path.isfile(path):
            self.parse_info.visible = False
            return
        try:
            all_cmds = parse_commands_file(path)
            band_names = list(all_cmds.keys())
            total = sum(len(cats) for cats in all_cmds.values())
            self.parse_info.value = (
                f"Found {len(band_names)} bands: {', '.join(band_names)} ({total} categories total)"
            )
            self.parse_info.visible = True
            self.parse_info.color = ft.Colors.GREEN_400
        except Exception as ex:
            self.parse_info.value = f"Error parsing: {ex}"
            self.parse_info.visible = True
            self.parse_info.color = ft.Colors.RED_400

    def _on_start(self, e):
        shortcode = self.shortcode_field.value.strip()
        node_name = self.node_name_field.value.strip()
        host = self.host_field.value.strip()
        username = self.username_field.value.strip()
        password = self.password_field.value.strip()
        commands_file = self.commands_field.value.strip()
        demo_mode = self.demo_checkbox.value

        errors = []
        if not shortcode:
            errors.append("Shortcode is required")
        if not node_name:
            errors.append("Node Name is required")
        if not host and not demo_mode:
            errors.append("SSH Host is required")
        if not username and not demo_mode:
            errors.append("Username is required")
        if not password and not demo_mode:
            errors.append("Password is required")
        if not commands_file:
            errors.append("Commands File is required")
        elif not os.path.isfile(commands_file):
            errors.append(f"Commands file not found: {commands_file}")

        if errors:
            self.error_text.value = "\n".join(errors)
            self.error_text.visible = True
            self.page.update()
            return

        self.error_text.visible = False

        config = build_config_from_form(
            shortcode=shortcode,
            node_name=node_name,
            host=host,
            username=username,
            password=password,
            commands_file=commands_file,
            config_path=self.defaults.get("config_path") or None,
        )

        self.page.trfs_config = config
        self.page.trfs_demo_mode = demo_mode
        self.page.trfs_commands_file = commands_file

        asyncio.create_task(self.page.push_route("/progress"))