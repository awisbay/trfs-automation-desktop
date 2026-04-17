"""
Persist last-used form values to disk so they survive app restarts.
"""
import json
import os

from app_path import get_app_dir

_SESSION_FILE = os.path.join(get_app_dir(), ".session.json")

# All form fields persisted between sessions.
_SESSION_KEYS = (
    "host", "port", "username", "password",
    "shortcode",
    "node_name", "node_ip", "subnetwork",
    "node2_name", "node2_ip", "node2_subnetwork",
    "gsm_node_name", "bsc_name", "gsm_node_ip", "gsm_subnetwork",
    "commands_file", "lkf_file", "relation_file",
)


def load_session() -> dict:
    try:
        with open(_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: data.get(k, "") for k in _SESSION_KEYS}
    except Exception:
        return {}


def save_session(host: str = "", port=None, username: str = "",
                 password: str = "", **extra) -> None:
    """Save session. Accepts all _SESSION_KEYS via kwargs."""
    # Merge with existing file so partial saves don't wipe other keys.
    existing = {}
    try:
        with open(_SESSION_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        pass

    data = dict(existing)
    data.update({
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    })
    for k, v in extra.items():
        if k in _SESSION_KEYS:
            data[k] = v

    try:
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
