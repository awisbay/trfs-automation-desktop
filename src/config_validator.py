"""
config_validator.py — pre-run sanity check for NodeCraft.

Before an integration/cut-over run, confirm the environment the config points
at is actually usable, so failures like an empty preHC path, a moved script or
an unreachable BSC broker surface as a clear checklist BEFORE the operator
commits — not three steps into a live run.

Two families of check, both read-only:
  * every absolute script path in ``config.json`` exists on the gateway
    (one bash round-trip: ``test -e`` per path);
  * the BSC broker for the site is configured and answers a ping from the
    gateway (a light proxy for "the run's broker check will pass").

Returns plain ``CheckResult`` rows the GUI renders; no side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class CheckResult:
    name: str                 # short label, e.g. "cli_py"
    status: str               # pass | warn | fail | skip
    detail: str               # human explanation
    target: str = ""          # the path / IP checked


# config.json keys whose value is a single absolute gateway path.
_SCRIPT_KEYS = [
    "scripts_path", "cli_py", "create_arne_script", "entity_maker_script",
    "exe_entity_script", "enrollment_mos", "sgw_check_mos",
    "lkf_import_script", "lkf_install_script", "lkf_status_script",
    "baseline_script_path", "external_alarm_template",
]


def collect_script_paths(cfg: dict) -> dict:
    """Every absolute (``/…``) gateway path the config references, label→path.
    Includes the per-RAT baseline files and the cut-over preHC script."""
    out: dict = {}
    for k in _SCRIPT_KEYS:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip().startswith("/"):
            out[k] = v.strip()
    for rat, p in (cfg.get("baseline_files") or {}).items():
        if isinstance(p, str) and p.strip().startswith("/"):
            out[f"baseline_files.{rat}"] = p.strip()
    # Cut-over preHC (nested) — only when actually configured.
    prehc = (((cfg.get("cutover") or {}).get("preparation") or {})
             .get("prehc") or {})
    sp = prehc.get("script_path")
    if isinstance(sp, str) and sp.strip().startswith("/"):
        out["cutover.prehc"] = sp.strip()
    return out


def check_script_paths(ssh, cfg: dict,
                       log: Callable[[str], None] = lambda m: None
                       ) -> List[CheckResult]:
    """Verify each configured gateway path exists. One bash call tests them all;
    a path is reported ``fail`` if missing, ``pass`` if present."""
    paths = collect_script_paths(cfg)
    if not paths:
        return [CheckResult("script paths", "warn",
                            "no absolute script paths in config.json")]
    labels = list(paths)
    # Emit "<i>\t<OK|MISSING>" per path so one round-trip covers everything.
    tests = "; ".join(
        f'test -e "{paths[l]}" && echo "{i}\tOK" || echo "{i}\tMISSING"'
        for i, l in enumerate(labels))
    try:
        out = ssh.run_command(tests, timeout=60)
    except Exception as exc:
        log(f"[preflight] path check failed: {exc}")
        return [CheckResult("script paths", "warn",
                            f"could not check paths: {exc}")]
    seen = {}
    for m in re.finditer(r"(?m)^(\d+)\t(OK|MISSING)\s*$", out):
        seen[int(m.group(1))] = m.group(2)
    results = []
    for i, label in enumerate(labels):
        state = seen.get(i)
        if state == "OK":
            results.append(CheckResult(label, "pass", "exists", paths[label]))
        elif state == "MISSING":
            results.append(CheckResult(label, "fail", "NOT found on gateway",
                                       paths[label]))
        else:
            results.append(CheckResult(label, "warn", "no result", paths[label]))
    return results


def check_broker(ssh, cfg: dict, bsc_name: str,
                 log: Callable[[str], None] = lambda m: None) -> CheckResult:
    """Confirm the site's BSC has a broker IP mapped and the gateway can reach
    it. A configured-but-unreachable broker is exactly what makes the GSM SGw
    check fail mid-run, so surfacing it up-front saves a wasted attempt."""
    bsc = (bsc_name or "").strip()
    if not bsc:
        return CheckResult("BSC broker", "skip", "no BSC name for this site")
    ip = (cfg.get("bsc_broker_map") or {}).get(bsc)
    if not ip:
        return CheckResult("BSC broker", "fail",
                           f"BSC '{bsc}' is not in config.json bsc_broker_map "
                           f"— add its broker IP", bsc)
    try:
        out = ssh.run_command(f"ping -c 2 -W 2 {ip}", timeout=30)
    except Exception as exc:
        return CheckResult("BSC broker", "warn",
                           f"could not ping {ip}: {exc}", ip)
    reachable = bool(re.search(r"bytes from|[12] received|0% packet loss", out))
    if reachable:
        return CheckResult("BSC broker", "pass",
                           f"{bsc} → {ip} reachable from gateway", ip)
    return CheckResult("BSC broker", "warn",
                       f"{bsc} → {ip}: no ping reply from the gateway "
                       f"(may still be reachable from the node)", ip)


def run_preflight(ssh, cfg: dict, bsc_name: str = "",
                  log: Callable[[str], None] = lambda m: None
                  ) -> List[CheckResult]:
    """Full pre-run validation: script paths + BSC broker. Ordered fails-first
    so the operator sees blockers at the top."""
    results = check_script_paths(ssh, cfg, log)
    results.append(check_broker(ssh, cfg, bsc_name, log))
    order = {"fail": 0, "warn": 1, "skip": 2, "pass": 3}
    results.sort(key=lambda r: order.get(r.status, 9))
    return results
