"""CLI runner: upload a relation XML and run `netconf <path>` on a node."""
import os, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from integration_runner import IntegrationSSH

HOST = "10.51.246.22"
PORT = 5023
USER = "EWISBAY"
PASS = "4prilMay@2026!#"

NODE = "MIN3115_BUNGVISVITAGUMDDNB01"
XML_LOCAL = r"C:\Users\ewisbay\Downloads\LTE_MIN3115_BUNGVISVITAGUMDDNB01_Relation.xml"
REMOTE_DIR = f"/home/shared/{USER}/RELATION"


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ssh = IntegrationSSH(host=HOST, port=PORT, username=USER, password=PASS,
                         log_callback=log)
    ssh.connect()
    try:
        fname = os.path.basename(XML_LOCAL)
        log(f"SFTP upload {XML_LOCAL} -> {REMOTE_DIR}/{fname}")
        ssh.sftp_upload(XML_LOCAL, REMOTE_DIR)

        log(f"Entering AMOS for {NODE}...")
        ssh.enter_amos(NODE, timeout=120)

        remote_path = f"{REMOTE_DIR}/{fname}"
        log(f"Running: netconf {remote_path}")
        out = ssh.run_amos_command_safe(f"netconf {remote_path}", NODE, timeout=900)
        print("\n----- netconf output -----")
        print(out)
        print("--------------------------\n")

        ok = "<ok/>" in out
        err = "</error-message>" in out
        if ok and not err:
            log("[OK] netconf returned <ok/>")
            return 0
        if err:
            log("[FAIL] netconf returned </error-message>")
            return 2
        log("[UNKNOWN] no <ok/> or </error-message> in output")
        return 3
    finally:
        try: ssh.disconnect()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
