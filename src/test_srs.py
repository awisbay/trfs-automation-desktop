"""
Quick CLI test for the SRS Measurement Monitoring capture flow.
Tests: Edge launch -> CDP connection -> login check -> filter + capture.
"""
import os
import sys
import time

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure src is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srs_capture import (
    _find_edge_exe,
    launch_edge_with_debug_port,
    is_edge_debug_port_open,
    wait_for_cdp_ready,
    SrsBrowserSession,
    CDP_PORT,
    SRS_BASE_URL,
)


def main():
    print("=" * 60)
    print("  SRS Measurement Monitoring - CLI Test")
    print("=" * 60)

    auto_mode = "--auto" in sys.argv

    # Step 1: Find Edge
    print("\n[1/6] Looking for Microsoft Edge...")
    edge_exe = _find_edge_exe()
    if not edge_exe:
        print("  [FAIL] Edge not found on this system. Aborting.")
        return
    print(f"  [OK] Found: {edge_exe}")

    # Step 2: Launch Edge with debug port
    srs_login_url = SRS_BASE_URL
    print(f"\n[2/6] Launching Edge with --remote-debugging-port={CDP_PORT}...")
    proc = launch_edge_with_debug_port(srs_login_url, CDP_PORT)
    if not proc:
        print("  [FAIL] Failed to launch Edge. Aborting.")
        return
    print(f"  [OK] Edge launched (PID {proc.pid}), waiting for debug port...")

    # Step 3: Check debug port (with /json/version readiness check)
    print(f"\n[3/6] Waiting for CDP port {CDP_PORT} to be fully ready...")
    if not wait_for_cdp_ready(CDP_PORT, retries=15, delay=2.0):
        print(f"  [FAIL] CDP port {CDP_PORT} is not reachable after 30s.")
        print("    Edge may already be running without the debug flag.")
        print("    Try closing ALL Edge windows first, then re-run this test.")
        return
    print(f"  [OK] CDP port {CDP_PORT} is fully ready!")

    # Step 4: Wait for user to log in
    print("\n[4/6] Waiting for user login...")
    if not auto_mode:
        try:
            print("  " + "-" * 50)
            print("  Please log in to SRS in the Edge window now.")
            print("  Complete the Azure SSO login flow.")
            print("  Once you see the SRS dashboard, press ENTER here.")
            print("  (Type 'skip' to abort)")
            print("  " + "-" * 50)
            user_input = input("  > ").strip().lower()
            if user_input == "skip":
                print("  Skipped by user.")
                return
        except EOFError:
            print("  (non-interactive - proceeding automatically)")
            time.sleep(3)
    else:
        print("  (auto mode - proceeding without waiting for login)")
        time.sleep(3)

    # Step 5: Get segment name from user
    segment_name = "TCPHTP3ACANOCOTAGUMDDNK-142"
    if not auto_mode:
        try:
            print("\n[5/6] Enter segment name to filter...")
            print("  " + "-" * 50)
            print("  Type the segment name (same as cell name)")
            print("  " + "-" * 50)
            user_segment = input("  Segment name > ").strip()
            if user_segment:
                segment_name = user_segment
        except EOFError:
            pass
    print(f"  Using segment name: {segment_name}")

    # Step 6: Connect and run the measurement monitoring workflow
    print(f"\n[6/6] Running Measurement Monitoring capture...")
    try:
        save_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots", "srs_test")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"SRS_measurement_{segment_name}.png")

        with SrsBrowserSession(port=CDP_PORT, debug_dir=save_dir) as session:
            print("  [OK] Connected to Edge via CDP")

            logged_in = session.check_logged_in()
            if logged_in:
                print("  [OK] User appears logged in to SRS")
            else:
                print("  [WARN] Could not confirm SRS login")
                print("    Will still attempt the workflow...")

            print(f"\n  Running measurement monitoring workflow...")
            print(f"  Segment: {segment_name}")
            print(f"  Steps: Navigate -> Filter -> MEASUREMENT_OK -> Detail -> Map -> Details -> Full")
            print()

            result = session.capture_measurement_monitoring(
                segment_name=segment_name,
                save_path=save_path,
            )

            if result:
                print(f"\n  [OK] Capture complete — {len(result)} screenshot(s):")
                for name, path in result.items():
                    abs_path = os.path.abspath(path)
                    size_kb = os.path.getsize(abs_path) / 1024
                    print(f"    {name}: {abs_path} ({size_kb:.1f} KB)")
            else:
                print("\n  [FAIL] Measurement monitoring capture failed")
                print("    Check debug screenshots in:", os.path.abspath(save_dir))

    except Exception as exc:
        print(f"  [FAIL] CDP connection failed: {exc}")
        import traceback
        traceback.print_exc()

    # List debug screenshots
    print("\n  Debug screenshots:")
    save_dir_abs = os.path.abspath(save_dir)
    for f in sorted(os.listdir(save_dir_abs)):
        if f.startswith("debug_srs_"):
            fpath = os.path.join(save_dir_abs, f)
            size_kb = os.path.getsize(fpath) / 1024
            print(f"    {f} ({size_kb:.1f} KB)")

    print("\n" + "=" * 60)
    print("  Test complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
