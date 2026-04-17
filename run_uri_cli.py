"""CLI: proper URI setting flow — set commands (with y/n) + curl updateUpMoFtpServerDetails.

For each node:
  1. amos <node> + lt all
  2. set SwM=1 defaultUri                     → y
  3. set SystemFunctions=1,SwM=1,UpgradePackage=<pkg> uri                → y
  4. set SystemFunctions=1,SwM=1,UpgradePackage=<pkg> password cleartext=true,password=  → y
  5. !curl login → cookie.txt
  6. !curl POST updateUpMoFtpServerDetails → node gets full sftp URI
  7. get depack → verify uri starts with sftp://mm-software@
"""
import os, sys, time, re
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from integration_runner import IntegrationSSH, strip_ansi

HOST = "10.51.246.22"
PORT = 5023
USER = "EWISBAY"
PASS = "4prilMay@2026!#"

NODES = [
    "MIN1763_MAGUGWESTAGUMDDNB01",
    "MIN1763_MAGUGWESTAGUMDDNB02",
]
UPGRADE_PKG = "CXP2010174/2-R42H05"
ENM_LOGIN_URL = "https://lhgenm1.globetel.com/login"
ENM_URI_UPDATE_URL = (
    "https://lhgenm1.globetel.com/oss/shm/rest/softwarePackage/"
    "updateUpMoFtpServerDetails"
)
EXPECTED_URI_PREFIX = "sftp://mm-software@"


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


PROMPT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*>\s*$")


def read_until(shell, needles, timeout=60):
    """Read until any `needle` substring (case-insensitive) OR amos prompt."""
    buf = ""
    start = time.time()
    while time.time() - start < timeout:
        if shell.recv_ready():
            buf += shell.recv(65536).decode("utf-8", errors="replace")
            clean = strip_ansi(buf)
            low = clean.lower()
            for n in needles:
                if n.lower() in low:
                    return buf, n
            last = clean.strip().split("\n")[-1].strip()
            if (PROMPT_RE.match(last)
                    and "<" not in last and "/" not in last):
                return buf, "__prompt__"
        else:
            time.sleep(0.2)
    return buf, None


def send_set_with_confirm(ssh, cmd, label):
    log(f"  $ {cmd}")
    ssh.send(cmd)
    buf, hit = read_until(ssh.shell, ["[y/n]", "are you sure"], timeout=30)
    print(buf, end="")
    if hit in (None, "__prompt__"):
        log(f"    (no y/n prompt detected for {label})")
    else:
        log(f"    -> answering 'y'")
        ssh.send("y")
    tail = ssh._read_until_amos(timeout=60)
    print(tail, end="")
    return buf + tail


def run_curl_login(ssh):
    login_cmd = (
        f'!curl --insecure --request POST '
        f'--data "IDToken1={USER}" '
        f'--data "IDToken2={PASS}" '
        f'--cookie-jar ./cookie.txt {ENM_LOGIN_URL}'
    )
    log(f"  $ {login_cmd}")
    out = ssh.run_amos_command_safe(login_cmd, "_", timeout=60)
    print(out, end="")
    return out


def run_curl_update(ssh, node):
    # Build without problematic shell-escaping: use plain single quotes
    update_cmd = (
        f"!curl --insecure --request POST "
        f"'{ENM_URI_UPDATE_URL}' "
        f"--cookie cookie.txt "
        f'-H "Content-Type: application/json" '
        f'-d \'["{node}"]\''
    )
    log(f"  $ {update_cmd}")
    out = ssh.run_amos_command_safe(update_cmd, node, timeout=120)
    print(out, end="")
    return out


def main():
    ssh = IntegrationSSH(host=HOST, port=PORT, username=USER, password=PASS,
                         log_callback=log)
    ssh.connect()
    try:
        results = {}
        for node in NODES:
            log("=" * 64)
            log(f"Node: {node}")
            log("=" * 64)
            ssh.enter_amos(node, timeout=120)

            # 3 AMOS set commands
            send_set_with_confirm(ssh, "set SwM=1 defaultUri", "defaultUri")
            send_set_with_confirm(
                ssh, f"set SystemFunctions=1,SwM=1,UpgradePackage={UPGRADE_PKG} uri",
                "uri"
            )
            send_set_with_confirm(
                ssh,
                f"set SystemFunctions=1,SwM=1,UpgradePackage={UPGRADE_PKG} "
                "password cleartext=true,password=",
                "password"
            )

            # curl login + update
            login_out = run_curl_login(ssh)
            auth_ok = "Authentication Successful" in login_out or \
                      "authentication successful" in login_out.lower()
            if not auth_ok:
                log("  [WARN] ENM login did not show 'Authentication Successful'")

            update_out = run_curl_update(ssh, node)
            api_ok = "SUCCESS" in update_out.upper()
            log(f"  updateUpMoFtpServerDetails -> SUCCESS? {api_ok}")

            # Verify
            log(f"  Verifying: get depack")
            vout = ssh.run_amos_command_safe("get depack", node, timeout=60)
            print("\n----- get depack -----")
            print(vout)
            print("----------------------\n")

            uri_line = ""
            for line in vout.splitlines():
                s = line.strip()
                if s.startswith("uri") and len(s) > 3:
                    # "uri     <value>"
                    parts = s.split(None, 1)
                    if len(parts) > 1:
                        uri_line = parts[1].strip()
                    break
            uri_ok = uri_line.startswith(EXPECTED_URI_PREFIX)
            results[node] = (auth_ok, api_ok, uri_line, uri_ok)
            log(f"  uri = '{uri_line}'  startswith {EXPECTED_URI_PREFIX}? {uri_ok}")

            try:
                ssh.exit_amos()
            except Exception:
                pass

        log("=" * 64)
        log("SUMMARY")
        log("=" * 64)
        all_ok = True
        for n, (al, api, uri, ok) in results.items():
            status = "OK" if ok else "FAIL"
            log(f"  {n}: login={al}  api_success={api}  uri_ok={ok}")
            log(f"     uri = {uri}")
            if not ok: all_ok = False
        return 0 if all_ok else 1
    finally:
        try: ssh.disconnect()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
