"""
NodeCraft License Manager — Verification Module

Validates license keys signed by the admin keygen tool.
License format: base64 encoded JSON payload + Ed25519 signature.
"""
import base64
import json
import os
import socket
import time
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# ── Public key (embedded) — can verify but CANNOT generate licenses ──
_PUBLIC_KEY_B64 = "2Nz/qcut2Ro3W0BFe3ErZ6HivtAqmwEzWPozavPJCxA="

LICENSE_FILE = "license.key"


def get_hostname() -> str:
    """Return the current PC hostname (normalized: lowercase, stripped)."""
    return socket.gethostname().strip().lower()


def _get_public_key() -> Ed25519PublicKey:
    pub_bytes = base64.b64decode(_PUBLIC_KEY_B64)
    return Ed25519PublicKey.from_public_bytes(pub_bytes)


def _get_license_path() -> str:
    """Return path to license.key next to the exe (or repo root when dev)."""
    from app_path import get_app_dir
    return os.path.join(get_app_dir(), LICENSE_FILE)


def verify_license(license_key: str) -> dict:
    """Verify a license key string.

    Args:
        license_key: The full license key string (base64).

    Returns:
        dict with keys:
            valid (bool): True if license is valid and not expired.
            payload (dict|None): The decoded payload if signature is valid.
            error (str|None): Error message if invalid.
    """
    try:
        raw = base64.b64decode(license_key.strip())
    except Exception:
        return {"valid": False, "payload": None, "error": "Invalid license format."}

    # Format: payload_json_bytes + b"|SIG|" + signature_bytes
    separator = b"|SIG|"
    if separator not in raw:
        return {"valid": False, "payload": None, "error": "Invalid license structure."}

    idx = raw.index(separator)
    payload_bytes = raw[:idx]
    signature = raw[idx + len(separator):]

    # Verify signature
    try:
        pub = _get_public_key()
        pub.verify(signature, payload_bytes)
    except Exception:
        return {"valid": False, "payload": None, "error": "License signature is invalid."}

    # Decode payload
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {"valid": False, "payload": None, "error": "License payload is corrupted."}

    # Check expiry
    expires = payload.get("expires")
    if expires:
        try:
            exp_date = datetime.strptime(expires, "%Y-%m-%d")
            if datetime.now() > exp_date:
                return {
                    "valid": False,
                    "payload": payload,
                    "error": f"License expired on {expires}.",
                }
        except ValueError:
            return {"valid": False, "payload": payload, "error": "Invalid expiry date in license."}

    # Check product
    if payload.get("product") != "NodeCraft":
        return {"valid": False, "payload": payload, "error": "License is not for this product."}

    # Check hostname (if present in license)
    license_hostname = payload.get("hostname")
    if license_hostname:
        current = get_hostname()
        if license_hostname.strip().lower() != current:
            return {
                "valid": False,
                "payload": payload,
                "error": (
                    f"License is bound to hostname '{license_hostname}', "
                    f"but this PC is '{current}'."
                ),
            }

    return {"valid": True, "payload": payload, "error": None}


def load_saved_license() -> dict:
    """Load and verify the saved license.key file.

    Returns:
        Same dict as verify_license, or error if file not found.
    """
    path = _get_license_path()
    if not os.path.isfile(path):
        return {"valid": False, "payload": None, "error": "No license file found."}

    try:
        with open(path, "r", encoding="utf-8") as f:
            license_key = f.read().strip()
    except Exception as e:
        return {"valid": False, "payload": None, "error": f"Cannot read license file: {e}"}

    if not license_key:
        return {"valid": False, "payload": None, "error": "License file is empty."}

    return verify_license(license_key)


def save_license(license_key: str) -> str:
    """Save a license key to disk.

    Returns:
        The path where the file was saved.
    """
    path = _get_license_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(license_key.strip())
    return path


def get_license_info() -> dict | None:
    """Get the current license info if valid, else None."""
    result = load_saved_license()
    if result["valid"]:
        return result["payload"]
    return None
