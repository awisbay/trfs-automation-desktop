"""
Persist last-used SSH session to disk so values survive app restarts.
"""
import json
import os

from app_path import get_app_dir

_SESSION_FILE = os.path.join(get_app_dir(), ".session.json")

_SESSION_KEYS = ("host", "port", "username", "password")


def load_session() -> dict:
    try:
        with open(_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: data.get(k, "") for k in _SESSION_KEYS}
    except Exception:
        return {}


def save_session(host: str, port, username: str, password: str) -> None:
    data = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }
    try:
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass