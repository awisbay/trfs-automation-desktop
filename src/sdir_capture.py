"""
Shared `sdir` capture and band detection commands.

`sdir` is expensive (runs ~4 minutes on a loaded node) and its output is
node-level, not band-level — every band that needs it can reuse the same
screenshot. This module runs sdir once per node, extracts the FRU table
that includes the VSWR column, renders it as a terminal screenshot, and
optionally returns the raw output for band detection.

Band detection commands (:func:`run_band_detection_commands`) are much faster
(~10 seconds) and use ``hgetc`` to query the node's Managed Object tree for
authoritative band/frequency information. These should be run **before** sdir
to determine which bands each node has.
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
    saw_data_row = False
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if not saw_bottom_eq:
            if set(stripped) == {"="} and len(stripped) >= 10:
                saw_bottom_eq = True
            continue
        # After the header's closing ===, the raw output often has extra
        # separator lines (---, another ===, blanks) BEFORE the actual data
        # rows. Only treat a --- as the end of the table once we've actually
        # seen at least one data row — otherwise we cut the screenshot off
        # before any rows are included.
        if not stripped:
            continue
        is_dash = set(stripped) == {"-"} and len(stripped) >= 10
        is_eq = set(stripped) == {"="} and len(stripped) >= 10
        if is_dash:
            if saw_data_row:
                end_idx = j + 1
                break
            continue  # pre-data separator, keep scanning
        if is_eq:
            continue  # extra separator, keep scanning
        # A real row
        saw_data_row = True

    block = lines[start_idx:end_idx]
    return "\n".join(line for line in block if line.strip())


def capture_shared_sdir_screenshot(config: AppConfig) -> Optional[str]:
    """
    Open one SSH session, run ``sdir`` once, and render the FRU+VSWR table
    as a screenshot. Returns the screenshot path, or None on failure.

    The raw sdir output is also saved to logs/ for audit.

    This is the original single-node version retained for backward
    compatibility.
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


