"""
TRFS Automation Tool - Flet Desktop GUI Entry Point
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flet as ft
from gui.form_page import FormPage
from gui.integration_page import IntegrationPage, IntegrationRunPage
from gui.progress_page import ProgressPage
from gui.result_page import ResultPage
from gui.theme import BG_TOP


def main(page: ft.Page):
    page.title = "TRFS Generator — by ewisbay"
    from app_path import get_app_dir
    _icon_path = os.path.join(get_app_dir(), "snapshot.ico")
    if os.path.exists(_icon_path):
        page.window.icon = _icon_path
    page.window.width = 1380
    page.window.height = 920
    page.window.min_width = 1080
    page.window.min_height = 720
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.spacing = 0
    page.bgcolor = BG_TOP
    page.window.bgcolor = BG_TOP
    page.theme = ft.Theme(
        color_scheme_seed="#39C0BA",
        color_scheme=ft.ColorScheme(
            surface=BG_TOP,
            on_surface=BG_TOP,
            surface_container=BG_TOP,
            surface_container_highest=BG_TOP,
            surface_container_high=BG_TOP,
            surface_container_low=BG_TOP,
            surface_container_lowest=BG_TOP,
        ),
        scaffold_bgcolor=BG_TOP,
        scrollbar_theme=ft.ScrollbarTheme(
            thumb_color="#39C0BA",
            track_color="#163247",
        ),
    )
    page.window.title_bar_hidden = False
    page.window.frameless = False

    def build_view(route: str) -> ft.View:
        if route in ("", "/", "/form"):
            return FormPage(page).build()
        elif route == "/progress":
            return ProgressPage(page).build()
        elif route == "/integration":
            return IntegrationPage(page).build()
        elif route == "/integration_run":
            return IntegrationRunPage(page).build()
        elif route == "/result":
            return ResultPage(page).build()
        return FormPage(page).build()

    def route_change(e):
        route = page.route or "/"
        print(f"[ROUTE] route_change fired: route={route}, views before={len(page.views)}")
        page.views.clear()
        page.views.append(build_view(route))
        print(f"[ROUTE] views after={len(page.views)}, controls in view={len(page.views[-1].controls)}")
        page.update()

    def view_pop(e):
        print(f"[VIEW_POP] fired, views={len(page.views)}")
        page.views.pop()
        if page.views:
            top_view = page.views[-1]
            page.go(top_view.route)

    page.on_route_change = route_change
    page.on_view_pop = view_pop

    # Initial render
    print(f"[INIT] Initial views={len(page.views)}, route={page.route}")
    page.views.clear()
    page.views.append(build_view("/form"))
    print(f"[INIT] After append views={len(page.views)}")
    page.update()
    print(f"[INIT] After update views={len(page.views)}")


if __name__ == "__main__":
    from app_path import get_app_dir
    _assets = get_app_dir()
    ft.run(main, assets_dir=_assets)
