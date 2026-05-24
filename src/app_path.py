"""
Resolve the application root directory and manage bundled assets.

When running as a PyInstaller frozen exe, ``get_app_dir()`` is the
folder containing the exe (where logs/config end up on disk). When
running from source, it is the project root (one level above src/).

For single-file (--onefile) builds, PyInstaller extracts data files
into a temp dir at ``sys._MEIPASS``. We use that as the SOURCE of
default assets and ``ensure_assets_in_app_dir`` copies them next to
the exe on first run so the operator can edit them (config.yaml etc.)
without re-building.
"""
import logging
import os
import shutil
import sys
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller --onefile: exe lives here
        return os.path.dirname(sys.executable)
    # Running from source: src/ -> project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_bundle_dir() -> Optional[str]:
    """When frozen, the dir PyInstaller extracted bundled data into.
    Returns None when running from source."""
    if getattr(sys, "frozen", False):
        # ``_MEIPASS`` is set by PyInstaller's bootloader for both
        # --onefile (temp extraction) and --onedir (the bundle root).
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return None


def get_resource_path(name: str) -> Optional[str]:
    """Find a bundled resource by relative path.

    Lookup order:
      1. ``<exe_dir>/<name>``    — user's editable copy (post first-run)
      2. ``<_MEIPASS>/<name>``  — read-only default shipped with exe
      3. ``<project_root>/<name>`` — dev-mode fallback
    Returns absolute path of the first match, or ``None``.
    """
    candidates = [os.path.join(get_app_dir(), name)]
    bundle = get_bundle_dir()
    if bundle:
        candidates.append(os.path.join(bundle, name))
    candidates.append(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            name,
        )
    )
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def ensure_assets_in_app_dir(filenames: Iterable[str]) -> list[str]:
    """On first frozen-exe run, copy each filename from the bundled
    ``_MEIPASS`` directory to the exe directory if it doesn't exist
    there yet. Lets users edit config.yaml, commands file, etc.
    without re-building.

    No-op when running from source (files already in project root).

    Returns a list of paths that were created (newly copied).
    """
    if not getattr(sys, "frozen", False):
        return []
    bundle = get_bundle_dir()
    if not bundle:
        return []
    app_dir = get_app_dir()
    copied: list[str] = []
    for name in filenames:
        target = os.path.join(app_dir, name)
        if os.path.exists(target):
            continue
        src = os.path.join(bundle, name)
        if not os.path.exists(src):
            logger.info(f"[assets] bundled '{name}' not found at {src}")
            continue
        try:
            os.makedirs(os.path.dirname(target) or app_dir, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(target)
            logger.info(f"[assets] seeded {name} → {target}")
        except Exception as exc:
            logger.warning(f"[assets] could not copy {name}: {exc}")
    return copied
