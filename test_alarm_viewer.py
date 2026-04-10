"""
Quick standalone test for ENM Alarm Viewer screenshot capture.
Run from c:\dev\TRFS with: venv\Scripts\python test_alarm_viewer.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from config_loader import load_config
from enm_capture import capture_alarm_viewer_screenshot

config = load_config("config.yaml")

# Override headless to False so we can watch the browser
config.enm.headless = False

save_path = os.path.join("screenshots", "test_alarm_viewer.png")
print(f"Capturing alarm viewer screenshot to: {save_path}")

try:
    result = capture_alarm_viewer_screenshot(config, save_path)
    print(f"SUCCESS: Screenshot saved at {result}")
except Exception as e:
    print(f"FAILED: {e}")
    import traceback
    traceback.print_exc()
