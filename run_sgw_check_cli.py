"""CLI: run SGW Check for LTE/NR and GSM sample nodes, with timing."""
import os, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from integration_runner import IntegrationSSH, run_sgw_check

HOST = "10.51.246.22"
PORT = 5023
USER = "EWISBAY"
PASS = "4prilMay@2026!#"

TARGETS = [
    ("MIN3884_MANKITAGUMDDNB01", "lte_nr"),
    ("MIN3884_MANKITAGUMDDNB02", "gsm"),
]
SHORTCODE = "MIN3884"
LOG_DIR = os.path.join("LOG", SHORTCODE)
os.makedirs(LOG_DIR, exist_ok=True)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ssh = IntegrationSSH(host=HOST, port=PORT, username=USER, password=PASS,
                         log_callback=log)
    ssh.connect()
    try:
        for node, node_type in TARGETS:
            log("=" * 70)
            log(f"NODE {node} ({node_type})")
            log("=" * 70)
            t0 = time.time()
            try:
                ssh.enter_amos(node, timeout=120)
                log(f"AMOS entered in {time.time()-t0:.1f}s")
            except Exception as exc:
                log(f"enter_amos failed: {exc}")
                continue

            t1 = time.time()
            try:
                ok, full = run_sgw_check(
                    ssh, node, log_cb=log,
                    wait_for_user=None, node_type=node_type,
                )
                log(f"SGW check finished in {time.time()-t1:.1f}s  result={ok}")
            except Exception as exc:
                log(f"SGW check raised: {exc}")
                full = ""

            out_path = os.path.join(LOG_DIR, f"{node}_SGW_cli.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(full)
            log(f"Saved {out_path}")

            try:
                ssh.exit_amos()
            except Exception as exc:
                log(f"exit_amos: {exc}")
    finally:
        try: ssh.disconnect()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
