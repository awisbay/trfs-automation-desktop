# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Collect all Flet runtime data (icons.json, flet_desktop binaries, etc.)
flet_datas = collect_data_files('flet') + collect_data_files('flet_desktop')
playwright_datas = collect_data_files('playwright')

hidden = ['paramiko', 'openpyxl', 'yaml', 'PIL', 'pyautogui', 'playwright',
          'flet', 'flet_desktop', 'cryptography']
hidden += collect_submodules('flet')
hidden += collect_submodules('flet_desktop')

a = Analysis(
    ['src\\gui_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.yaml', '.'),
        ('TEMPLATE_REPORT.xlsx', '.'),
        ('TRFS commands.txt', '.'),
        ('TRFS commands - Copy.txt', '.'),
        ('snapshot.ico', '.'),
        ('src', 'src'),
        ('txt_to_xml_converter.py', '.'),
        ('Relation.txt', '.'),
    ] + flet_datas + playwright_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NodeCraft',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['snapshot.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NodeCraft',
)
