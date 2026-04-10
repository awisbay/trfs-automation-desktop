"""
TRFS GUI - Results Summary Page
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class ResultPage:
    def __init__(self, page: ft.Page):
        self.page = page

    def build(self) -> ft.View:
        generated_files = getattr(self.page, 'trfs_generated_files', []) or []

        file_rows = []
        for fpath in generated_files:
            folder = os.path.dirname(fpath)
            fname = os.path.basename(fpath)
            file_rows.append(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.INSERT_DRIVE_FILE, color=ft.Colors.GREEN_400, size=20),
                        ft.Text(fname, size=13, weight=ft.FontWeight.W_500, selectable=True),
                        ft.ElevatedButton(
                            "Open Folder",
                            icon=ft.Icons.FOLDER_OPEN,
                            on_click=lambda _, p=folder: os.startfile(p) if sys.platform == "win32" else None,
                            style=ft.ButtonStyle(
                                bgcolor=ft.Colors.BLUE_800,
                                color=ft.Colors.WHITE,
                            ),
                        ),
                    ],
                    spacing=8,
                )
            )

        if not file_rows:
            file_rows.append(
                ft.Text("No files were generated. Check the logs for errors.", color=ft.Colors.RED_400)
            )

        return ft.View(
            route="/result",
            appbar=ft.AppBar(
                title=ft.Text("TRFS - Results", size=18, weight=ft.FontWeight.BOLD),
                bgcolor=ft.Colors.BLUE_900,
                automatically_imply_leading=False,
            ),
            controls=[
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text(
                                "Processing Complete",
                                size=20,
                                weight=ft.FontWeight.BOLD,
                                color=ft.Colors.GREEN_400,
                            ),
                            ft.Divider(),
                            ft.Text(
                                f"Generated Files ({len(generated_files)})",
                                size=14,
                                weight=ft.FontWeight.W_600,
                            ),
                            *file_rows,
                            ft.Divider(),
                            ft.Row(
                                [
                                    ft.ElevatedButton(
                                        "Run Again",
                                        icon=ft.Icons.REPLAY,
                                        on_click=lambda _: asyncio.create_task(self.page.push_route("/")),
                                        style=ft.ButtonStyle(
                                            bgcolor=ft.Colors.BLUE_700,
                                            color=ft.Colors.WHITE,
                                        ),
                                    ),
                                    ft.ElevatedButton(
                                        "Close",
                                        icon=ft.Icons.CLOSE,
                                        on_click=lambda _: self.page.window.destroy(),
                                        style=ft.ButtonStyle(
                                            bgcolor=ft.Colors.GREY_800,
                                            color=ft.Colors.WHITE,
                                        ),
                                    ),
                                ],
                                spacing=12,
                            ),
                        ],
                        spacing=12,
                    ),
                    padding=30,
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
        )