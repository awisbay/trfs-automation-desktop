"""
NodeCraft - Flet Desktop GUI Entry Point
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# First-run asset seed for frozen single-exe build: copy
# user-editable defaults from the embedded _MEIPASS dir to the exe
# directory if they don't exist there yet. No-op when running from
# source. Done BEFORE importing GUI modules in case any of them want
# to read config.yaml at import time.
from app_path import ensure_assets_in_app_dir
ensure_assets_in_app_dir([
    "config.yaml",          # site shortcode + SSH credentials
    "config.json",          # integration script paths (ENM cli.py, baseline, …)
    "TEMPLATE_REPORT.xlsx",
    "TRFS commands.txt",
    "snapshot.ico",
])

import flet as ft
from gui.form_page import FormPage
from gui.integration_page import IntegrationPage, IntegrationRunPage
from gui.license_page import LicensePage
from gui.progress_page import ProgressPage
from gui.result_page import ResultPage
from gui.terminal_page import TerminalPage
from gui.theme import BG_TOP
from license_manager import load_saved_license


def main(page: ft.Page):
    # Default title; updated below with the licensed user's name once verified.
    page.title = "NodeCraft v1.0 — ewisbay"
    from app_path import get_app_dir
    # Icon can live next to the exe OR inside _internal/ (PyInstaller COLLECT)
    _app_dir = get_app_dir()
    _icon_candidates = [
        os.path.join(_app_dir, "snapshot.ico"),
        os.path.join(_app_dir, "_internal", "snapshot.ico"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "snapshot.ico"),
    ]
    for _p in _icon_candidates:
        if os.path.exists(_p):
            page.window.icon = os.path.abspath(_p)
            print(f"[ICON] Using: {os.path.abspath(_p)}")
            break
    else:
        print(f"[ICON] Not found in any of: {_icon_candidates}")
    page.window.width = 1380
    page.window.height = 920
    page.window.min_width = 1080
    page.window.min_height = 720
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.spacing = 0
    page.bgcolor = BG_TOP
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
    page.window.title_bar_button_color = "#39C0BA"
    page.window.frameless = False

    def build_view(route: str) -> ft.View:
        if route == "/license":
            return LicensePage(page).build()
        elif route in ("", "/", "/form"):
            return FormPage(page).build()
        elif route == "/progress":
            return ProgressPage(page).build()
        elif route == "/integration":
            return IntegrationPage(page).build()
        elif route == "/integration_run":
            return IntegrationRunPage(page).build()
        elif route == "/result":
            return ResultPage(page).build()
        elif route == "/terminal":
            return TerminalPage(page).build()
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

    # Kill every live SSH session and force-exit when the window is closed
    # so worker threads (paramiko readers, ThreadPoolExecutor workers,
    # Playwright drivers) unwind immediately and the process exits cleanly
    # instead of lingering in the background.
    _shutdown_called = {"done": False}

    def _shutdown(reason: str):
        if _shutdown_called["done"]:
            return
        _shutdown_called["done"] = True
        print(f"[EXIT] shutting down ({reason})")
        # Kill every registered SSH channel — unblocks paramiko readers so
        # their worker threads don't stall the interpreter shutdown.
        try:
            import ssh_registry
            n = ssh_registry.kill_all()
            print(f"[EXIT] killed {n} live SSH session(s)")
        except Exception as exc:
            print(f"[EXIT] ssh_registry.kill_all failed: {exc}")
        # Force the whole process to die. Worker threads are now daemon=True
        # so os._exit takes them down with us, along with any Playwright
        # node.exe child that was mid-capture.
        try:
            page.window.destroy()
        except Exception:
            pass
        os._exit(0)

    def _on_window_event(e):
        data = getattr(e, "data", None)
        # Flet versions differ: data can be "close", a WindowEventType-ish
        # string, or a dict. Match any close-ish signal defensively.
        if data == "close" or (isinstance(data, str) and "close" in data.lower()):
            _shutdown("window close")

    def _on_disconnect(e):
        # Fires when the Flet client disconnects (tab closed, window killed
        # via taskbar, OS shutdown). Belt-and-braces against missed
        # window.on_event fires.
        _shutdown("page disconnect")

    try:
        page.window.prevent_close = False
        page.window.on_event = _on_window_event
    except Exception:
        pass
    try:
        page.on_disconnect = _on_disconnect
    except Exception:
        pass

    # atexit safety net: if the interpreter is somehow shutting down without
    # either of the above firing (e.g. Ctrl+C, SIGTERM from parent), still
    # kill SSH so we don't leak paramiko transports.
    import atexit
    def _atexit_kill():
        try:
            import ssh_registry
            ssh_registry.kill_all()
        except Exception:
            pass
    atexit.register(_atexit_kill)

    # Initial render — check license first
    license_result = load_saved_license()
    start_route = "/form" if license_result["valid"] else "/license"

    if license_result["valid"]:
        p = license_result["payload"]
        print(f"[LICENSE] Valid — user={p.get('user')}, expires={p.get('expires')}")
        _user = p.get("user") or p.get("name")
        if _user:
            page.title = f"NodeCraft v1.0 — Welcome, {_user}"
    else:
        print(f"[LICENSE] {license_result['error']} — showing activation screen")

    print(f"[INIT] Initial views={len(page.views)}, route={page.route}")
    page.views.clear()
    page.views.append(build_view(start_route))
    print(f"[INIT] After append views={len(page.views)}")
    page.update()
    print(f"[INIT] After update views={len(page.views)}")


if __name__ == "__main__":
    from app_path import get_app_dir
    _assets = get_app_dir()
    ft.run(main, assets_dir=_assets)
