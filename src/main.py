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
from datetime import datetime

from config_loader import load_config, get_full_path, AppConfig
from command_parser import parse_commands_file, get_moshell_categories
from ssh_runner import MoshellSession
from terminal_renderer import render_terminal_screenshot, render_multi_command_screenshot
from excel_writer import create_band_excel, insert_screenshots_for_band
from enm_capture import (
    prompt_and_capture,
    check_manual_screenshots,
    capture_cell_management_screenshots,
    capture_alarm_viewer_screenshot,
    capture_shm_software_administration_screenshot,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trfs_automation.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_band_commands(
    session: MoshellSession,
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
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

        # Run all commands for this category and collect outputs
        cmd_outputs = []
        for cmd in commands:
            actual_cmd = cmd
            if cmd.strip().lower() == "sdir" and not is_first_band:
                actual_cmd = "sdi"
                logger.info(f"  [{band}] {category}: Using sdi (sector dir already loaded)")
            output = session.run_command(actual_cmd)
            cmd_outputs.append((actual_cmd, output))

        # Generate separate screenshot per command
        category_screenshots = []
        for idx, (cmd, output) in enumerate(cmd_outputs):
            suffix = f"_{idx+1}" if len(cmd_outputs) > 1 else ""
            screenshot_path = os.path.join(
                screenshots_dir,
                f"{config.site.shortcode}_{band}_{category}{suffix}.png",
            )
            render_terminal_screenshot(
                command=cmd,
                output=output,
                style=config.terminal_style,
                save_path=screenshot_path,
                title=f"{config.site.shortcode} - {band} - {category}",
            )
            category_screenshots.append(screenshot_path)

        screenshots[category] = category_screenshots

    return screenshots


def handle_enm_items(
    config: AppConfig,
    band: str,
    commands_by_category: dict,
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

    for enm_key, enm_items in enm_categories.items():
        parent_category = enm_key.replace("_ENM", "")

        # Check for pre-saved manual screenshots first
        manual_screenshots = check_manual_screenshots(screenshots_dir, band, parent_category)
        if manual_screenshots:
            screenshots[enm_key] = manual_screenshots
            continue

        if config.enm and config.enm.enabled:
            # CELL_ENM -> Cell Management (NR Cells tab for NR bands, LTE Cells for LTE/G)
            if enm_key == "CELL_ENM":
                save_base_path = os.path.join(
                    screenshots_dir,
                    f"{config.site.shortcode}_{band}_{parent_category}_ENM",
                )
                # Determine which tab to capture based on band type
                is_nr = band.upper().startswith("NR")
                target_tab = "NR Cells" if is_nr else "LTE Cells"
                try:
                    automated_screenshots = capture_cell_management_screenshots(
                        config,
                        save_base_path,
                        band=band,
                        tabs_override=[target_tab],
                    )
                    if automated_screenshots:
                        screenshots[enm_key] = automated_screenshots
                        continue
                except Exception as e:
                    logger.warning(f"Automated Cell Management capture failed for {band}/{enm_key}: {e}")

            # ALARM_ENM -> FM Alarm Viewer
            elif enm_key == "ALARM_ENM":
                save_path = os.path.join(
                    screenshots_dir,
                    f"{config.site.shortcode}_{band}_{parent_category}_ENM_1.png",
                )
                try:
                    captured_path = capture_alarm_viewer_screenshot(config, save_path)
                    if captured_path:
                        screenshots[enm_key] = [captured_path]
                        continue
                except Exception as e:
                    logger.warning(f"Automated Alarm Viewer capture failed for {band}/{enm_key}: {e}")

            # NOC_Logs_ENM -> SHM Software Administration
            elif enm_key == "NOC_Logs_ENM":
                save_path = os.path.join(
                    screenshots_dir,
                    f"{config.site.shortcode}_{band}_{parent_category}_ENM_1.png",
                )
                try:
                    captured_path = capture_shm_software_administration_screenshot(config, save_path)
                    if captured_path:
                        screenshots[enm_key] = [captured_path]
                        continue
                except Exception as e:
                    logger.warning(f"Automated SHM capture failed for {band}/{enm_key}: {e}")

        # Fallback: prompt user for each ENM item (skip if non-interactive)
        enm_screenshots = []
        for i, item in enumerate(enm_items):
            save_path = os.path.join(
                screenshots_dir,
                f"{config.site.shortcode}_{band}_{parent_category}_ENM_{i+1}.png",
            )
            try:
                result = prompt_and_capture(item, save_path, band, parent_category)
            except EOFError:
                logger.info(f"  [{band}] {enm_key}: Skipped (non-interactive mode)")
                break
            if result:
                enm_screenshots.append(result)

        if enm_screenshots:
            screenshots[enm_key] = enm_screenshots

    return screenshots


def process_band(
    session: MoshellSession,
    config: AppConfig,
    band: str,
    commands_by_category: dict,
    is_first_band: bool = True,
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
        enm_screenshots = handle_enm_items(config, band, commands_by_category)
    except Exception as e:
        logger.warning(f"ENM screenshots skipped: {e}")
        enm_screenshots = {}

    # Step 4: Merge all screenshots and insert into Excel
    all_screenshots = {**moshell_screenshots, **enm_screenshots}
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
        help="Process only a specific band (e.g., L900, NR700)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Demo mode: simulate with sample output, no SSH needed",
    )
    args = parser.parse_args()

    # Load configuration
    config_path = os.path.abspath(args.config)
    logger.info(f"Loading config from: {config_path}")
    config = load_config(config_path)

    # Parse commands
    commands_file = get_full_path(config, config.paths.commands)
    logger.info(f"Parsing commands from: {commands_file}")
    all_commands = parse_commands_file(commands_file)

    logger.info(f"Found {len(all_commands)} bands: {', '.join(all_commands.keys())}")

    # Filter to specific band if requested
    if args.band:
        band_upper = args.band.upper()
        if band_upper not in all_commands:
            logger.error(f"Band '{args.band}' not found. Available: {', '.join(all_commands.keys())}")
            sys.exit(1)
        bands_to_process = {band_upper: all_commands[band_upper]}
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

    # Connect and run
    start_time = datetime.now()
    generated_files = []

    print(f"\n{'='*60}")
    print(f"  TRFS Automation Tool")
    print(f"  Site: {config.site.shortcode} ({config.site.node_name})")
    print(f"  Server: {config.ssh.host}")
    print(f"  Bands to process: {len(bands_to_process)}")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    with MoshellSession(config) as session:
        for i, (band, categories) in enumerate(bands_to_process.items()):
            try:
                excel_path = process_band(session, config, band, categories, is_first_band=(i == 0))
                generated_files.append(excel_path)
            except Exception as e:
                logger.error(f"Error processing band {band}: {e}", exc_info=True)
                print(f"\n  ERROR: Failed to process band {band}: {e}")
                continue

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
