"""
Shared `sdir` capture.

`sdir` is expensive (runs ~4 minutes on a loaded node) and its output is
node-level, not band-level — every band that needs it can reuse the same
screenshot. This module runs sdir exactly once per automation run, extracts
the FRU table that includes the VSWR column, and renders it as a terminal
screenshot via the existing renderer.
"""
import os
import logging
from datetime import datetime
from typing import Optional

from config_loader import AppConfig, get_full_path
from ssh_runner import MoshellSession
from terminal_renderer import render_terminal_screenshot

logger = logging.getLogger(__name__)

SDIR_TIMEOUT_SEC = 15 * 60  # 15 minutes


def extract_fru_vswr_table(output: str) -> str:
    """
    Extract the FRU-with-VSWR table block from sdir output.

    sdir prints multiple FRU tables. We want the one with the VSWR column:
        =====...=====
        FRU  ;LNH  ;BOARD  ;RF  ;BP  ;TX (W/dBm)  ;VSWR (RL)  ;RX (dBm) ...
        =====...=====
        <rows>
        -----...-----  (or EOF)

    Header and separators may be separated by blank lines; those blanks
    are collapsed so the rendered screenshot is tight.
    """
    lines = output.splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        if "FRU" in line and "VSWR" in line and ";" in line:
            header_idx = i
            break
    if header_idx is None:
        return ""

    start_idx = header_idx
    for i in range(header_idx - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if set(stripped) == {"="} and len(stripped) >= 10:
            start_idx = i
        break

    end_idx = len(lines)
    saw_bottom_eq = False
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if not saw_bottom_eq:
            if set(stripped) == {"="} and len(stripped) >= 10:
                saw_bottom_eq = True
            continue
        if stripped and set(stripped) == {"-"} and len(stripped) >= 10:
            end_idx = j + 1
            break

    block = lines[start_idx:end_idx]
    return "\n".join(line for line in block if line.strip())


def capture_shared_sdir_screenshot(config: AppConfig) -> Optional[str]:
    """
    Open one SSH session, run `sdir` once, and render the FRU+VSWR table
    as a screenshot. Returns the screenshot path, or None on failure.

    The raw sdir output is also saved to logs/ for audit.
    """
    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)
    os.makedirs(screenshots_dir, exist_ok=True)

    logs_dir = os.path.join(config.base_dir, "logs", config.site.shortcode)
    os.makedirs(logs_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shortcode = config.site.shortcode
    raw_log_path = os.path.join(logs_dir, f"sdir_{shortcode}_{timestamp}.txt")
    screenshot_path = os.path.join(
        screenshots_dir, f"{shortcode}_SHARED_sdir_VSWR.png"
    )

    try:
        logger.info("[sdir] Opening dedicated SSH session for shared sdir capture...")
        with MoshellSession(config) as session:
            logger.info(f"[sdir] Running 'sdir' (timeout={SDIR_TIMEOUT_SEC}s)...")
            output = session.run_command("sdir", timeout=SDIR_TIMEOUT_SEC)
    except Exception as exc:
        logger.error(f"[sdir] SSH/sdir capture failed: {exc}", exc_info=True)
        return None

    try:
        with open(raw_log_path, "w", encoding="utf-8") as f:
            f.write(output)
        logger.info(f"[sdir] Raw log saved: {raw_log_path} ({len(output)} chars)")
    except Exception as exc:
        logger.warning(f"[sdir] Could not save raw log: {exc}")

    table = extract_fru_vswr_table(output)
    if not table:
        logger.error(
            f"[sdir] FRU+VSWR table not found in output — check raw log: {raw_log_path}"
        )
        return None

    try:
        render_terminal_screenshot(
            command="sdir",
            output=table,
            style=config.terminal_style,
            save_path=screenshot_path,
            title=f"{shortcode} - sdir (FRU / VSWR)",
        )
        logger.info(f"[sdir] Shared screenshot saved: {screenshot_path}")
        return screenshot_path
    except Exception as exc:
        logger.error(f"[sdir] Failed to render screenshot: {exc}", exc_info=True)
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from config_loader import load_config
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    cfg = load_config(cfg_path)
    path = capture_shared_sdir_screenshot(cfg)
    sys.exit(0 if path else 2)
