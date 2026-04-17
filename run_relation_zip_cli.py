"""CLI: run relation ZIP flow for a single node (saves log + prints summary)."""
import os, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from integration_runner import IntegrationSSH, run_relation

HOST = "10.51.246.22"
PORT = 5023
USER = "EWISBAY"
PASS = "4prilMay@2026!#"

NODE = "MIN3884_MANKITAGUMDDNB01"
SHORTCODE = "MIN3884"
ZIP_LOCAL = r"C:\dev\TRFS\NBR_Relation_Scripts_Delta_with_Enbid_404020_PA2.zip"
LOG_DIR = os.path.join("LOG", SHORTCODE)
os.makedirs(LOG_DIR, exist_ok=True)


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)


def main():
    ssh = IntegrationSSH(host=HOST, port=PORT, username=USER, password=PASS,
                         log_callback=log)
    ssh.connect()
    try:
        log(f"Entering AMOS for {NODE}...")
        ssh.enter_amos(NODE, timeout=120)

        log(f"Running relation ZIP flow: {ZIP_LOCAL}")
        ok, full = run_relation(ssh, NODE, SHORTCODE, ZIP_LOCAL, LOG_DIR,
                                log_cb=log, wait_for_user=None)

        # Save full output
        out_path = os.path.join(LOG_DIR, f"{NODE}_RELATION_cli.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(full)
        log(f"Saved full output to: {out_path}")

        log("=" * 60)
        log(f"RELATION result for {NODE}: {'OK' if ok else 'FAIL'}")
        log("=" * 60)
        # Print a condensed summary (last 120 lines)
        lines = full.splitlines()
        tail = "\n".join(lines[-120:])
        print("\n----- TAIL OF OUTPUT -----")
        print(tail)
        print("--------------------------")
        return 0 if ok else 1
    finally:
        try: ssh.disconnect()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
