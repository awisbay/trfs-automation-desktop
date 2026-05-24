"""
TRFS Automation Tool - Main Orchestrator

Automates Ericsson baseband TRFS testing:
1. SSH into server -> login to moshell
2. Run commands per band frequency
3. Capture terminal output as screenshot images
4. Save screenshots into Excel files (one per band)
"""
import os
import sys
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from app_path import get_app_dir
from config_loader import load_config, get_full_path, AppConfig, _apply_shortcode_paths, create_node_config
from command_parser import parse_commands_file, get_moshell_categories, filter_gsm_pim
from ssh_runner import MoshellSession
from terminal_renderer import render_terminal_screenshot, render_multi_command_screenshot
from excel_writer import create_band_excel, insert_screenshots_for_band
from enm_capture import (
    prompt_and_capture,
    check_manual_screenshots,
    capture_cell_management_screenshots,
    capture_alarm_viewer_screenshot,
    capture_shm_software_administration_screenshot,
    EnmBrowserSession,
)
from sdir_capture import capture_shared_sdir_screenshot, capture_sdir_for_node, run_band_detection_commands, run_node_setup
from band_detector import detect_bands_from_sdir, detect_bands_from_hgetc, get_node_bands_from_sdir_map

# Setup logging
os.makedirs(os.path.join(get_app_dir(), "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(get_app_dir(), "logs", "trfs_automation.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_band_commands(
    session: MoshellSession,
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
    shared_sdir_screenshot: str | None = None,
    node_name: str = "",
) -> dict:
    """
    Run all moshell commands for a band and generate screenshots.

    Args:
        session: Active moshell session
        config: App configuration
        band: Band key (e.g., "L900")
        commands_by_category: Dict of category -> list of commands
        is_first_band: If True, use 'sdir'; if False, replace with 'sdi'

    Returns:
        Dict of category -> list of screenshot paths
    """
    screenshots = {}
    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)
    moshell_categories = get_moshell_categories()

    for category in moshell_categories:
        commands = commands_by_category.get(category, [])
        if not commands:
            logger.info(f"  [{band}] {category}: No commands, skipping")
            continue

        logger.info(f"  [{band}] {category}: Running {len(commands)} command(s)...")

        # Walk commands in order. Each command becomes one screenshot entry.
        # For sdir/sdi we reuse the shared capture (one-shot, node-level).
        cmd_entries: list[tuple[str, str | None]] = []  # (cmd, output_or_None)
        for cmd in commands:
            if cmd.strip().lower() in ("sdir", "sdi"):
                if shared_sdir_screenshot:
                    cmd_entries.append((cmd, None))  # marker: use shared path
                else:
                    logger.warning(
                        f"  [{band}] {category}: Shared sdir screenshot unavailable — skipping {cmd}"
                    )
                continue
            output = session.run_command(cmd)
            cmd_entries.append((cmd, output))

        if not cmd_entries:
            logger.info(f"  [{band}] {category}: No commands produced output, skipping screenshots")
            continue

        category_screenshots = []
        for idx, (cmd, output) in enumerate(cmd_entries):
            suffix = f"_{idx+1}" if len(cmd_entries) > 1 else ""
            if output is None:
                # Reuse the shared sdir screenshot as-is
                category_screenshots.append(shared_sdir_screenshot)
                continue
            # Stamp the node tag (last underscore-segment of the DN) into
            # the filename so parallel-node runs never collide on disk and
            # so each screenshot is traceable to the node it came from.
            # Example: `TCPHT_P3ACANOCOTAGUMDDNB01_L900_CELL.png`.
            if node_name:
                node_tag = node_name.split("_")[-1] if "_" in node_name else node_name[:12]
                # Per-node shortcode from the DN's first underscore segment
                sc = node_name.split("_")[0] if "_" in node_name else config.site.shortcode
                screenshot_path = os.path.join(
                    screenshots_dir,
                    f"{sc}_{node_tag}_{band}_{category}{suffix}.png",
                )
                title_sc = sc
            else:
                screenshot_path = os.path.join(
                    screenshots_dir,
                    f"{config.site.shortcode}_{band}_{category}{suffix}.png",
                )
                title_sc = config.site.shortcode
            render_terminal_screenshot(
                command=cmd,
                output=output,
                style=config.terminal_style,
                save_path=screenshot_path,
                title=f"{title_sc} - {band} - {category}",
            )
            category_screenshots.append(screenshot_path)

        screenshots[category] = category_screenshots

    return screenshots


def handle_enm_items(
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    enm_prompt=None,
) -> dict:
    """
    Handle ENM GUI screenshot items for a band.

    Args:
        config: App configuration
        band: Band key
        commands_by_category: Dict including ENM categories

    Returns:
        Dict of ENM category -> list of screenshot paths
    """
    screenshots = {}
    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)

    enm_categories = {k: v for k, v in commands_by_category.items() if k.endswith("_ENM")}

    # Separate items that still need the browser from ones already served
    # by pre-saved manual screenshots.
    pending: dict[str, list[str]] = {}
    for enm_key, enm_items in enm_categories.items():
        parent_category = enm_key.replace("_ENM", "")
        manual_screenshots = check_manual_screenshots(screenshots_dir, band, parent_category)
        if manual_screenshots:
            screenshots[enm_key] = manual_screenshots
        else:
            pending[enm_key] = enm_items

    # If nothing left to capture via browser, we are done.
    if not pending:
        return screenshots

    # If ENM is enabled, open ONE browser session and reuse it for every
    # pending category in this band. Login happens only once.
    browser_session = None
    if config.enm and config.enm.enabled:
        try:
            browser_session = EnmBrowserSession(config, debug_dir=screenshots_dir).__enter__()
        except Exception as e:
            logger.warning(f"[{band}] Failed to open ENM browser session: {e}")
            browser_session = None

    try:
        for enm_key, enm_items in pending.items():
            parent_category = enm_key.replace("_ENM", "")

            if browser_session is not None:
                if enm_key == "CELL_ENM":
                    save_base_path = os.path.join(
                        screenshots_dir,
                        f"{config.site.shortcode}_{band}_{parent_category}_ENM",
                    )
                    is_nr = band.upper().startswith("NR")
                    target_tab = "NR Cells" if is_nr else "LTE Cells"
                    try:
                        automated = browser_session.capture_cell_management(
                            save_base_path,
                            band=band,
                            tabs_override=[target_tab],
                        )
                        if automated:
                            screenshots[enm_key] = automated
                            continue
                    except Exception as e:
                        logger.warning(
                            f"[{band}] Cell Management capture failed: {e}"
                        )

                elif enm_key == "ALARM_ENM":
                    save_path = os.path.join(
                        screenshots_dir,
                        f"{config.site.shortcode}_{band}_{parent_category}_ENM_1.png",
                    )
                    try:
                        captured = browser_session.capture_alarm_viewer(save_path)
                        if captured:
                            screenshots[enm_key] = [captured]
                            continue
                    except Exception as e:
                        logger.warning(
                            f"[{band}] Alarm Viewer capture failed: {e}"
                        )

                elif enm_key == "NOC_Logs_ENM":
                    save_path = os.path.join(
                        screenshots_dir,
                        f"{config.site.shortcode}_{band}_{parent_category}_ENM_1.png",
                    )
                    try:
                        captured = browser_session.capture_shm_software_admin(save_path)
                        if captured:
                            screenshots[enm_key] = [captured]
                            continue
                    except Exception as e:
                        logger.warning(
                            f"[{band}] SHM capture failed: {e}"
                        )

            # Fallback: prompt user for each ENM item (GUI callback or terminal input)
            enm_screenshots = []
            for i, item in enumerate(enm_items):
                save_path = os.path.join(
                    screenshots_dir,
                    f"{config.site.shortcode}_{band}_{parent_category}_ENM_{i+1}.png",
                )
                try:
                    if enm_prompt is not None:
                        result = enm_prompt(item, save_path, band, parent_category)
                    else:
                        result = prompt_and_capture(item, save_path, band, parent_category)
                except EOFError:
                    logger.info(f"  [{band}] {enm_key}: Skipped (non-interactive mode)")
                    break
                if result:
                    enm_screenshots.append(result)

            if enm_screenshots:
                screenshots[enm_key] = enm_screenshots
    finally:
        if browser_session is not None:
            try:
                browser_session.__exit__(None, None, None)
            except Exception:
                pass

    return screenshots


