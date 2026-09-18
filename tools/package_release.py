"""Package a built NodeCraft into a distributable zip.

    python tools/package_release.py

Run AFTER ``pyinstaller --noconfirm --clean NodeCraft.spec``. Produces
``dist/NodeCraft_v<version>.zip`` containing:

    NodeCraft.exe         the one-file build
    config.json           src/config.json      (read from beside the exe first)
    audit_map.json        src/audit_map.json   (read from beside the exe first)
    READ ME FIRST.txt     update instructions

Why a zip and not just the exe: the app prefers a ``config.json`` /
``audit_map.json`` sitting next to the exe over its bundled copy. Users who
copied only the new exe kept an OLD config.json beside it, so audits compared
against stale settings. Shipping the JSON files with the exe keeps them in step.

Never includes license.key, config.yaml, LOG/, .session.json or other run data.
"""
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

README = """NodeCraft v{version}
==================

ISI PAKET
  NodeCraft.exe     aplikasi
  config.json       konfigurasi (path script, BSC map, cutover, dll.)
  audit_map.json    mapping audit CDD

CARA INSTALL / UPDATE
  1. Tutup NodeCraft kalau sedang berjalan.
  2. Extract SEMUA file di zip ini ke folder NodeCraft kamu, dan pilih
     "Replace" untuk file yang sudah ada.
  3. Jalankan NodeCraft.exe.

PENTING
  - Selalu salin NodeCraft.exe BERSAMA config.json dan audit_map.json.
    NodeCraft membaca config.json / audit_map.json yang ada di samping exe.
    Kalau hanya exe yang diganti, file JSON lama tetap dipakai dan hasil
    audit bisa mismatch.
  - JANGAN hapus license.key dan folder LOG di folder NodeCraft kamu —
    keduanya tidak ada di zip ini dan tidak akan tertimpa.
"""


def read_version() -> str:
    text = open(os.path.join(ROOT, "src", "version.py"), encoding="utf-8").read()
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        sys.exit("Cannot read __version__ from src/version.py")
    return match.group(1)


def main() -> None:
    version = read_version()
    exe = os.path.join(DIST, "NodeCraft.exe")
    if not os.path.isfile(exe):
        sys.exit(f"Missing {exe} - run PyInstaller first.")
    version_py = os.path.join(ROOT, "src", "version.py")
    if os.path.getmtime(exe) < os.path.getmtime(version_py):
        sys.exit("dist/NodeCraft.exe is older than src/version.py - rebuild first "
                 "so the exe matches v" + version + ".")

    payload = [(exe, "NodeCraft.exe")]
    for name in ("config.json", "audit_map.json"):
        path = os.path.join(ROOT, "src", name)
        with open(path, encoding="utf-8") as fh:
            json.load(fh)  # refuse to ship a broken config
        payload.append((path, name))

    out = os.path.join(DIST, f"NodeCraft_v{version}.zip")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arc in payload:
            zf.write(src, arc)
        zf.writestr("READ ME FIRST.txt",
                    README.format(version=version).replace("\n", "\r\n"))

    with zipfile.ZipFile(out) as zf:
        names = sorted(zf.namelist())
        bad = zf.testzip()
    expected = sorted(["NodeCraft.exe", "config.json", "audit_map.json",
                       "READ ME FIRST.txt"])
    if bad or names != expected:
        sys.exit(f"Zip verification failed: bad={bad} names={names}")
    print(f"{out}  ({os.path.getsize(out) / 1024 / 1024:.1f} MB)")
    for name in names:
        print(f"  {name}")


if __name__ == "__main__":
    main()
