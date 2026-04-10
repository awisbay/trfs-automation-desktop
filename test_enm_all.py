"""
Test all 3 ENM screenshot captures: Alarm Viewer, SHM, Cell Management.
Run from c:\dev\TRFS with: venv\Scripts\python test_enm_all.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from config_loader import load_config
from enm_capture import (
    capture_alarm_viewer_screenshot,
    capture_shm_software_administration_screenshot,
    capture_cell_management_screenshots,
)

config = load_config("config.yaml")
config.enm.headless = False

screenshots_dir = os.path.join("screenshots", "enm_test")
os.makedirs(screenshots_dir, exist_ok=True)

results = {}

# 1. Alarm Viewer (FM)
print("\n" + "="*60)
print("  [1/3] Capturing Alarm Viewer (FM) screenshot...")
print("="*60)
try:
    path = os.path.join(screenshots_dir, "test_fm_alarm.png")
    result = capture_alarm_viewer_screenshot(config, path)
    results["FM Alarm"] = f"OK -> {result}"
    print(f"  SUCCESS: {result}")
except Exception as e:
    results["FM Alarm"] = f"FAILED: {e}"
    print(f"  FAILED: {e}")
    import traceback
    traceback.print_exc()

# 2. SHM Software Administration
print("\n" + "="*60)
print("  [2/3] Capturing SHM Software Administration screenshot...")
print("="*60)
try:
    path = os.path.join(screenshots_dir, "test_shm.png")
    result = capture_shm_software_administration_screenshot(config, path)
    results["SHM"] = f"OK -> {result}"
    print(f"  SUCCESS: {result}")
except Exception as e:
    results["SHM"] = f"FAILED: {e}"
    print(f"  FAILED: {e}")
    import traceback
    traceback.print_exc()

# 3. Cell Management
print("\n" + "="*60)
print("  [3/3] Capturing Cell Management screenshots...")
print("="*60)
try:
    path = os.path.join(screenshots_dir, "test_cell_mgmt")
    result = capture_cell_management_screenshots(config, path, band="L900")
    results["Cell Management"] = f"OK -> {result}"
    print(f"  SUCCESS: {result}")
except Exception as e:
    results["Cell Management"] = f"FAILED: {e}"
    print(f"  FAILED: {e}")
    import traceback
    traceback.print_exc()

# Summary
print("\n" + "="*60)
print("  RESULTS SUMMARY")
print("="*60)
for name, status in results.items():
    print(f"  {name}: {status}")
print()
