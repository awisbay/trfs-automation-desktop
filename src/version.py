"""
version.py — single source of truth for the NodeCraft app version.

Bump ``__version__`` (semantic versioning: MAJOR.MINOR.PATCH) whenever you ship
changes, and add a matching entry at the top of ``CHANGELOG.md``:

  * PATCH  — bug fixes / small tweaks, no new capability.
  * MINOR  — new feature or capability, backward compatible.
  * MAJOR  — breaking change / large redesign.

Everything that shows a version (window title, About, exe metadata) reads it
from here, so there is exactly one place to update.
"""

__version__ = "1.2.0"

# Human-friendly app name used in titles.
APP_NAME = "NodeCraft"


def version_string() -> str:
    """e.g. 'NodeCraft v1.1.0'."""
    return f"{APP_NAME} v{__version__}"
