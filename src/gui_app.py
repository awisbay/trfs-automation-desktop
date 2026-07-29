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
    "audit_map.json",       # CDD audit mapping (sheet/column → MO.attribute)
    "TEMPLATE_REPORT.xlsx",
    "TRFS commands.txt",
    "snapshot.ico",
])

import flet as ft
from gui.audit_page import AuditPage
from gui.form_page import FormPage
from gui.integration_page import IntegrationPage, IntegrationRunPage
from gui.license_page import LicensePage
from gui.progress_page import ProgressPage
from gui.result_page import ResultPage
from gui.terminal_page import TerminalPage
from gui.theme import BG_TOP
from license_manager import load_saved_license


# One stable identity for every NodeCraft window, so Windows groups all
# instances under a SINGLE taskbar button (hover → window previews).
APP_USER_MODEL_ID = "Globe.NodeCraft.Integration"


def _set_app_user_model_id():
    """Give this process an explicit AppUserModelID.

    Without it Windows derives app identity from the executable path, and
    every instance ends up as its own taskbar button.
    """
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
        print(f"[TASKBAR] AppUserModelID = {APP_USER_MODEL_ID}")
    except Exception as exc:
        print(f"[TASKBAR] could not set AppUserModelID: {exc}")


def _prefer_bundled_flet_view():
    if not getattr(sys, "frozen", False):
        return

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not bundle_dir:
        return

    flet_view_dir = os.path.join(bundle_dir, "flet_desktop", "app", "flet")
    flet_view_exe = os.path.join(flet_view_dir, "flet.exe")
    if not os.path.isfile(flet_view_exe):
        print(f"[FLET] Bundled view not found: {flet_view_exe}")
        return

    # IMPORTANT (taskbar grouping): with --onefile, ``_MEIPASS`` is a NEW
    # temp folder on every launch, so running flet.exe straight from there
    # makes Windows see each instance as a DIFFERENT application → one
    # taskbar button per window. Copying the client ONCE to a stable path
    # gives every instance the same identity → a single grouped button.
    try:
        import shutil
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        target = os.path.join(base, "NodeCraft", "flet-client", "flet")
        if not os.path.isfile(os.path.join(target, "flet.exe")):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copytree(flet_view_dir, target, dirs_exist_ok=True)
            print(f"[FLET] Seeded stable client → {target}")
        os.environ["FLET_VIEW_PATH"] = target
        print(f"[FLET] Using stable view: {target}")
    except Exception as exc:
        # Seeding failed (permissions, disk) — fall back to the per-run
        # bundled copy. App still works; taskbar just won't group.
        os.environ["FLET_VIEW_PATH"] = flet_view_dir
        print(f"[FLET] Stable seed failed ({exc}); using bundled: {flet_view_exe}")


def main(page: ft.Page):
    # Default title; updated below with the licensed user's name once verified.
    from version import version_string
    page.title = f"{version_string()} — ewisbay"
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
        elif route == "/audit":
            return AuditPage(page).build()
        return FormPage(page).build()

    def route_change(e):
        route = page.route or "/"
        # Guard against duplicate route_change firings for the SAME
        # route. Flet can fire on_route_change redundantly; without
        # this, /integration_run would be built twice — spawning a
        # SECOND set of worker threads + SSH sessions per node. That
        # caused duplicate / out-of-order log lines and SSH socket
        # conflicts (two sessions fighting over the same node).
        if (page.views
                and getattr(page.views[-1], "route", None) == route):
            print(f"[ROUTE] ignoring duplicate route_change for {route}")
            return
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
            from version import version_string
            page.title = f"{version_string()} — Welcome, {_user}"
    else:
        print(f"[LICENSE] {license_result['error']} — showing activation screen")

    print(f"[INIT] Initial views={len(page.views)}, route={page.route}")
    page.views.clear()
    page.views.append(build_view(start_route))
    print(f"[INIT] After append views={len(page.views)}")
    page.update()
    print(f"[INIT] After update views={len(page.views)}")


if __name__ == "__main__":
    # Must run BEFORE the Flet client window is created so the taskbar
    # picks up the shared identity.
    _set_app_user_model_id()
    _prefer_bundled_flet_view()
    from app_path import get_app_dir
    _assets = get_app_dir()
    ft.run(main, assets_dir=_assets)