def capture_sdir_for_node(
    config: AppConfig, node_name: str
) -> tuple[Optional[str], str]:
    """
    Run ``sdir`` on a specific node and return both the screenshot path
    and the raw sdir output (for band detection).

    Creates a per-node config that targets *node_name* instead of the
    original node configured in *config*.

    Args:
        config: Base application configuration.
        node_name: The node DN to connect to (e.g. ``MIN3117_P3ACANOCOTAGUMDDNB02``).

    Returns:
        ``(screenshot_path, raw_sdir_output)`` — screenshot_path is None
        on failure; raw_sdir_output is empty string on failure.
    """
    from config_loader import create_node_config

    # Build a config variant targeting the requested node
    node_config = create_node_config(config, node_name)

    screenshots_dir = get_full_path(node_config, node_config.paths.screenshots_dir)
    os.makedirs(screenshots_dir, exist_ok=True)

    logs_dir = os.path.join(node_config.base_dir, "logs", node_config.site.shortcode)
    os.makedirs(logs_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Per-node shortcode derived from the node DN itself (first underscore
    # segment). Base-config shortcode is wrong for multi-site runs — e.g.
    # node `MIN3592_MAGUWETAGUMDDNB01` on a run that started at site
    # `MIN3117` must still be tagged with `MIN3592` in its own filename
    # and title, otherwise Excel thumbnails look like they're from the
    # wrong site.
    shortcode = node_name.split("_")[0] if "_" in node_name else node_config.site.shortcode
    node_tag = node_name.split("_")[-1] if "_" in node_name else node_name[:8]
    raw_log_path = os.path.join(logs_dir, f"sdir_{shortcode}_{node_tag}_{timestamp}.txt")
    screenshot_path = os.path.join(
        screenshots_dir, f"{shortcode}_{node_tag}_sdir_VSWR.png"
    )

    output = ""
    try:
        logger.info(f"[sdir] Opening SSH session for node {node_name}...")
        with MoshellSession(node_config) as session:
            logger.info(f"[sdir] Running 'sdir' on {node_name} (timeout={SDIR_TIMEOUT_SEC}s)...")
            output = session.run_command("sdir", timeout=SDIR_TIMEOUT_SEC)
    except Exception as exc:
        logger.error(f"[sdir] SSH/sdir capture failed for {node_name}: {exc}", exc_info=True)

    if not output:
        return None, ""

    try:
        with open(raw_log_path, "w", encoding="utf-8") as f:
            f.write(output)
        logger.info(f"[sdir] Raw log saved: {raw_log_path} ({len(output)} chars)")
    except Exception as exc:
        logger.warning(f"[sdir] Could not save raw log: {exc}")

    table = extract_fru_vswr_table(output)
    if not table:
        logger.warning(
            f"[sdir] FRU+VSWR table not found in output for {node_name} "
            f"— screenshot skipped, but raw output is available for band detection"
        )
    else:
        try:
            render_terminal_screenshot(
                command="sdir",
                output=table,
                style=node_config.terminal_style,
                save_path=screenshot_path,
                title=f"{shortcode} ({node_tag}) - sdir (FRU / VSWR)",
            )
            logger.info(f"[sdir] Screenshot saved: {screenshot_path}")
        except Exception as exc:
            logger.error(f"[sdir] Failed to render screenshot: {exc}", exc_info=True)
            screenshot_path = None

    return screenshot_path, output


def run_band_detection_commands(
    config: AppConfig, node_name: str,
    session: MoshellSession = None,
) -> tuple[str, str]:
    """
    Run the two ``hgetc`` commands that produce authoritative band/frequency
    information for a node:

      1. ``hgetc ^eutrancell[FT]DD= freqBand$`` — LTE cells + freqBand number
      2. ``hgetc nrcelldu bandListManual`` — NR cells + band number

    These commands are fast (~10 seconds) and should be run **before** sdir
    to determine which bands each node has.

    If *session* is provided, uses the existing moshell session (saves the
    2-5 minute ``amos + lt all`` setup). Otherwise opens a new session.

    Args:
        config: Base application configuration.
        node_name: The node DN to connect to.
        session: Optional existing ``MoshellSession`` to reuse.

    Returns:
        ``(lte_output, nr_output)`` — raw command output strings.
        Either may be empty if the command fails.
    """
    from config_loader import create_node_config

    lte_output = ""
    nr_output = ""
    own_session = session is None

    if session is not None:
        # Use the provided session (no connect/login needed)
        try:
            logger.info(f"[band-detect] Running hgetc LTE freqBand on {node_name} (reusing session)...")
            lte_output = session.run_command("hgetc ^eutrancell[FT]DD= freqBand$")
            logger.info(f"[band-detect] LTE output ({len(lte_output)} chars)")

            logger.info(f"[band-detect] Running hgetc NR bandListManual on {node_name} (reusing session)...")
            nr_output = session.run_command("hgetc nrcelldu bandListManual")
            logger.info(f"[band-detect] NR output ({len(nr_output)} chars)")
        except Exception as exc:
            logger.error(f"[band-detect] hgetc failed on {node_name} (reusing session): {exc}", exc_info=True)
    else:
        # Open a dedicated session
        node_config = create_node_config(config, node_name)
        try:
            logger.info(f"[band-detect] Opening SSH session for {node_name}...")
            with MoshellSession(node_config) as sess:
                logger.info(f"[band-detect] Running hgetc LTE freqBand on {node_name}...")
                lte_output = sess.run_command("hgetc ^eutrancell[FT]DD= freqBand$")
                logger.info(f"[band-detect] LTE output ({len(lte_output)} chars)")

                logger.info(f"[band-detect] Running hgetc NR bandListManual on {node_name}...")
                nr_output = sess.run_command("hgetc nrcelldu bandListManual")
                logger.info(f"[band-detect] NR output ({len(nr_output)} chars)")
        except Exception as exc:
            logger.error(f"[band-detect] Failed on {node_name}: {exc}", exc_info=True)

    return lte_output, nr_output


def run_node_setup(
    config: AppConfig, node_name: str
) -> tuple[Optional[str], str, str, str, MoshellSession | None]:
    """
    Open ONE moshell session for a node and run hgetc + sdir in sequence.

    This avoids opening 3 separate sessions (each doing ``amos <node>`` +
    ``lt all`` which takes 2-5 minutes). Instead we open one session and
    reuse it for all Phase-0 commands, then return the session so the caller
    can reuse it for Phase-1 moshell commands too.

    Returns:
        ``(sdir_screenshot_path, sdir_raw, lte_output, nr_output, session)``
        - sdir_screenshot_path: path to VSWR screenshot PNG (or None)
        - sdir_raw: raw sdir command output
        - lte_output: raw hgetc LTE freqBand output
        - nr_output: raw hgetc NR bandListManual output
        - session: the open MoshellSession (caller must close it), or None on failure
    """
    from config_loader import create_node_config

    node_config = create_node_config(config, node_name)

    screenshots_dir = get_full_path(node_config, node_config.paths.screenshots_dir)
    os.makedirs(screenshots_dir, exist_ok=True)

    logs_dir = os.path.join(node_config.base_dir, "logs", node_config.site.shortcode)
    os.makedirs(logs_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Per-node shortcode from the DN's first underscore segment (e.g. MIN3592_MAGU... -> MIN3592)
    shortcode = node_name.split("_")[0] if "_" in node_name else node_config.site.shortcode
    node_tag = node_name.split("_")[-1] if "_" in node_name else node_name[:8]
    raw_log_path = os.path.join(logs_dir, f"sdir_{shortcode}_{node_tag}_{timestamp}.txt")
    screenshot_path = os.path.join(
        screenshots_dir, f"{shortcode}_{node_tag}_sdir_VSWR.png"
    )

    lte_output = ""
    nr_output = ""
    sdir_raw = ""

    try:
        logger.info(f"[node-setup] Opening SSH session for {node_name} (single session)...")
        session = MoshellSession(node_config)
        session.connect()
        session.login_moshell()
        logger.info(f"[node-setup] Session established for {node_name}")

        # ── hgetc commands (fast, ~10 seconds) ──
        logger.info(f"[node-setup] Running hgetc on {node_name}...")
        lte_output, nr_output = run_band_detection_commands(config, node_name, session=session)

        # ── sdir (slow, ~4 minutes) ──
        logger.info(f"[node-setup] Running sdir on {node_name} (timeout={SDIR_TIMEOUT_SEC}s)...")
        sdir_raw = session.run_command("sdir", timeout=SDIR_TIMEOUT_SEC)
        logger.info(f"[node-setup] sdir output ({len(sdir_raw)} chars)")

        # Save raw sdir log
        try:
            with open(raw_log_path, "w", encoding="utf-8") as f:
                f.write(sdir_raw)
            logger.info(f"[node-setup] Raw log saved: {raw_log_path}")
        except Exception as exc:
            logger.warning(f"[node-setup] Could not save raw log: {exc}")

        # Render screenshot
        table = extract_fru_vswr_table(sdir_raw)
        if table:
            try:
                render_terminal_screenshot(
                    command="sdir",
                    output=table,
                    style=node_config.terminal_style,
                    save_path=screenshot_path,
                    title=f"{shortcode} ({node_tag}) - sdir (FRU / VSWR)",
                )
                logger.info(f"[node-setup] Screenshot saved: {screenshot_path}")
            except Exception as exc:
                logger.error(f"[node-setup] Failed to render screenshot: {exc}", exc_info=True)
                screenshot_path = None
        else:
            logger.warning(f"[node-setup] FRU+VSWR table not found in sdir output for {node_name}")
            screenshot_path = None

        # Return the session — caller is responsible for closing it
        return screenshot_path or None, sdir_raw, lte_output, nr_output, session

    except Exception as exc:
        logger.error(f"[node-setup] Failed for {node_name}: {exc}", exc_info=True)
        return None, "", lte_output, nr_output, None


def run_node_setup_standalone(
    config: AppConfig, node_name: str
) -> tuple[Optional[str], str, str, str]:
    """Phase-0 worker safe for use in multiple threads concurrently.

    Opens its OWN SSH session, runs ``hgetc`` + ``sdir``, closes the
    session. Each caller thread gets an isolated MoshellSession which,
    together with the stale-AMOS guard in ``ssh_runner.connect()`` and
    the full teardown in ``disconnect()``, means two of these can safely
    run against the gateway in parallel.

    Returns:
        ``(sdir_screenshot_path, sdir_raw, lte_output, nr_output)``
        — screenshot_path is None on failure; raw strings are ``""`` on
        failure. NO session is returned; this function owns the
        session's entire lifecycle.
    """
    sdir_path, sdir_raw, lte_output, nr_output, session = run_node_setup(
        config, node_name
    )
    if session is not None:
        try:
            session.disconnect()
        except Exception as exc:
            logger.warning(f"[node-setup] disconnect failed for {node_name}: {exc}")
    return sdir_path, sdir_raw, lte_output, nr_output


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
