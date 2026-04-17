"""
NodeCraft GUI — License Activation Page
"""
import flet as ft

from gui.theme import (
    ACCENT,
    BG_BOTTOM,
    BG_TOP,
    BORDER,
    PANEL,
    PANEL_RAISED,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    panel,
)
from license_manager import verify_license, save_license, get_hostname


class LicensePage:
    def __init__(self, page: ft.Page):
        self.page = page

    def build(self) -> ft.View:
        self.license_field = ft.TextField(
            label="License Key",
            hint_text="Paste your license key here",
            expand=True,
            multiline=True,
            min_lines=3,
            max_lines=5,
            dense=False,
            border_radius=16,
            filled=True,
            bgcolor=ft.Colors.with_opacity(0.25, BG_BOTTOM),
            border_color=BORDER,
            focused_border_color=ACCENT,
            label_style=ft.TextStyle(color=TEXT_MUTED),
            text_style=ft.TextStyle(color=TEXT, size=14),
            cursor_color=ACCENT,
        )

        self.status_text = ft.Text("", size=13, visible=False)
        self.info_column = ft.Column([], spacing=4, visible=False)

        content = panel(
            ft.Column(
                [
                    ft.Container(height=20),
                    ft.Icon(ft.Icons.LOCK_OUTLINE, size=48, color=ACCENT),
                    ft.Container(height=8),
                    ft.Text(
                        "NodeCraft",
                        size=36,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "License Activation",
                        size=16,
                        color=ACCENT,
                        weight=ft.FontWeight.W_500,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Container(height=16),
                    ft.Text(
                        "Enter your license key to activate NodeCraft.\n"
                        "Send your hostname below to your administrator to obtain a license.",
                        size=13,
                        color=TEXT_MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Container(height=14),
                    self._hostname_box(),
                    ft.Container(height=14),
                    self.license_field,
                    ft.Container(height=8),
                    self.status_text,
                    self.info_column,
                    ft.Container(height=16),
                    ft.ElevatedButton(
                        "Activate",
                        icon=ft.Icons.VERIFIED_USER,
                        width=200,
                        style=ft.ButtonStyle(
                            bgcolor=ACCENT,
                            color="#FFFFFF",
                            shape=ft.RoundedRectangleBorder(radius=14),
                            padding=ft.Padding.symmetric(horizontal=24, vertical=14),
                        ),
                        on_click=self._on_activate,
                    ),
                    ft.Container(height=30),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.COPYRIGHT, size=12, color=TEXT_MUTED),
                            ft.Text(
                                "NodeCraft by ewisbay",
                                size=11,
                                color=TEXT_MUTED,
                                weight=ft.FontWeight.W_500,
                            ),
                        ],
                        spacing=6,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            bgcolor=PANEL,
            padding=40,
        )

        body = ft.Container(
            expand=True,
            gradient=background_gradient(),
            alignment=ft.alignment.center,
            padding=ft.Padding.symmetric(horizontal=80, vertical=40),
            content=ft.Container(
                content=content,
                width=520,
            ),
        )

        return ft.View(
            route="/license",
            padding=0,
            spacing=0,
            bgcolor=BG_TOP,
            controls=[body],
        )

    def _on_activate(self, e):
        key = self.license_field.value.strip()
        if not key:
            self.status_text.value = "Please enter a license key."
            self.status_text.color = "#FFB4B4"
            self.status_text.visible = True
            self.info_column.visible = False
            self.page.update()
            return

        result = verify_license(key)

        if result["valid"]:
            # Save to disk
            save_license(key)

            payload = result["payload"]
            self.status_text.value = "License activated successfully!"
            self.status_text.color = SUCCESS
            self.status_text.visible = True

            info_rows = [
                self._info_row("Licensed to", payload.get("user", "—")),
                self._info_row("Expires", payload.get("expires", "—")),
                self._info_row("Issued", payload.get("issued", "—")),
            ]
            if payload.get("hostname"):
                info_rows.append(self._info_row("PC-locked to", payload["hostname"]))
            self.info_column.controls = info_rows
            self.info_column.visible = True
            self.page.update()

            # Navigate to main app after a short delay
            import time, threading
            def _go():
                time.sleep(1.5)
                self.page.go("/form")
            threading.Thread(target=_go, daemon=True).start()
        else:
            self.status_text.value = result["error"]
            self.status_text.color = "#FFB4B4"
            self.status_text.visible = True
            self.info_column.visible = False

            # Show partial info if payload was decoded
            if result["payload"]:
                payload = result["payload"]
                self.info_column.controls = [
                    self._info_row("Licensed to", payload.get("user", "—")),
                    self._info_row("Expires", payload.get("expires", "—")),
                ]
                self.info_column.visible = True

            self.page.update()

    def _hostname_box(self) -> ft.Container:
        hostname = get_hostname()

        def _copy(e):
            self.page.set_clipboard(hostname)
            e.control.tooltip = "Copied!"
            self.page.update()

        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            bgcolor=PANEL_RAISED,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.3, ACCENT)),
            border_radius=12,
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.COMPUTER, size=18, color=ACCENT),
                    ft.Column(
                        [
                            ft.Text("Your Hostname", size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                            ft.Text(
                                hostname,
                                size=14,
                                color=TEXT,
                                weight=ft.FontWeight.W_600,
                                selectable=True,
                            ),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CONTENT_COPY,
                        icon_color=ACCENT,
                        icon_size=18,
                        tooltip="Copy hostname",
                        on_click=_copy,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _info_row(self, label: str, value: str) -> ft.Row:
        return ft.Row(
            [
                ft.Text(f"{label}:", size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                ft.Text(value, size=12, color=TEXT),
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.CENTER,
        )
