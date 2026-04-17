"""
Quick CLI runner for LKF install (no GUI).

Uploads the LKF zip, then for each node: import, install (extract job name),
and poll status until COMPLETED.

Usage:
    python run_lkf_cli.py
"""
import os
import re
import sys
import time
import paramiko

# ── Config ───────────────────────────────────────────────────────
HOST = "10.51.246.22"
PORT = 5023
USER = "EWISBAY"
PASS = "4prilMay@2026!#"

LKF_LOCAL = r"C:\Users\ewisbay\Downloads\LKF_58629.zip"
NODES = [
    "MIN1763_MAGUGWESTAGUMDDNB01",
    "MIN1763_MAGUGWESTAGUMDDNB02",
]

SCRIPTS_PATH = "/home/shared/ESETARI/INOC/SCRIPTS"
LKF_REMOTE_DIR = f"/home/shared/{USER}/LKF"
ZIP_NAME = os.path.basename(LKF_LOCAL)
REMOTE_ZIP = f"{LKF_REMOTE_DIR}/{ZIP_NAME}"

MAX_STATUS_ATTEMPTS = 10
STATUS_INTERVAL = 60


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def exec_cmd(ssh, cmd, timeout=300):
    log(f"$ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    combined = out + (("\n[stderr]\n" + err) if err.strip() else "")
    print(combined)
    return combined


def main():
    log(f"Connecting to {HOST}:{PORT} as {USER}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30,
                look_for_keys=False, allow_agent=False)
    log("Connected.")

    # 1. Upload LKF zip
    log(f"Opening SFTP, ensuring {LKF_REMOTE_DIR} exists...")
    sftp = ssh.open_sftp()
    try:
        sftp.stat(LKF_REMOTE_DIR)
    except IOError:
        # Create recursively
        parts = LKF_REMOTE_DIR.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)
    log(f"Uploading {LKF_LOCAL} -> {REMOTE_ZIP}...")
    sftp.put(LKF_LOCAL, REMOTE_ZIP)
    sftp.close()
    log("[OK] Upload done.")

    # 2. Import LKF (once, shared across both nodes)
    log("=" * 60)
    log("Importing LKF zip...")
    log("=" * 60)
    exec_cmd(ssh, f"python {SCRIPTS_PATH}/lkfimport.py {REMOTE_ZIP}", timeout=600)

    # 3. Per-node install + poll
    results = {}
    for node in NODES:
        log("=" * 60)
        log(f"LKF install for {node}")
        log("=" * 60)
        out = exec_cmd(ssh, f"python {SCRIPTS_PATH}/lkfinstall.py {node}", timeout=600)

        # Extract job name
        job_name = None
        for line in out.splitlines():
            if "job name:" in line.lower():
                parts = line.split(":", 1)
                if len(parts) > 1:
                    tok = parts[1].strip().split()
                    if tok:
                        job_name = tok[0]
                break
        if not job_name:
            # Fallback regex
            m = re.search(r"(Shm_Cli_InstallLicense\S+)", out)
            if m:
                job_name = m.group(1)

        if not job_name:
            log(f"[FAIL] Could not extract job name for {node}.")
            results[node] = ("NO_JOB", None)
            continue
        log(f"[OK] Job name: {job_name}")

        # Poll status
        completed = False
        for attempt in range(1, MAX_STATUS_ATTEMPTS + 1):
            log(f"Status check #{attempt}/{MAX_STATUS_ATTEMPTS} for {job_name}...")
            sout = exec_cmd(ssh, f"python {SCRIPTS_PATH}/lkfstatus.py {job_name}", timeout=120)
            if "COMPLETED" in sout:
                completed = True
                break
            if attempt < MAX_STATUS_ATTEMPTS:
                log(f"Not yet COMPLETED, waiting {STATUS_INTERVAL}s...")
                time.sleep(STATUS_INTERVAL)

        if completed:
            log(f"[OK] LKF COMPLETED for {node} (job: {job_name})")
            results[node] = ("COMPLETED", job_name)
        else:
            log(f"[FAIL] LKF NOT COMPLETED after {MAX_STATUS_ATTEMPTS} attempts for {node}")
            results[node] = ("TIMEOUT", job_name)

    # Summary
    log("=" * 60)
    log("SUMMARY")
    log("=" * 60)
    for node, (status, job) in results.items():
        log(f"  {node}: {status}  (job: {job})")

    ssh.close()
    all_ok = all(s == "COMPLETED" for s, _ in results.values())
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