def run_moshell_for_band(
    session: MoshellSession,
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
    shared_sdir_screenshot: str | None = None,
    node_name: str = "",
) -> tuple[str, dict]:
    """
    Phase 1 worker: create Excel file + run moshell commands for one band.
    Does NOT touch ENM (browser captures happen in a shared phase later).

    Returns:
        (excel_path, moshell_screenshots_dict)
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"[{band}] Moshell phase starting")
    logger.info(f"{'='*60}")
    excel_path = create_band_excel(config, band, node_name=node_name)
    moshell_screenshots = run_band_commands(
        session, config, band, commands_by_category, is_first_band,
        shared_sdir_screenshot=shared_sdir_screenshot,
        node_name=node_name,
    )
    return excel_path, moshell_screenshots


def capture_enm_shared_artifacts(config: AppConfig) -> dict:
    """
    Per-node ENM capture. Opens ONE Playwright browser session for the
    node described by *config* (config.site.node_name) and captures:

        - Cell Management  — tabs depend on tech:
              GSM node     -> ["GSM Cell"]           (1 screenshot)
              LTE/NR node  -> ["NR Cells","LTE Cells"] (2 screenshots)
        - Alarm Viewer     (1 screenshot)
        - SHM Software Administration (1 screenshot)

    All filenames are stamped with the **per-node** shortcode + node tag
    (derived from the DN's first and last underscore segments) so
    multi-node parallel runs never collide on disk, e.g.::

        MIN3592_MAGUWETAGUMDDNB01_SHARED_LTE_CELLS_ENM_2.png
        MIN3117_P3ACANOCOTAGUMDDNB02_SHARED_GSM_CELL_ENM_1.png
        MIN3117_P3ACANOCOTAGUMDDNB01_SHARED_ALARM_ENM_1.png

    Returns dict keyed by artifact (``shm``, ``alarm``, ``cellmgmt_nr``,
    ``cellmgmt_lte``, ``cellmgmt_gsm``) -> path or None on failure.
    ``build_enm_screenshots_for_band`` consumes those keys.
    """
    result = {
        "shm": None, "alarm": None,
        "cellmgmt_nr": None, "cellmgmt_lte": None, "cellmgmt_gsm": None,
    }

    if not config.enm or not config.enm.enabled:
        logger.info("[ENM] Skipped — enm disabled in config")
        return result

    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)
    os.makedirs(screenshots_dir, exist_ok=True)

    # Per-node shortcode + tag (matches run_band_commands / test_three_nodes_*).
    node_name = config.site.node_name
    if "_" in node_name:
        node_shortcode = node_name.split("_")[0]
        node_tag = node_name.split("_")[-1]
    else:
        node_shortcode = config.site.shortcode
        node_tag = node_name[:12] or "NODE"
    prefix = f"{node_shortcode}_{node_tag}"
    is_gsm_node = (
        bool(getattr(config.site, "gsm_node_name", ""))
        and node_name == config.site.gsm_node_name
    )
    tech_label = "GSM" if is_gsm_node else "LTE/NR"
    logger.info(f"[ENM] {node_name}: opening browser session (tech={tech_label})")

    try:
        session = EnmBrowserSession(config, debug_dir=screenshots_dir).__enter__()
    except Exception as e:
        logger.warning(f"[ENM] {node_name}: failed to open browser session: {e}")
        return result

    try:
        # ── 1. Cell Management ──────────────────────────────────────
        try:
            tabs = ["GSM Cell"] if is_gsm_node else ["NR Cells", "LTE Cells"]
            base_path = os.path.join(screenshots_dir, f"{prefix}_SHARED_CELL_ENM")
            captured = session.capture_cell_management(
                base_path, band=None, tabs_override=tabs,
            )
            # Rename index suffix -> include tab name so filenames are readable
            # and so Cell Mgmt captures don't collide between GSM and LTE/NR
            # runs that happen in the same screenshots folder.
            for idx, (tab, path) in enumerate(zip(tabs, captured), start=1):
                # `path` is None when that tab's capture failed — skip rename
                # and leave the corresponding result key as None. Sibling tabs
                # and the alarm/SHM captures that follow still proceed.
                if not path:
                    logger.warning(
                        f"[ENM] {node_name}: '{tab}' tab capture produced no "
                        f"file — leaving result slot empty"
                    )
                    continue
                safe_tab = tab.replace(" ", "_").upper()
                new_path = os.path.join(
                    screenshots_dir,
                    f"{prefix}_SHARED_{safe_tab}_ENM_{idx}.png",
                )
                try:
                    if os.path.abspath(path) != os.path.abspath(new_path):
                        if os.path.exists(new_path):
                            os.remove(new_path)
                        os.rename(path, new_path)
                except Exception as exc:
                    logger.warning(
                        f"[ENM] {node_name}: rename {path} -> {new_path} "
                        f"failed ({exc}); keeping original"
                    )
                    new_path = path
                if tab == "NR Cells":
                    result["cellmgmt_nr"] = new_path
                elif tab == "LTE Cells":
                    result["cellmgmt_lte"] = new_path
                elif tab == "GSM Cell":
                    result["cellmgmt_gsm"] = new_path
            logger.info(
                f"[ENM] {node_name}: Cell Management OK — "
                f"nr={bool(result['cellmgmt_nr'])} "
                f"lte={bool(result['cellmgmt_lte'])} "
                f"gsm={bool(result['cellmgmt_gsm'])}"
            )
        except Exception as e:
            logger.warning(f"[ENM] {node_name}: Cell Management failed: {e}")

        # ── 2. Alarm Viewer ─────────────────────────────────────────
        try:
            alarm_path = os.path.join(
                screenshots_dir, f"{prefix}_SHARED_ALARM_ENM_1.png"
            )
            result["alarm"] = session.capture_alarm_viewer(alarm_path)
            logger.info(f"[ENM] {node_name}: Alarm Viewer OK -> {result['alarm']}")
        except Exception as e:
            logger.warning(f"[ENM] {node_name}: Alarm Viewer failed: {e}")

        # ── 3. SHM Software Administration ──────────────────────────
        try:
            shm_path = os.path.join(
                screenshots_dir, f"{prefix}_SHARED_NOC_Logs_ENM_1.png"
            )
            result["shm"] = session.capture_shm_software_admin(shm_path)
            logger.info(f"[ENM] {node_name}: SHM OK -> {result['shm']}")
        except Exception as e:
            logger.warning(f"[ENM] {node_name}: SHM failed: {e}")
    finally:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass
        logger.info(f"[ENM] {node_name}: browser session closed")

    return result


def build_enm_screenshots_for_band(
    band: str,
    commands_by_category: dict,
    shared_artifacts: dict,
    config: AppConfig,
) -> dict:
    """
    Phase 3 helper: given the shared ENM artifacts and the ENM items a
    band actually needs, build a per-band ENM screenshots dict that can
    be merged into the final insert step.

    Manual pre-saved screenshots (band-specific files in screenshots_dir)
    still take priority over the shared cache.
    """
    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)
    out: dict = {}
    enm_categories = {k: v for k, v in commands_by_category.items() if k.endswith("_ENM")}

    for enm_key in enm_categories:
        parent_category = enm_key.replace("_ENM", "")

        # Manual override: if band has pre-saved files, use those and skip cache.
        manual = check_manual_screenshots(screenshots_dir, band, parent_category)
        if manual:
            out[enm_key] = manual
            continue

        if enm_key == "NOC_Logs_ENM" and shared_artifacts.get("shm"):
            out[enm_key] = [shared_artifacts["shm"]]
        elif enm_key == "ALARM_ENM" and shared_artifacts.get("alarm"):
            out[enm_key] = [shared_artifacts["alarm"]]
        elif enm_key == "CELL_ENM":
            is_nr = band.upper().startswith("NR")
            is_gsm = band.upper().startswith("G")
            if is_gsm:
                key = "cellmgmt_gsm"
            elif is_nr:
                key = "cellmgmt_nr"
            else:
                key = "cellmgmt_lte"
            if shared_artifacts.get(key):
                out[enm_key] = [shared_artifacts[key]]

    return out


def process_band(
    session: MoshellSession,
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
    enm_prompt=None,
) -> str:
    """
    Process a single band: run commands, capture screenshots, create Excel.

    Args:
        session: Active moshell session
        config: App configuration
        band: Band key
        commands_by_category: Dict of category -> commands
        is_first_band: If True, use 'sdir'; if False, replace with 'sdi'

    Returns:
        Path to generated Excel file
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Processing band: {band}")
    logger.info(f"{'='*60}")

    # Step 1: Create Excel file from template
    excel_path = create_band_excel(config, band)

    # Step 2: Run moshell commands and capture screenshots
    moshell_screenshots = run_band_commands(session, config, band, commands_by_category, is_first_band)

    # Step 3: Handle ENM GUI screenshots (best-effort, won't block)
    try:
        enm_screenshots = handle_enm_items(config, band, commands_by_category, enm_prompt=enm_prompt)
    except Exception as e:
        logger.warning(f"ENM screenshots skipped: {e}")
        enm_screenshots = {}

    # Step 4: Merge all screenshots and insert into Excel
    all_screenshots = {**moshell_screenshots, **enm_screenshots}

    # Defensive: if the Excel file disappeared between create and insert
    # (Windows Defender, manual delete, antivirus, etc.) re-create it from
    # the template so we never lose the run's work.
    if not os.path.exists(excel_path):
        logger.warning(f"Excel file vanished before insert — re-creating from template: {excel_path}")
        excel_path = create_band_excel(config, band)

    try:
        insert_screenshots_for_band(excel_path, all_screenshots)
    except FileNotFoundError:
        logger.warning(f"Excel file missing at insert time — re-creating and retrying: {excel_path}")
        excel_path = create_band_excel(config, band)
        insert_screenshots_for_band(excel_path, all_screenshots)

    logger.info(f"Completed band {band}: {excel_path}")
    return excel_path


###############################################################################
# DEMO MODE - simulate without SSH
###############################################################################

SAMPLE_OUTPUTS = {
    "VSWR": {
        "sdir": (
            "=====================================================================================================================================\n"
            "FRU          ;LNH      ;BOARD                  ;RF  ;BP  ;TX (W/dBm)  ;VSWR (RL)   ;RX (dBm) ;UEs/gUEs  ;Sector/AntennaGroup/Cells (State:CellIds:PCIs)\n"
            "=====================================================================================================================================\n"
            "AAS_B41_RRU1 ;fru_2054 ;AIR6419B41             ; A  ;11  ;-           ;-           ;         ;34/4      ;SE=AAS_B41_S1 TDD=... (1:31:398, 1:41:398, 1:401:398)\n"
            "AAS_B41_RRU2 ;fru_2055 ;AIR6419B41             ; A  ;11  ;-           ;-           ;         ;35/4      ;SE=AAS_B41_S2 TDD=... (1:32:397, 1:42:397, 1:402:397)\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; A  ;11  ;51.1 (47.1) ;1.14 (23.5) ;-77.0    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=... (1:171:398, 1:121:398, 1:501:398)\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; B  ;11  ;42.8 (46.3) ;1.15 (22.9) ;-77.8    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; C  ;11  ;33.2 (45.2) ;1.17 (22.3) ;-84.9    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; D  ;11  ;33.2 (45.2) ;1.36 (16.3) ;-84.8    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; A  ;11  ;33.5 (45.2) ;1.15 (22.9) ;-93.3    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; B  ;11  ;32.6 (45.1) ;1.15 (23.3) ;-93.3    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; C  ;11  ;12.4 (41.0) ;1.29 (18.0) ;-92.2    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; D  ;11  ;12.3 (40.9) ;1.15 (23.2) ;-93.8    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU3   ;BXP_7    ;RRU449944B0A44B28C*    ; A  ;11  ;30.3 (44.8) ;1.14 (23.6) ;-89.2    ;9/1       ;SE=B0B28_S3* AG=B0B28_S3 FDD=...\n"
            "-------------------------------------------------------------------------------------------------------------------------------------"
        ),
        "sdi": (
            "=====================================================================================================================================\n"
            "FRU          ;LNH      ;BOARD                  ;RF  ;BP  ;TX (W/dBm)  ;VSWR (RL)   ;RX (dBm) ;UEs/gUEs  ;Sector/AntennaGroup/Cells (State:CellIds:PCIs)\n"
            "=====================================================================================================================================\n"
            "AAS_B41_RRU1 ;fru_2054 ;AIR6419B41             ; A  ;11  ;-           ;-           ;         ;34/4      ;SE=AAS_B41_S1 TDD=... (1:31:398, 1:41:398, 1:401:398)\n"
            "AAS_B41_RRU2 ;fru_2055 ;AIR6419B41             ; A  ;11  ;-           ;-           ;         ;35/4      ;SE=AAS_B41_S2 TDD=... (1:32:397, 1:42:397, 1:402:397)\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; A  ;11  ;51.1 (47.1) ;1.14 (23.5) ;-77.0    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=... (1:171:398, 1:121:398, 1:501:398)\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; B  ;11  ;42.8 (46.3) ;1.15 (22.9) ;-77.8    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; C  ;11  ;33.2 (45.2) ;1.17 (22.3) ;-84.9    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU1   ;BXP_2    ;RRU449944B0A44B28C*    ; D  ;11  ;33.2 (45.2) ;1.36 (16.3) ;-84.8    ;19/-      ;SE=B0B28_S1* AG=B0B28_S1 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; A  ;11  ;33.5 (45.2) ;1.15 (22.9) ;-93.3    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; B  ;11  ;32.6 (45.1) ;1.15 (23.3) ;-93.3    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; C  ;11  ;12.4 (41.0) ;1.29 (18.0) ;-92.2    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU2   ;BXP_5    ;RRU449944B0A44B28C*    ; D  ;11  ;12.3 (40.9) ;1.15 (23.2) ;-93.8    ;22/1      ;SE=B0B28_S2* AG=B0B28_S2 FDD=...\n"
            "B0B28_RRU3   ;BXP_7    ;RRU449944B0A44B28C*    ; A  ;11  ;30.3 (44.8) ;1.14 (23.6) ;-89.2    ;9/1       ;SE=B0B28_S3* AG=B0B28_S3 FDD=...\n"
            "-------------------------------------------------------------------------------------------------------------------------------------"
        ),
        "default": (
            "Proxy                                    vswrSupervisionSensitivity\n"
            "RfPort=A                                 5000\n"
            "RfPort=B                                 5000\n"
            "RfPort=C                                 5000\n"
            "RfPort=D                                 5000\n"
            "\n4 MOs found\n"
        ),
    },
    "CELL": {
        "st": (
            " Proxy(MO)                                            AdmState  OpState  AvailStatus\n"
            " EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-121             UNLOCKED ENABLED  null\n"
            " EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-122             UNLOCKED ENABLED  null\n"
            " EUtranCellFDD=TCPHTP3ACANOCOTAGUMDDNY-123             UNLOCKED ENABLED  null\n"
            "\n3 MOs found\n"
        ),
    },
    "NOC_Logs": {
        "hget": (
            " Proxy                              dlChannelBandwidth  ulChannelBandwidth  earfcndl\n"
            " EUtranCellFDD=Y-121                5000                5000                3749\n"
            " EUtranCellFDD=Y-122                5000                5000                3749\n"
            " EUtranCellFDD=Y-123                5000                5000                3749\n"
            "\n3 MOs found\n"
        ),
        "lpr": (
            "EUtranCellRelation  cellIndividualOffsetEUtran  coverageIndicator  isHoAllowed  isRemoveAllowed\n"
            "TCPHTP3A-121->122   0                           1                 true         true\n"
            "TCPHTP3A-121->123   0                           1                 true         true\n"
            "TCPHTP3A-122->121   0                           1                 true         true\n"
            "\n3 MOs found\n"
        ),
        "cvcu": (
            "CV                         Date              Type      Status\n"
            "CXP9024418/20_R56A14       2026-04-08 14:22  SYSTEM    OK\n"
            "CXP9024418/20_R56A14       2026-04-09 02:00  SCHEDULED OK\n"
        ),
    },
    "ALARM": {
        "alt": (
            "=============  ACTIVE ALARMS  =============\n"
            "*** No Active alarms ***\n"
        ),
    },
    "PIM": {
        "pmxhetd": (
            "EUtranCellFDD                          Int_RadioRecInterferencePwr\n"
            "TCPHTP3ACANOCOTAGUMDDNY-121            -120.5\n"
            "TCPHTP3ACANOCOTAGUMDDNY-122            -119.8\n"
            "TCPHTP3ACANOCOTAGUMDDNY-123            -121.2\n"
        ),
    },
    "RET": {
        "hgetc": (
            "RetSubUnit    electricalAntennaTilt  operationalState  userLabel\n"
            "RetSubUnit=1  450                    ENABLED           Sector1-B0\n"
            "RetSubUnit=2  300                    ENABLED           Sector2-B0\n"
            "RetSubUnit=3  600                    ENABLED           Sector3-B0\n"
            "\n3 MOs found\n"
        ),
    },
}


def _get_sample_output(category: str, command: str) -> str:
    """Get sample output for a command in demo mode."""
    cat_samples = SAMPLE_OUTPUTS.get(category, {})
    # Try exact command match first
    for key, output in cat_samples.items():
        if key in command:
            return output
    # Fallback to 'default' or first available
    if "default" in cat_samples:
        return cat_samples["default"]
    if cat_samples:
        return list(cat_samples.values())[0]
    return f"(sample output for: {command})\n"


def process_band_demo(
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
) -> str:
    """Process a band in demo mode with sample outputs."""
    logger.info(f"\n{'='*60}")
    logger.info(f"[DEMO] Processing band: {band}")
    logger.info(f"{'='*60}")

    # Step 1: Create Excel from template
    excel_path = create_band_excel(config, band)

    # Step 2: Generate screenshots from sample outputs
    screenshots = {}
    screenshots_dir = get_full_path(config, config.paths.screenshots_dir)
    moshell_categories = get_moshell_categories()

    for category in moshell_categories:
        commands = commands_by_category.get(category, [])
        if not commands:
            logger.info(f"  [{band}] {category}: No commands, skipping")
            continue

        logger.info(f"  [{band}] {category}: Generating {len(commands)} screenshot(s)...")

        cmd_outputs = []
        for cmd in commands:
            actual_cmd = cmd
            if cmd.strip().lower() == "sdir" and not is_first_band:
                actual_cmd = "sdi"
                logger.info(f"  [{band}] {category}: Using sdi (sector dir already loaded)")
            output = _get_sample_output(category, actual_cmd)
            cmd_outputs.append((actual_cmd, output))

        screenshot_path = os.path.join(
            screenshots_dir,
            f"{config.site.shortcode}_{band}_{category}.png",
        )

        if len(cmd_outputs) == 1:
            render_terminal_screenshot(
                command=cmd_outputs[0][0],
                output=cmd_outputs[0][1],
                style=config.terminal_style,
                save_path=screenshot_path,
                title=f"{config.site.shortcode} - {band} - {category}",
            )
        else:
            render_multi_command_screenshot(
                commands_outputs=cmd_outputs,
                style=config.terminal_style,
                save_path=screenshot_path,
                title=f"{config.site.shortcode} - {band} - {category}",
            )

        screenshots[category] = [screenshot_path]
        print(f"  [OK] {band}/{category} -> {screenshot_path}")

    # Step 3: Insert into Excel (skip ENM in demo)
    insert_screenshots_for_band(excel_path, screenshots)

    logger.info(f"[DEMO] Completed band {band}: {excel_path}")
    return excel_path


def run_multi_node(
    config: AppConfig,
    all_commands: dict,
    band_filter: list[str] | None = None,
    enm_prompt=None,
    node_band_map: dict[str, list[str]] | None = None,
) -> list[str]:
    """
    Multi-node TRFS orchestrator with automatic band detection.

    Phase 0: Run ``hgetc`` band detection commands on each node (fast, ~10s).
    Phase 0b: Run ``sdir`` on each node (slow, ~4 min) for VSWR screenshots.
    Phase 1: Run moshell commands per band (on the correct node).
    Phase 2: Capture shared ENM artifacts per node.
    Phase 3: Distribute ENM screenshots and insert into Excel.

    If ``node_band_map`` is supplied, it overrides hgetc/sdir-based band
    detection entirely — each node processes exactly the bands listed for it.
    sdir still runs (for VSWR screenshots) and the GSM guard + dedupe steps
    still apply as safety nets.

    Returns list of generated Excel file paths.
    """
    shortcode = config.site.shortcode
    lte_nr_nodes = config.site.get_node_names()
    gsm_node = config.site.gsm_node_name if config.site.gsm_node_name else None

    # LTE/NR nodes: hgetc detection + sdir
    # GSM node: sdir + moshell commands (all GSM bands on that node)
    all_band_keys = list(all_commands.keys())

    # Collect all nodes that need processing (LTE/NR + GSM)
    sdir_nodes = list(lte_nr_nodes)
    if gsm_node:
        sdir_nodes.append(gsm_node)

    node_bands: dict[str, list[str]] = {}
    sdir_screenshots: dict[str, str | None] = {}

    # ── Phase 0: Open ONE SSH session per node, run hgetc + sdir ─────────
    # This avoids opening multiple sessions per node (each doing amos + lt all
    # which takes 2-5 minutes). One session = one amos + one lt all.
    print(f"\n{'='*60}")
    print(f"  Phase 0: Detecting bands + sdir per node (single SSH session)")
    print(f"  Nodes: {', '.join(sdir_nodes)}")
    print(f"{'='*60}\n")

    node_sessions: dict[str, MoshellSession | None] = {}

    for node_name in sdir_nodes:
        print(f"  [{node_name}] Opening SSH session...")
        logger.info(f"[multi-node] Opening single session for {node_name} (hgetc + sdir + moshell)")

        try:
            sdir_path, sdir_raw, lte_output, nr_output, session = run_node_setup(config, node_name)
        except Exception as exc:
            logger.error(f"[multi-node] Node setup failed for {node_name}: {exc}", exc_info=True)
            print(f"  [ERROR] {node_name}: setup failed — {exc}")
            # Try standalone sdir as last resort
            try:
                sdir_path, sdir_raw = capture_sdir_for_node(config, node_name)
            except Exception:
                sdir_path, sdir_raw = None, ""
            lte_output, nr_output, session = "", "", None

        sdir_screenshots[node_name] = sdir_path
        node_sessions[node_name] = session

        # ── Band detection: try hgetc first, fall back to sdir ──────────
        is_gsm = gsm_node and node_name == gsm_node

        if lte_output or nr_output:
            detection = detect_bands_from_hgetc(lte_output, nr_output)
            detected = detection.detected_bands
            node_band_list = [b for b in detected if b in all_commands]

            if node_band_list:
                node_bands[node_name] = node_band_list
                print(f"  [OK] {node_name}: hgetc detected bands = {', '.join(detected)}")
                print(f"       matching commands = {', '.join(node_band_list)}")
                if detection.cell_prefixes_found:
                    prefix_detail = ", ".join(
                        f"{k}- = {v}" for k, v in detection.cell_prefixes_found.items()
                    )
                    print(f"       cell prefixes: {prefix_detail}")
                # Print band number mapping for transparency
                for line in lte_output.splitlines():
                    stripped = line.strip()
                    if "EUtranCell" in stripped and ";" in stripped:
                        print(f"       {stripped}")
                for line in nr_output.splitlines():
                    stripped = line.strip()
                    if "NRCellDU" in stripped and ";" in stripped:
                        print(f"       {stripped}")

        # If hgetc didn't work, try sdir detection
        if node_name not in node_bands:
            if sdir_raw:
                detection = detect_bands_from_sdir(sdir_raw)
                detected = detection.detected_bands
                node_band_list = [b for b in detected if b in all_commands]
                if not node_band_list and not detection.cell_prefixes_found:
                    if is_gsm:
                        node_band_list = [b for b in all_band_keys if b.upper().startswith("G")]
                    else:
                        node_band_list = all_band_keys
                node_bands[node_name] = node_band_list

                print(f"  [OK] {node_name}: sdir detected bands = {', '.join(detected)}")
                print(f"       matching commands = {', '.join(node_band_list)}")
                if detection.cell_prefixes_found:
                    prefix_detail = ", ".join(
                        f"{k}- = {v}" for k, v in detection.cell_prefixes_found.items()
                    )
                    print(f"       cell prefixes: {prefix_detail}")
                if detection.rru_names:
                    print(f"       RRUs found: {', '.join(detection.rru_names)}")
            else:
                # Both hgetc and sdir failed — use fallback bands
                if is_gsm:
                    node_bands[node_name] = [b for b in all_band_keys if b.upper().startswith("G")]
                else:
                    node_bands[node_name] = all_band_keys
                print(f"  [WARN] {node_name}: all detection failed — falling back to all bands")
                logger.warning(f"[multi-node] All detection failed on {node_name}, using all bands as fallback")

    # Hard constraint: GSM node can only process G-prefixed bands.
    if gsm_node and gsm_node in node_bands:
        node_bands[gsm_node] = [
            b for b in node_bands[gsm_node] if b.upper().startswith("G")
        ]
        print(f"  [gsm-guard] {gsm_node}: restricted to G-bands = "
              f"{', '.join(node_bands[gsm_node]) or '(none)'}")

    # Apply band filter if specified
    if band_filter:
        filter_set = set(b.strip().upper() for b in band_filter)
        for node_name in sdir_nodes:
            node_bands[node_name] = [
                b for b in node_bands[node_name] if b.upper() in filter_set
            ]
            print(f"  [filter] {node_name}: filtered to {', '.join(node_bands[node_name])}")

    # Dedupe bands across nodes: each band must be owned by exactly one node,
    # otherwise two parallel Phase-1 workers write to the same Excel file
    # and Phase-3 stacks duplicate screenshots into the workbook. Priority
    # follows sdir_nodes order (node1 wins over node2, etc.). GSM-only bands
    # still belong to the GSM node thanks to the GSM-guard above.
    _claimed_bands: set[str] = set()
    for node_name in sdir_nodes:
        kept = []
        for b in node_bands.get(node_name, []):
            key = b.upper()
            if key in _claimed_bands:
                print(f"  [dedupe] {node_name}: dropping {b} (already owned by earlier node)")
                continue
            _claimed_bands.add(key)
            kept.append(b)
        node_bands[node_name] = kept

    # Remove GSM bands that have no commands, but process those that do.
    # PIM category is skipped for GSM bands (BSC access not yet available).
    for node_name in sdir_nodes:
        processed_bands = []
        for band in node_bands.get(node_name, []):
            if band.upper().startswith("G"):
                commands = all_commands.get(band, {})
                if not commands:
                    print(f"  [SKIP] {band} on {node_name} — no commands defined")
                    logger.info(f"[{band}] Skipped — no commands defined")
                    continue
                # Filter out PIM for GSM (BSC not yet available)
                filtered = filter_gsm_pim(band, commands)
                all_commands[band] = filtered
            processed_bands.append(band)
        node_bands[node_name] = processed_bands

    # Build flat list of (node_name, band) pairs ordered for processing
    node_band_pairs: list[tuple[str, str]] = []
    for node_name in sdir_nodes:
        for band in node_bands.get(node_name, []):
            node_band_pairs.append((node_name, band))

    if not node_band_pairs:
        print("\n  No bands to process. Exiting.")
        return []

    print(f"\n  Band assignment:")
    for node_name in sdir_nodes:
        print(f"    {node_name}: {', '.join(node_bands.get(node_name, [])) or '(none)'}")
    print()

    # ── Phase 1: Moshell SSH per node — reuse sessions from Phase 0 when available ─
    phase1_results: dict[str, tuple[str, dict]] = {}

    print(f"{'='*60}")
    print(f"  Phase 1: Moshell SSH commands")
    print(f"{'='*60}\n")

    for node_name in sdir_nodes:
        bands_for_node = node_bands.get(node_name, [])
        if not bands_for_node:
            continue

        node_config = create_node_config(config, node_name)
        shared_sdir = sdir_screenshots.get(node_name)
        session = node_sessions.get(node_name)

        # Try to reuse the session from Phase 0 (saves 2-5 min of amos + lt all)
        if session is not None:
            logger.info(f"[{node_name}] Reusing SSH session from Phase 0 for {len(bands_for_node)} band(s)")
            for i, band in enumerate(bands_for_node):
                commands = all_commands.get(band, {})
                if not commands:
                    continue
                try:
                    excel_path, moshell = run_moshell_for_band(
                        session,
                        node_config,
                        band,
                        commands,
                        is_first_band=(i == 0),
                        shared_sdir_screenshot=shared_sdir,
                    )
                    phase1_results[f"{node_name}::{band}"] = (excel_path, moshell)
                    print(f"  [OK phase1] {node_name}/{band}")
                except Exception as e:
                    logger.error(f"[{node_name}/{band}] Phase 1 error: {e}", exc_info=True)
                    print(f"  [ERROR phase1] {node_name}/{band}: {e}")
            # Close the reused session since we're done with it
            try:
                session.disconnect()
            except Exception:
                pass
            node_sessions[node_name] = None
        else:
            # Open a new session for this node
            logger.info(f"[{node_name}] Opening new SSH session for {len(bands_for_node)} band(s)")
            try:
                with MoshellSession(node_config) as new_session:
                    for i, band in enumerate(bands_for_node):
                        commands = all_commands.get(band, {})
                        if not commands:
                            continue
                        try:
                            excel_path, moshell = run_moshell_for_band(
                                new_session,
                                node_config,
                                band,
                                commands,
                                is_first_band=(i == 0),
                                shared_sdir_screenshot=shared_sdir,
                            )
                            phase1_results[f"{node_name}::{band}"] = (excel_path, moshell)
                            print(f"  [OK phase1] {node_name}/{band}")
                        except Exception as e:
                            logger.error(f"[{node_name}/{band}] Phase 1 error: {e}", exc_info=True)
                            print(f"  [ERROR phase1] {node_name}/{band}: {e}")
            except Exception as e:
                logger.error(f"[{node_name}] Phase 1 SSH session failed: {e}", exc_info=True)
                print(f"  [ERROR phase1] {node_name}: {e}")

    # ── Phase 2: Shared ENM artifacts per node (parallel across nodes) ─
    print(f"\n{'='*60}")
    print(f"  Phase 2: ENM capture (parallel per node)")
    print(f"{'='*60}\n")

    enm_artifacts_per_node: dict[str, dict] = {}

    def _phase2_one_node(node_name: str) -> tuple[str, dict]:
        node_bands_list = node_bands.get(node_name, [])
        if not node_bands_list:
            return node_name, {
                "shm": None, "alarm": None,
                "cellmgmt_nr": None, "cellmgmt_lte": None, "cellmgmt_gsm": None,
            }
        node_config = create_node_config(config, node_name)
        try:
            shared_enm = capture_enm_shared_artifacts(node_config)
        except Exception as e:
            logger.error(f"[{node_name}] Phase 2 ENM failed: {e}", exc_info=True)
            print(f"  [ERROR phase2] {node_name}: {e}")
            shared_enm = {
                "shm": None, "alarm": None,
                "cellmgmt_nr": None, "cellmgmt_lte": None, "cellmgmt_gsm": None,
            }
        nr_path = shared_enm.get("cellmgmt_nr")
        lte_path = shared_enm.get("cellmgmt_lte")
        gsm_path = shared_enm.get("cellmgmt_gsm")
        print(f"  [OK phase2] {node_name} ENM: NR={nr_path is not None}, LTE={lte_path is not None}, GSM={gsm_path is not None}")
        return node_name, shared_enm

    if sdir_nodes:
        with ThreadPoolExecutor(max_workers=max(1, len(sdir_nodes))) as _p2pool:
            for n, art in _p2pool.map(_phase2_one_node, sdir_nodes):
                enm_artifacts_per_node[n] = art

    # ── Phase 3: Distribute ENM + insert into Excel ─────────────────────
    print(f"\n{'='*60}")
    print(f"  Phase 3: Insert screenshots into Excel")
    print(f"{'='*60}\n")

    generated_files = []
    for key, (excel_path, moshell) in phase1_results.items():
        node_name, band = key.split("::", 1)
        commands = all_commands.get(band, {})
        shared_enm = enm_artifacts_per_node.get(node_name, {})

        try:
            enm_for_band = build_enm_screenshots_for_band(
                band, commands, shared_enm, config
            )
            all_screenshots = {**moshell, **enm_for_band}

            if not os.path.exists(excel_path):
                logger.warning(f"[{band}] Excel vanished — re-creating")
                excel_path = create_band_excel(config, band, node_name=node_name)

            try:
                insert_screenshots_for_band(excel_path, all_screenshots)
            except FileNotFoundError:
                logger.warning(f"[{band}] Excel missing at insert — re-creating")
                excel_path = create_band_excel(config, band, node_name=node_name)
                insert_screenshots_for_band(excel_path, all_screenshots)

            generated_files.append(excel_path)
            print(f"  [OK phase3] {node_name}/{band} -> {excel_path}")
        except Exception as e:
            logger.error(f"[{node_name}/{band}] Phase 3 error: {e}", exc_info=True)
            print(f"  [ERROR phase3] {node_name}/{band}: {e}")

    return generated_files


def main():
    parser = argparse.ArgumentParser(description="TRFS Automation Tool")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse commands and show what would be executed (no SSH connection)",
    )
    parser.add_argument(
        "--band",
        help="Process only specific bands. Comma-separated for multiple (e.g., L900 or NR700,L1800,L2600)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Demo mode: simulate with sample output, no SSH needed",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="Process N bands in parallel (each gets its own SSH session). Default: 0 = auto (all non-GSM bands concurrently)",
    )
    parser.add_argument(
        "--node2",
        default="",
        help="Second LTE/NR node name for multi-node TRFS (overrides config)",
    )
    args = parser.parse_args()

    # Load configuration
    config_path = os.path.abspath(args.config)
    logger.info(f"Loading config from: {config_path}")
    config = load_config(config_path)
    _apply_shortcode_paths(config)

    # Override node2 name from CLI if provided
    if args.node2:
        config.site.node2_name = args.node2

    # Parse commands
    commands_file = get_full_path(config, config.paths.commands)
    logger.info(f"Parsing commands from: {commands_file}")
    all_commands = parse_commands_file(commands_file)

    logger.info(f"Found {len(all_commands)} bands: {', '.join(all_commands.keys())}")

    # Filter to specific band if requested
    if args.band:
        requested = [b.strip().upper() for b in args.band.split(",") if b.strip()]
        missing = [b for b in requested if b not in all_commands]
        if missing:
            logger.error(f"Band(s) not found: {', '.join(missing)}. Available: {', '.join(all_commands.keys())}")
            sys.exit(1)
        bands_to_process = {b: all_commands[b] for b in requested}
    else:
        bands_to_process = all_commands

    # Dry run mode - just show parsed commands
    if args.dry_run:
        print("\n=== DRY RUN MODE ===\n")
        for band, categories in bands_to_process.items():
            print(f"\nBand: {band}")
            print("-" * 40)
            for cat, cmds in categories.items():
                print(f"  {cat}:")
                for cmd in cmds:
                    print(f"    -> {cmd}")
        print(f"\nTotal bands: {len(bands_to_process)}")
        print("Dry run complete. No SSH connection was made.")
        return

    # Demo mode - simulate with sample output, no SSH
    if args.demo:
        print("\n=== DEMO MODE (no SSH connection) ===\n")
        start_time = datetime.now()
        generated_files = []

        for i, (band, categories) in enumerate(bands_to_process.items()):
            # Filter out PIM for GSM bands
            if band.upper().startswith("G"):
                categories = filter_gsm_pim(band, categories)
            try:
                excel_path = process_band_demo(config, band, categories, is_first_band=(i == 0))
                generated_files.append(excel_path)
            except Exception as e:
                logger.error(f"Error processing band {band}: {e}", exc_info=True)
                continue

        duration = datetime.now() - start_time
        print(f"\n{'='*60}")
        print(f"  DEMO Complete! Duration: {duration}")
        print(f"  Generated files ({len(generated_files)}):")
        for f in generated_files:
            print(f"    -> {f}")
        print(f"{'='*60}\n")
        return

    # Connect and run — choose multi-node or single-node path.
    has_node2 = bool(config.site.node2_name)
    start_time = datetime.now()

    if has_node2:
        # ── Multi-node path: auto-detect bands from sdir ──────────────
        print(f"\n{'='*60}")
        print(f"  TRFS Automation Tool (Multi-Node)")
        print(f"  Site: {config.site.shortcode}")
        print(f"  Node 1: {config.site.node_name}")
        if config.site.node2_name:
            print(f"  Node 2: {config.site.node2_name}")
        if config.site.gsm_node_name:
            print(f"  GSM Node: {config.site.gsm_node_name}")
        print(f"  Server: {config.ssh.host}")
        print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

        generated_files = run_multi_node(
            config, all_commands,
            band_filter=args.band.split(",") if args.band else None,
        )

        end_time = datetime.now()
        duration = end_time - start_time
        print(f"\n{'='*60}")
        print(f"  Multi-Node TRFS Complete!")
        print(f"  Duration: {duration}")
        print(f"  Generated files ({len(generated_files)}):")
        for f in generated_files:
            print(f"    -> {f}")
        print(f"{'='*60}\n")
        return

    # ── Single-node path (original flow) ─────────────────────────────
    generated_files = []

    print(f"\n{'='*60}")
    print(f"  TRFS Automation Tool")
    print(f"  Site: {config.site.shortcode} ({config.site.node_name})")
    print(f"  Server: {config.ssh.host}")
    print(f"  Bands to process: {len(bands_to_process)}")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # Filter bands: process all including GSM, but skip PIM for GSM.
    all_items = list(bands_to_process.items())
    band_items = [(b, filter_gsm_pim(b, c) if b.upper().startswith("G") else c) for b, c in all_items]
    for b, c in band_items:
        if b.upper().startswith("G") and "PIM" in all_commands.get(b, {}):
            print(f"  [INFO] {b} — PIM skipped (GSM requires BSC access)")
            logger.info(f"[{b}] PIM skipped — GSM requires BSC access")

    # Default: run every non-GSM band in parallel. User can still override
    # with --parallel to cap concurrency (useful for debugging / rate limits).
    if args.parallel and args.parallel > 0:
        parallel = args.parallel
    else:
        parallel = max(1, len(band_items))

    # Phase 0 — shared sdir capture (one SSH session, runs once, reused by
    # every band). Must finish before Phase 1 so bands can reference it.
    print("  Phase 0: take sdir command (shared across all bands)\n")
    logger.info("[sdir] Phase 0: capturing shared sdir screenshot...")
    shared_sdir_path = capture_shared_sdir_screenshot(config)
    if shared_sdir_path:
        print(f"  [OK phase0] sdir -> {shared_sdir_path}\n")
    else:
        print("  [WARN phase0] sdir capture failed — VSWR sdir entries will be skipped\n")

    # Phase 1 — moshell SSH (parallel per-band). No ENM yet.
    # Each band gets a tuple (excel_path, moshell_screenshots_dict).
    phase1_results: dict[str, tuple[str, dict]] = {}

    def _moshell_worker(band: str, categories: dict) -> tuple[str, dict]:
        logger.info(f"[{band}] Opening SSH session...")
        with MoshellSession(config) as session:
            # Each parallel session has its own sdir state — treat as first band.
            return run_moshell_for_band(
                session, config, band, categories, is_first_band=True,
                shared_sdir_screenshot=shared_sdir_path,
            )

    if parallel == 1:
        print("  Phase 1: sequential SSH\n")
        with MoshellSession(config) as session:
            for i, (band, categories) in enumerate(band_items):
                try:
                    excel_path, moshell = run_moshell_for_band(
                        session, config, band, categories, is_first_band=(i == 0),
                        shared_sdir_screenshot=shared_sdir_path,
                    )
                    phase1_results[band] = (excel_path, moshell)
                    print(f"  [OK phase1] {band}")
                except Exception as e:
                    logger.error(f"[{band}] Phase 1 error: {e}", exc_info=True)
                    print(f"  [ERROR phase1] {band}: {e}")
    else:
        print(f"  Phase 1: parallel SSH ({parallel} workers)\n")
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {
                pool.submit(_moshell_worker, band, cats): band
                for band, cats in band_items
            }
            for fut in as_completed(futures):
                band = futures[fut]
                try:
                    excel_path, moshell = fut.result()
                    phase1_results[band] = (excel_path, moshell)
                    print(f"  [OK phase1] {band}")
                except Exception as e:
                    logger.error(f"[{band}] Phase 1 error: {e}", exc_info=True)
                    print(f"  [ERROR phase1] {band}: {e}")

    # Phase 2 — shared ENM artifacts (one browser, four captures max).
    print("\n  Phase 2: shared ENM capture (one browser)\n")
    shared_enm = capture_enm_shared_artifacts(config)

    # Phase 3 — distribute ENM artifacts per band and insert into Excel.
    print("\n  Phase 3: inserting screenshots into Excel\n")
    for band, (excel_path, moshell) in phase1_results.items():
        try:
            categories = bands_to_process[band]
            enm_for_band = build_enm_screenshots_for_band(
                band, categories, shared_enm, config
            )
            all_screenshots = {**moshell, **enm_for_band}

            if not os.path.exists(excel_path):
                logger.warning(f"[{band}] Excel vanished — re-creating")
                excel_path = create_band_excel(config, band)

            try:
                insert_screenshots_for_band(excel_path, all_screenshots)
            except FileNotFoundError:
                logger.warning(f"[{band}] Excel missing at insert — re-creating")
                excel_path = create_band_excel(config, band)
                insert_screenshots_for_band(excel_path, all_screenshots)

            generated_files.append(excel_path)
            print(f"  [OK phase3] {band} -> {excel_path}")
        except Exception as e:
            logger.error(f"[{band}] Phase 3 error: {e}", exc_info=True)
            print(f"  [ERROR phase3] {band}: {e}")

    # Summary
    end_time = datetime.now()
    duration = end_time - start_time

    print(f"\n{'='*60}")
    print(f"  TRFS Automation Complete!")
    print(f"  Duration: {duration}")
    print(f"  Generated files ({len(generated_files)}):")
    for f in generated_files:
        print(f"    -> {f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
