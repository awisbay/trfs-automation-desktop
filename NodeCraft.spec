# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for NodeCraft single-exe build.

Build with:
    venv\\Scripts\\pyinstaller.exe --noconfirm --clean NodeCraft.spec

Output:
    dist\\NodeCraft.exe  (single self-contained exe, ~80-150 MB)

On first run the exe seeds these user-editable files next to itself:
    config.yaml
    TEMPLATE_REPORT.xlsx
    TRFS commands.txt
    snapshot.ico

Logs / output / screenshots are written under the exe's directory
(see app_path.get_app_dir).
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ── Project root resolution ────────────────────────────────────
# SPECPATH is provided by PyInstaller; equals the dir holding this .spec.
PROJECT_ROOT = os.path.abspath(SPECPATH)


def _project_path(*parts):
    return os.path.join(PROJECT_ROOT, *parts)


# ── Flet runtime files ─────────────────────────────────────────
# Flet ships its own desktop client (the Flutter-built viewer)
# inside the ``flet`` package. ``collect_data_files`` pulls those
# Python-side data (icons.json, etc.) into the bundle.
flet_datas = collect_data_files("flet")
flet_hidden = collect_submodules("flet")
flet_desktop_hidden = collect_submodules("flet_desktop")

# The actual desktop CLIENT binary (~95 MB extracted, ~38 MB zipped)
# is NOT shipped with the flet wheel — flet_desktop downloads it on
# first run from GitHub. For an offline single-exe build we pre-pack
# it into the bundle at the exact path ``flet_desktop`` checks:
#     get_package_bin_dir() → <flet_desktop>/app/flet-windows.zip
# When the bundled zip is present, flet_desktop extracts it locally
# instead of hitting the network. Run ``build_assets/build_flet_zip.py``
# (or the in-repo helper) to regenerate the zip from the cached
# client at ``~/.flet/client/flet-desktop-full-<version>/flet/``.
flet_client_zip = _project_path("build_assets", "flet-windows.zip")
if os.path.exists(flet_client_zip):
    flet_datas.append((flet_client_zip, os.path.join("flet_desktop", "app")))
else:
    # Loud warning — without this the exe will need internet on
    # first launch to download the 95 MB client.
    print(
        "[NodeCraft.spec] WARNING: flet-windows.zip not found at "
        + flet_client_zip + " — the built exe will download Flet's "
        "desktop client from GitHub on first launch. "
        "Regenerate with the helper script to ship offline."
    )

# Paramiko has dynamic crypto backend selection — pull all submodules
# to avoid runtime ImportError.
paramiko_hidden = collect_submodules("paramiko")
cryptography_hidden = collect_submodules("cryptography")

# ── User-editable assets seeded next to the exe on first run ───
# These also live inside the bundle so the exe is fully self-contained.
asset_datas = []
for fn in ("config.yaml", "TEMPLATE_REPORT.xlsx",
           "TRFS commands.txt", "snapshot.ico"):
    p = _project_path(fn)
    if os.path.exists(p):
        asset_datas.append((p, "."))

# ── Module-internal config (read via __file__ in integration_runner) ──
src_config = _project_path("src", "config.json")
if os.path.exists(src_config):
    asset_datas.append((src_config, "."))   # placed at bundle root
    # integration_runner.py reads it from beside itself — that's the
    # same _MEIPASS dir at runtime, so root placement is correct.

datas = flet_datas + asset_datas

hiddenimports = (
    flet_hidden
    + flet_desktop_hidden
    + paramiko_hidden
    + cryptography_hidden
    + [
        "openpyxl",
        "openpyxl.workbook",
        "openpyxl.styles",
        "PIL",
        "PIL.Image",
        "yaml",
        "tkinter",          # for clipboard fallback in terminal_page
    ]
)

# ── Excludes — drop libraries we removed from the build ───────
# Playwright + Chromium pulled out to shrink the bundle; pyautogui
# kept (~5 MB) since enm_capture imports it but TRFS button is
# disabled anyway.
excludes = [
    "playwright",
    "playwright.sync_api",
    "playwright.async_api",
    "playwright._impl",
    # Test infra we don't ship
    "tkinter.test",
    "test", "tests",
    # Stuff that shouldn't be bundled but sometimes gets pulled in
    "PyQt5", "PySide2", "PySide6", "PyQt6",
    "matplotlib", "scipy", "numpy.testing",
]

block_cipher = None

a = Analysis(
    [_project_path("src", "gui_app.py")],
    pathex=[_project_path("src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NodeCraft",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX often false-flags as malware
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                   # windowed app — no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_project_path("snapshot.ico"),
)
