# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\gui_app.py'],
    pathex=[],
    binaries=[],
    datas=[('config.yaml', '.'), ('TEMPLATE_REPORT.xlsx', '.'), ('TRFS commands.txt', '.'), ('TRFS commands - Copy.txt', '.'), ('snapshot.ico', '.'), ('src', 'src'), ('txt_to_xml_converter.py', '.'), ('Relation.txt', '.')],
    hiddenimports=['paramiko', 'openpyxl', 'yaml', 'PIL', 'pyautogui', 'playwright'],
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
    version='C:\\Users\\ewisbay\\AppData\\Local\\Temp\\bf201d99-e8af-4f51-bbfa-1ee4a2d3c927',
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
