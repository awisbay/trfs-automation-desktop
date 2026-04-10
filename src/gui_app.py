"""
TRFS Automation Tool - Flet Desktop GUI Entry Point
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flet as ft
from gui.form_page import FormPage
from gui.progress_page import ProgressPage
from gui.result_page import ResultPage


def main(page: ft.Page):
    page.title = "TRFS Automation Tool"
    page.window.width = 900
    page.window.height = 750
    page.window.min_width = 700
    page.window.min_height = 550
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    def route_change(e):
        page.views.clear()

        if page.route == "/" or page.route == "/form":
            page.views.append(FormPage(page).build())
        elif page.route == "/progress":
            page.views.append(ProgressPage(page).build())
        elif page.route == "/result":
            page.views.append(ResultPage(page).build())

        page.update()

    page.on_route_change = route_change
    route_change(None)


if __name__ == "__main__":
    ft.run(main)