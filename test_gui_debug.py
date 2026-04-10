"""Bisect - expand=True in Row with wrap=True"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import flet as ft
from gui.theme import *

def main(page: ft.Page):
    page.title = "Bisect Test 5"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG_TOP
    page.padding = 0
    page.spacing = 0
    page.theme = ft.Theme(color_scheme_seed="#39C0BA")

    hero = ft.Container(
        content=ft.Column([
            ft.Text("TRFS Command Studio", size=42, weight=ft.FontWeight.BOLD, color=TEXT),
        ], spacing=18),
        padding=28,
        expand=True,
        bgcolor=PANEL,
        border_radius=24,
    )

    form_panel = ft.Container(
        content=ft.Column([
            ft.Text("Run Configuration", size=26, weight=ft.FontWeight.BOLD, color=TEXT),
        ], spacing=16),
        padding=28,
        expand=True,
        bgcolor="#0F2132",
        border_radius=24,
    )

    body = ft.Container(
        expand=True,
        gradient=background_gradient(),
        padding=ft.Padding.symmetric(horizontal=28, vertical=26),
        content=ft.Row([hero, form_panel], spacing=20, wrap=True),
    )

    page.add(body)

ft.run(main)
