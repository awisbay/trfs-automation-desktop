import flet as ft


BG_TOP = "#07111E"
BG_BOTTOM = "#102337"
PANEL = "#12263A"
PANEL_RAISED = "#18324B"
PANEL_SOFT = "#0F1E2C"
TEXT = "#F4F7FB"
TEXT_MUTED = "#9EB1C7"
ACCENT = "#39C0BA"
ACCENT_WARM = "#FFB14A"
SUCCESS = "#5FD38D"
DANGER = "#FF6B6B"
INFO = "#7EC6FF"
BORDER = "#29455F"


def background_gradient() -> ft.LinearGradient:
    return ft.LinearGradient(
        begin=ft.Alignment(-1, -1),
        end=ft.Alignment(1, 1),
        colors=[BG_TOP, BG_BOTTOM],
    )


def soft_shadow(color: str = "#000000", opacity: float = 0.18) -> list[ft.BoxShadow]:
    return [
        ft.BoxShadow(
            spread_radius=0,
            blur_radius=32,
            color=ft.Colors.with_opacity(opacity, color),
            offset=ft.Offset(0, 18),
        )
    ]


def panel(
    content: ft.Control,
    *,
    padding: int | ft.Padding = 24,
    bgcolor: str = PANEL,
    expand: bool = False,
    border_color: str = BORDER,
) -> ft.Container:
    return ft.Container(
        content=content,
        padding=padding,
        expand=expand,
        bgcolor=bgcolor,
        border=ft.Border.all(1, ft.Colors.with_opacity(0.65, border_color)),
        border_radius=24,
        shadow=soft_shadow(),
    )


def glow_orb(color: str, width: int, height: int, top: int, left: int | None = None, right: int | None = None) -> ft.Container:
    return ft.Container(
        width=width,
        height=height,
        top=top,
        left=left,
        right=right,
        border_radius=max(width, height),
        blur=70,
        bgcolor=ft.Colors.with_opacity(0.16, color),
    )


def badge(label: str, color: str = ACCENT, icon: str | None = None) -> ft.Container:
    row_items: list[ft.Control] = []
    if icon:
        row_items.append(ft.Icon(icon, size=14, color=color))
    row_items.append(
        ft.Text(
            label,
            size=11,
            color=TEXT,
            weight=ft.FontWeight.W_600,
        )
    )
    return ft.Container(
        content=ft.Row(row_items, spacing=6, tight=True),
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        bgcolor=ft.Colors.with_opacity(0.14, color),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.3, color)),
        border_radius=999,
    )


def input_style() -> ft.InputBorder:
    return ft.InputBorder.UNDERLINE


def primary_button_style() -> ft.ButtonStyle:
    return ft.ButtonStyle(
        bgcolor=ACCENT,
        color="#06242A",
        padding=ft.Padding.symmetric(horizontal=20, vertical=18),
        shape=ft.RoundedRectangleBorder(radius=16),
    )


def secondary_button_style() -> ft.ButtonStyle:
    return ft.ButtonStyle(
        bgcolor=PANEL_RAISED,
        color=TEXT,
        padding=ft.Padding.symmetric(horizontal=18, vertical=18),
        shape=ft.RoundedRectangleBorder(radius=16),
    )
