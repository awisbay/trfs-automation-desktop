"""
TRFS GUI - Final result workspace.
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gui.theme import (
    ACCENT,
    INFO,
    PANEL,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    background_gradient,
    badge,
    panel,
    primary_button_style,
    secondary_button_style,
)


class ResultPage:
    def __init__(self, page: ft.Page):
        self.page = page

    def build(self) -> ft.View:
        generated_files = getattr(self.page, "trfs_generated_files", []) or []
        duration = getattr(self.page, "trfs_duration", "—")

        file_cards: list[ft.Control] = []
        for fpath in generated_files:
            folder = os.path.dirname(fpath)
            fname = os.path.basename(fpath)
            file_cards.append(
                panel(
                    ft.Row(
                        [
                            ft.Container(
                                width=48,
                                height=48,
                                border_radius=14,
                                bgcolor=ft.Colors.with_opacity(0.14, SUCCESS),
                                alignment=ft.Alignment(0, 0),
                                content=ft.Icon(ft.Icons.DESCRIPTION_ROUNDED, color=SUCCESS, size=24),
                            ),
                            ft.Column(
                                [
                                    ft.Text(fname, size=14, color=TEXT, weight=ft.FontWeight.BOLD),
                                    ft.Text(fpath, size=11, color=TEXT_MUTED, selectable=True),
                                ],
                                spacing=5,
                                expand=True,
                            ),
                            ft.ElevatedButton(
                                "Open Folder",
                                icon=ft.Icons.FOLDER_OPEN,
                                style=secondary_button_style(),
                                on_click=lambda _, p=folder: os.startfile(p) if sys.platform == "win32" else None,
                            ),
                        ],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor="#11253A",
                    padding=18,
                )
            )

        if not file_cards:
            file_cards.append(
                panel(
                    ft.Text(
                        "No files were generated. Check the run log and ENM flow before trying again.",
                        color="#FFB4B4",
                    ),
                    bgcolor="#301A22",
                )
            )

        hero = panel(
            ft.Column(
                [
                    ft.Row(
                        [
                            badge("Run Complete", SUCCESS, ft.Icons.CHECK_CIRCLE),
                            ft.Container(expand=True),
                            ft.ElevatedButton(
                                "Run Another Site",
                                icon=ft.Icons.ARROW_BACK,
                                style=primary_button_style(),
                                on_click=lambda _: asyncio.create_task(self.page.push_route("/")),
                            ),
                            ft.ElevatedButton(
                                "Close",
                                icon=ft.Icons.CLOSE_ROUNDED,
                                style=secondary_button_style(),
                                on_click=lambda _: self.page.window.destroy(),
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text("Reports are ready", size=34, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Your TRFS output has been assembled and the generated files are ready for review.",
                        size=14,
                        color=TEXT_MUTED,
                    ),
                    ft.Row(
                        [
                            self._metric("Files", str(len(generated_files)), SUCCESS),
                            self._metric("Duration", duration, INFO),
                            self._metric("State", "Success" if generated_files else "Review needed", ACCENT),
                        ],
                        spacing=14,
                        wrap=True,
                    ),
                ],
                spacing=16,
            ),
            bgcolor=PANEL,
            padding=28,
        )

        files_panel = panel(
            ft.Column(
                [
                    ft.Text("Generated output", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Each Excel file is listed below with a direct folder shortcut.",
                        size=12,
                        color=TEXT_MUTED,
                    ),
                    *file_cards,
                ],
                spacing=16,
            ),
            bgcolor="#0E2031",
            padding=24,
        )

        return ft.View(
            route="/result",
            padding=0,
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Container(
                    gradient=background_gradient(),
                    padding=ft.Padding.symmetric(horizontal=28, vertical=24),
                    content=ft.Column(
                        [
                            hero,
                            files_panel,
                        ],
                        spacing=18,
                    ),
                )
            ],
        )

    def _metric(self, label: str, value: str, color: str) -> ft.Container:
        return ft.Container(
            width=180,
            padding=16,
            bgcolor=ft.Colors.with_opacity(0.14, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.3, color)),
            border_radius=18,
            content=ft.Column(
                [
                    ft.Text(label, size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                    ft.Text(value, size=18, color=TEXT, weight=ft.FontWeight.BOLD),
                ],
                spacing=6,
            ),
        )
