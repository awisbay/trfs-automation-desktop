"""
Resolve the application root directory.

When running as a PyInstaller frozen exe, this is the folder containing
the exe.  When running from source, this is the project root (one level
above src/).
"""
import os
import sys


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller --onefile: exe lives here
        return os.path.dirname(sys.executable)
    # Running from source: src/ -> project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
