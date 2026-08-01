"""
WhatsApp handoff for Cut Over evidence — semi-automatic.

Why semi-automatic, and not a real send:

  * The official **WhatsApp Cloud API cannot post to groups at all**. It only
    addresses individual phone numbers, so it cannot satisfy "send this to the
    ops group" no matter how it is configured.
  * Driving ``web.whatsapp.com`` with a browser would work, but it means
    putting Playwright + Chromium (~150 MB) back into a build that
    deliberately removed them, and automating WhatsApp Web is against its
    terms — a banned number in the middle of a cut-over is a bad trade.

So this module does the parts a program can do reliably — render the evidence,
put the image on the clipboard, bring WhatsApp up — and leaves the final
keystroke to the operator, who is sitting there anyway.

The PNG is always written to disk first, so the evidence survives even when
every convenience step below fails.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HandoffResult:
    ok: bool                  # did anything useful happen
    clipboard: bool = False   # is the image on the clipboard
    opened: bool = False      # did WhatsApp come up
    message: str = ""         # operator-facing summary
    error: str = ""


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


# ──────────────────────────────────────────────────────────────────
# Clipboard
# ──────────────────────────────────────────────────────────────────
def copy_image_to_clipboard(image_path: str) -> tuple:
    """Put a PNG on the clipboard as an *image*. Returns ``(ok, error)``.

    Uses PowerShell rather than pywin32 so no new dependency is added to the
    build. ``Set-Clipboard -Path`` is deliberately not used — that copies a
    file reference, which pastes into WhatsApp as a document attachment
    instead of an inline image.
    """
    if not os.path.isfile(image_path):
        return False, f"Image not found: {image_path}"

    if not _is_windows():
        return False, "Clipboard image copy is only implemented on Windows."

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        f"$img = [System.Drawing.Image]::FromFile('{image_path}'); "
        "[System.Windows.Forms.Clipboard]::SetImage($img); "
        "$img.Dispose()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return False, "PowerShell was not found on PATH."
    except subprocess.TimeoutExpired:
        return False, "PowerShell timed out while copying the image."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "PowerShell failed").strip()[:300]
    return True, ""


def copy_text_to_clipboard(text: str) -> bool:
    """Best-effort caption copy. Never raises."""
    if not text:
        return False
    try:
        if _is_windows():
            proc = subprocess.run(
                ["clip"], input=text, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode == 0
    except Exception:
        pass
    return False


# ──────────────────────────────────────────────────────────────────
# Opening WhatsApp
# ──────────────────────────────────────────────────────────────────
def open_whatsapp(group_link: str = "") -> tuple:
    """Bring WhatsApp up, at *group_link* when one is configured.

    Returns ``(ok, error)``.

    A caveat worth being honest about: WhatsApp Desktop has no documented
    deep link that opens a specific existing **group** by name. A
    ``chat.whatsapp.com`` invite link usually lands on the right conversation,
    but that is not guaranteed across versions — which is exactly why the
    operator still confirms and presses Enter.
    """
    target = (group_link or "").strip() or "whatsapp://"
    try:
        if _is_windows():
            os.startfile(target)          # noqa: S606 — intended shell handoff
            return True, ""
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, target],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ──────────────────────────────────────────────────────────────────
# The one call the engine/GUI uses
# ──────────────────────────────────────────────────────────────────
def send_image_semi_auto(image_path: str, caption: str = "",
                         group_link: str = "",
                         open_app: bool = True) -> HandoffResult:
    """Prepare a WhatsApp send and hand the last step to the operator.

    Steps, each independent so a failure degrades rather than aborts:

      1. Copy the PNG to the clipboard as an image.
      2. Open WhatsApp (at *group_link* when set).

    The caller then shows the operator a "paste and press Enter" prompt.
    """
    if not os.path.isfile(image_path):
        return HandoffResult(ok=False, error=f"Image not found: {image_path}",
                             message="Nothing to send — the screenshot is missing.")

    clip_ok, clip_err = copy_image_to_clipboard(image_path)
    if not clip_ok:
        logger.warning("Clipboard copy failed: %s", clip_err)

    opened, open_err = (False, "")
    if open_app:
        opened, open_err = open_whatsapp(group_link)
        if not opened:
            logger.warning("Could not open WhatsApp: %s", open_err)

    folder = os.path.dirname(image_path)
    if clip_ok and opened:
        message = ("Screenshot copied to the clipboard and WhatsApp opened.\n"
                   "Open the right group, press Ctrl+V, then Enter to send.")
    elif clip_ok:
        message = ("Screenshot copied to the clipboard.\n"
                   "Open WhatsApp, press Ctrl+V, then Enter to send.")
    else:
        message = ("The screenshot could not be copied to the clipboard, so "
                   f"attach it manually from:\n{image_path}")

    if caption:
        message += "\n\nSuggested caption:\n" + caption

    return HandoffResult(
        ok=clip_ok or opened,
        clipboard=clip_ok,
        opened=opened,
        message=message,
        error=clip_err or open_err,
    )


def open_containing_folder(path: str) -> bool:
    """Open the folder holding *path* so the operator can grab the file."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder or not os.path.isdir(folder):
        return False
    try:
        if _is_windows():
            os.startfile(folder)          # noqa: S606
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, folder],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
