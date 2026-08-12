"""
NodeCraft License Manager — Verification Module

Validates license keys signed by the admin keygen tool.
License format: base64 encoded JSON payload + Ed25519 signature.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import time
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# ── Public key (embedded) — can verify but CANNOT generate licenses ──
_PUBLIC_KEY_B64 = "2Nz/qcut2Ro3W0BFe3ErZ6HivtAqmwEzWPozavPJCxA="

LICENSE_FILE = "license.key"

# ── Anti-rollback (system-clock backdate) protection ─────────────────
# The expiry check trusts the local clock, so setting the PC date back is the
# easy bypass. We keep a small HMAC-protected "last seen" timestamp that only
# ever moves FORWARD; if the clock is later found earlier than that (beyond a
# tolerance) the license is refused. The HMAC secret is embedded (obfuscation,
# not real secrecy) and the record is bound to the hostname, so it can't be
# copied to another PC or hand-edited casually. This stops the common backdate;
# a determined attacker patching the binary is out of scope for client-side.
_STATE_FILE = ".ncstate"
_STATE_SECRET = hashlib.sha256(b"NodeCraft::anti-rollback::v1").digest()
_ROLLBACK_GRACE = timedelta(days=1)      # tolerate TZ/DST/clock jitter

# ── Per-feature licensing ────────────────────────────────────────
# Canonical feature keys. A license may enable any subset of these.
# The values here MUST match what the license generator writes into
# the "features" field of the payload and what the GUI checks with
# ``has_feature`` below.
FEATURE_INTEGRATION = "integration"
FEATURE_TERMINAL = "terminal"
FEATURE_AUDIT = "audit"
FEATURE_TRFS = "trfs"
FEATURE_CUTOVER = "cutover"

ALL_FEATURES = [
    FEATURE_INTEGRATION,
    FEATURE_TERMINAL,
    FEATURE_AUDIT,
    FEATURE_TRFS,
    FEATURE_CUTOVER,
]

# Human-friendly labels (used for messages / admin UI).
FEATURE_LABELS = {
    FEATURE_INTEGRATION: "Integration",
    FEATURE_TERMINAL: "Terminal",
    FEATURE_AUDIT: "CDD Audit",
    FEATURE_TRFS: "TRFS",
    FEATURE_CUTOVER: "Cut Over",
}


def get_enabled_features(payload: dict | None) -> set:
    """Return the set of feature keys enabled by a license payload.

    Backward compatibility: a license WITHOUT a ``features`` field
    (older keys, issued before per-feature licensing) grants *all*
    features. A license with ``features`` set to ``"*"`` or ``"all"``
    also grants everything. Otherwise only the listed features are
    enabled.
    """
    if not payload:
        return set()

    feats = payload.get("features")

    # Legacy license → full access.
    if feats is None:
        return set(ALL_FEATURES)

    # Wildcard string.
    if isinstance(feats, str):
        if feats.strip().lower() in ("*", "all"):
            return set(ALL_FEATURES)
        feats = [f for f in feats.replace(";", ",").split(",")]

    return {str(f).strip().lower() for f in feats if str(f).strip()}


def has_feature(payload: dict | None, feature: str) -> bool:
    """True if the given feature key is enabled by the license payload."""
    return feature.strip().lower() in get_enabled_features(payload)


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


_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _state_path() -> str:
    from app_path import get_app_dir
    return os.path.join(get_app_dir(), _STATE_FILE)


def _state_mac(host: str, ts: str) -> str:
    return hmac.new(_STATE_SECRET, f"{host}|{ts}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _load_last_seen():
    """The stored (HMAC-verified, host-bound) last-seen datetime, or None."""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            obj = json.loads(base64.b64decode(f.read().strip()).decode("utf-8"))
        host, ts, mac = obj.get("host"), obj.get("ts"), obj.get("mac")
        if host == get_hostname() and hmac.compare_digest(
                mac or "", _state_mac(host, ts or "")):
            return datetime.strptime(ts, _TS_FMT)
    except Exception:
        pass
    return None


def _save_last_seen(when: datetime) -> None:
    host, ts = get_hostname(), when.strftime(_TS_FMT)
    obj = {"host": host, "ts": ts, "mac": _state_mac(host, ts)}
    try:
        raw = base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        with open(_state_path(), "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception:
        pass


def check_clock(update: bool = True) -> tuple:
    """Detect a backdated system clock. Returns (ok, error).

    The stored last-seen only ratchets forward; a ``now`` earlier than it (by
    more than the grace window) means the clock was set back."""
    now = datetime.now()
    last = _load_last_seen()
    if last is not None and now < last - _ROLLBACK_GRACE:
        return False, (
            "System clock appears to be set back (now "
            f"{now.strftime('%Y-%m-%d')} is before last use "
            f"{last.strftime('%Y-%m-%d')}). Set the correct date/time and "
            "restart.")
    if update:
        _save_last_seen(last if (last and last > now) else now)
    return True, None


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

    result = verify_license(license_key)
    # Only enforce (and advance) the anti-rollback clock when the signature and
    # expiry are otherwise valid — a genuinely expired/invalid key is handled by
    # verify_license already.
    if result["valid"]:
        ok, err = check_clock(update=True)
        if not ok:
            return {"valid": False, "payload": result["payload"], "error": err}
    return result


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
