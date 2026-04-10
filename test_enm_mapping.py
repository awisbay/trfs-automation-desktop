"""
Test ENM mapping: capture all 3 ENM types for L900 and insert into Excel.
Run from c:\dev\TRFS with: venv\Scripts\python test_enm_mapping.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from config_loader import load_config
from command_parser import parse_commands_file
from excel_writer import create_band_excel, insert_screenshots_for_band
from enm_capture import (
    capture_cell_management_screenshots,
    capture_alarm_viewer_screenshot,
    capture_shm_software_administration_screenshot,
)

config = load_config("config.yaml")
config.enm.headless = False

band = "L900"
screenshots_dir = os.path.join("screenshots", "enm_mapping_test")
os.makedirs(screenshots_dir, exist_ok=True)

all_screenshots = {}

# 1. CELL_ENM -> Cell Management, LTE Cells tab only (L900 is LTE)
print("\n[1/3] Capturing Cell Management (LTE Cells)...")
try:
    base_path = os.path.join(screenshots_dir, f"{config.site.shortcode}_{band}_CELL_ENM")
    result = capture_cell_management_screenshots(config, base_path, band=band, tabs_override=["LTE Cells"])
    all_screenshots["CELL_ENM"] = result
    print(f"  OK: {result}")
except Exception as e:
    print(f"  FAILED: {e}")
    import traceback; traceback.print_exc()

# 2. ALARM_ENM -> FM Alarm Viewer
print("\n[2/3] Capturing FM Alarm Viewer...")
try:
    path = os.path.join(screenshots_dir, f"{config.site.shortcode}_{band}_ALARM_ENM_1.png")
    result = capture_alarm_viewer_screenshot(config, path)
    all_screenshots["ALARM_ENM"] = [result]
    print(f"  OK: {result}")
except Exception as e:
    print(f"  FAILED: {e}")
    import traceback; traceback.print_exc()

# 3. NOC_Logs_ENM -> SHM Software Administration
print("\n[3/3] Capturing SHM Software Administration...")
try:
    path = os.path.join(screenshots_dir, f"{config.site.shortcode}_{band}_NOC_Logs_ENM_1.png")
    result = capture_shm_software_administration_screenshot(config, path)
    all_screenshots["NOC_Logs_ENM"] = [result]
    print(f"  OK: {result}")
except Exception as e:
    print(f"  FAILED: {e}")
    import traceback; traceback.print_exc()

# 4. Create Excel and insert
print("\n[4/4] Creating Excel and inserting screenshots...")
try:
    excel_path = create_band_excel(config, band)
    insert_screenshots_for_band(excel_path, all_screenshots)
    print(f"  Excel saved: {excel_path}")
except Exception as e:
    print(f"  FAILED: {e}")
    import traceback; traceback.print_exc()

print("\n" + "="*60)
print("  SUMMARY")
print("="*60)
for k, v in all_screenshots.items():
    print(f"  {k}: {v}")
print(f"\n  Excel: {excel_path if 'excel_path' in dir() else 'NOT CREATED'}")
print()
