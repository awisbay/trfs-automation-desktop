"""
Cut Over — backend engine.

Two layers:

  * **Per-node primitives** (``run_cutover_*``) follow the same contract as
    every other runner in this codebase —
    ``run_x(ssh, node_name, ..., log_cb, wait_for_user=None) -> tuple`` — and
    contain no threading, so they can be exercised against captured output.
  * :class:`CutoverEngine` owns the threads, deadlines and the shared
    :class:`~cutover_model.CutoverRun`.

Concurrency contract (same as the integration page, for the same reasons):

  * Worker threads mutate ``run`` only through ``run.set_cell`` / ``set_group``
    / ``set_phase``, which take ``run.lock`` and bump a version counter.
  * Worker threads never call ``page.update()`` and never import flet. They
    push text to ``log_queue`` and :class:`~cutover_model.CutoverEvent`s to
    ``event_queue``; a single asyncio loop in the GUI drains both.
  * One worker thread **per node**, never per cell — the paramiko shell is a
    single stateful PTY, so all of a node's traffic is serialized through
    ``NodeSession.run()``.

Safety notes that are load-bearing, not decoration:

  * ``ldeb`` unlocks live cells on a production network. ``dry_run`` logs the
    exact commands without sending them, confirmation is required by default,
    and one command is issued per MO so a bad pattern cannot unlock a node in
    one shot.
  * Cancel does **not** roll back. Cells already unlocked stay unlocked;
    re-locking cells that may already be carrying traffic is the more
    destructive option.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Optional

from cutover_model import (
    GSM,
    TERMINAL_FAIL,
    UNMAPPED,
    CellStatus,
    CutoverCell,
    CutoverEvent,
    CutoverRun,
    FinalStepState,
    GroupState,
    GroupStatus,
    NodeSession,
    RunPhase,
)
from cutover_parsers import (
    diff_alarms,
    gsm_band_of,
    gsm_sector_suffix,
    looks_like_unknown_command,
    match_row,
    parse_alarm_summary,
    parse_barred_state,
    parse_cells_from_hgetc,
    parse_gerancell,
    parse_gsmsector_list,
    parse_radio_status,
    parse_nr_sector_carrier_refs,
    parse_sdir_vswr,
    parse_st_cell_rows,
    parse_stzrc,
    parse_tss,
    parse_ue_counts,
    st_rows_from_stzrc,
    strip_ansi,
    ue_for_cell,
)
import cutover_persistence

logger = logging.getLogger(__name__)

#: Pillow rendering is CPU-bound and holds the GIL for seconds on long output.
#: Serializing it keeps the Flet event loop getting frames — the same reason
#: the integration page has a ``_heavy_lock``.
_render_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "enabled": True,
    "dry_run": False,
    # Open a second, read-only AMOS session per node for status/traffic/VSWR
    # polling so the background monitor never shares the single-writer PTY with
    # unlock. Falls back to the primary session (guarded by the action lock) if
    # the second connection cannot be established.
    "separate_read_session": True,
    "require_confirmation": True,
    "max_cells_per_unlock": 0,
    "discovery": {
        "lte_band_command": "hgetc ^eutrancell[FT]DD= freqBand$",
        "nr_band_command": "hgetc nrcelldu bandListManual",
        "command_timeout_s": 120,
        "amos_timeout_s": 180,
        "mo_types": ["EUtranCellFDD", "EUtranCellTDD", "NRCellDU"],
        "nr_multiband_policy": "first",
        "include_unmapped_bands": True,
        "sector_regex": "",
    },
    "band_groups": {
        "LB": ["L700", "L800", "L900", "NR700"],
        "MB": ["L1800", "L1900", "L2100", "NR1800", "NR1900", "NR2100"],
        "HB": ["L2300", "L2600", "NR2600", "NR3500"],
    },
    "group_order": ["LB", "MB", "HB"],
    "stop_on_group_failure": False,
    "unlock": {
        "command_template": "ldeb {mo_type}={cell_dn}",
        # NRCellDU depends on its sector carrier. For band 41 sector 1 this
        # resolves to GNBDUFunction=1,NRSectorCarrier=N41_S1 and is unlocked
        # once, before any related NRCellDU command.
        "nr_carrier_enabled": True,
        "nr_carrier_mo_template": (
            "GNBDUFunction=1,NRSectorCarrier=N{band_number}_S{sector}"
        ),
        "nr_carrier_unlock_template": "ldeb {mo_ref}",
        "nr_carrier_lookup_command": "get nrcelldu sectorcarrier",
        "nr_carrier_allow_name_fallback": False,
        "lock_command_template": "bl {mo_type}={cell_dn}",
        "graceful_lock": False,
        "graceful_lock_template": "set {mo_type}={cell_dn} administrativeState SHUTTING_DOWN",
        "expects_confirm": True,
        "confirm_answer": "y",
        "command_timeout_s": 120,
        "inter_command_delay_s": 0.5,
        "parallel_nodes": True,
        "abort_group_on_first_error": True,
        "error_patterns": ["ERROR", "Unable to", "not found", "No MOs",
                           "Syntax error", "failed"],
    },
    "prestate": {
        # Pre-state records which cells were already unlocked before this run so
        # rollback never re-locks a live cell. It is best-effort, NOT a gate:
        # a node that already has everything unlocked and carrying traffic must
        # still be workable, so a failed/partial read no longer blocks Cut Over.
        "enabled": True,
        "required": False,
        "skip_already_in_service": True,
    },
    "preparation": {
        "enabled": True,
        "stop_on_failure": True,
        "create_cv": {
            "enabled": True,
            "name_template": "PreCutover_{node}_{timestamp}",
        },
        "post_create_cv": {
            "enabled": True,
            "name_template": "Post_CutOver_{timestamp}",
        },
        "modump": {
            "enabled": True,
        },
        "prehc": {
            "enabled": True,
            "script_path": "/home/shared/common/INTEGRATION_TEAM/script/PreHC.txt",
            "command_template": "run {script_path}",
            "timeout_s": 900,
        },
        "posthc": {
            "enabled": True,
            "script_path": "/home/shared/common/INTEGRATION_TEAM/script/PostHC.txt",
            "command_template": "run {script_path}",
            "timeout_s": 900,
        },
    },
    "trfs": {
        "enabled": True,
        "label_script": "/home/shared/ms260229/INOC/SCRIPTS/DM/LABEL.mos",
        "command_template": "run {script_path}",
        "scripts": {
            "all": "/home/shared/common/INTEGRATION_TEAM/script/TRFS_2G4G5G_allTech_cmd.mos",
            "no2g": "/home/shared/common/INTEGRATION_TEAM/script/TRFS_4Gand5G_only_cmd.mos",
            "gsm_only": "/home/shared/common/INTEGRATION_TEAM/script/TRFS_2G_cmd_only.mos",
        },
        "remote_log_dir": "/home/shared/common/INTEGRATION_TEAM/TRFS",
        "rats_command": "pv $rats",
        "rats_timeout_s": 60,
        "idle_wait_s": 20,
        "timeout_s": 1800,
    },
    "persistence": {
        "enabled": True,
        "checkpoint_interval_s": 0.5,
        # Effective config changes are warned about during recovery, but live
        # state is still reconciled and no write is replayed automatically.
        # Set true for environments that require byte-equivalent run config.
        "require_config_match": False,
    },
    "diagnosis": {
        "enabled": True,
        "radio_status_template": "st B{band_number}",
        "barred_command_template": "hget {mo_type}={cell_dn} cellBarred|cellReservedForOperatorUse",
        "command_timeout_s": 60,
        "check_barred_before_traffic": True,
    },
    "endc": {
        "warn_nr_without_anchor": True,
        "lte_before_nr": True,
    },
    "enable_poll": {
        # Status (administrativeState / operationalState) comes from a fast
        # hgetc, NOT stzrc: stzrc loads the whole MO tree first and is slow, and
        # the cut-over only needs UNLOCKED/ENABLED vs LOCKED/DISABLED here.
        # Traffic (UE via stzrc) and VSWR (sdirc) are read separately in the
        # background. Set source="stzrc" to fold state out of the traffic table.
        "source": "st",
        "commands": [
            "hgetc EUtranCellFDD|EUtranCellTDD|NRCellDU administrativeState|operationalState"
        ],
        "command_timeout_s": 90,
        "interval_s": 15,
        "interval_max_s": 60,
        "backoff_after_s": 120,
        "timeout_s": 900,
        "max_polls": 200,
        "require_admin_unlocked": True,
        "enabled_op_states": ["ENABLED"],
        "row_regex": "",
        "match_mode": "suffix",
        "max_unmatched_polls": 3,
        "min_enabled_ratio": 0.0,
        "reconnect_credit_threshold_s": 60,
    },
    "traffic": {
        "command": "stzrc",
        # stzrc loads the whole MO tree before printing the cell tables; on a
        # large node that alone can exceed two minutes, so give it room or the
        # output gets truncated before the tables ("no cell table").
        "command_timeout_s": 300,
        "interval_s": 20,
        "timeout_s": 600,
        "ue_column_names": ["UE", "UEs", "NoOfUsers", "nrOfRrcConnected",
                            "connectedUsers", "RrcConnected"],
        "ue_regex": "",
        "ue_threshold": 1,
        "required_consecutive_samples": 2,
        "use_peak": True,
        "on_parse_failure": "manual_confirm",
        "unknown_command_patterns": ["Unknown command", "Syntax error",
                                     "command not found", "Invalid command"],
    },
    "alarm": {
        "command": "alt",
        "command_timeout_s": 120,
        "baseline_before_unlock": True,
        "no_alarm_patterns": ["No Active alarms"],
        # Once a node has at least one enabled cell, re-enable FM alarm
        # supervision in the background (once per node) — counterpart to the
        # integration Backup CV's fmalarmsupervision=false. {cli}=ENM CLI helper,
        # {node}=NetworkElement.
        "activate_enabled": True,
        "activate_check_command": (
            '!python {cli} "cmedit get {shortcode}* '
            'fmalarmsupervision.active -t"'),
        "activate_command": (
            '!python {cli} "cmedit set {node} fmalarmsupervision active=true"'),
        "activate_timeout_s": 60,
    },
    "vswr": {
        # After a cell enables, sdirc reads the VSWR of every RF port and the
        # cells each port carries, shown next to the cell alongside its UE
        # count. sdirc is slow (minutes), so it refreshes on its own long
        # interval rather than every status poll.
        "enabled": True,
        "command": "sdirc",
        "command_timeout_s": 600,
        "interval_s": 300,
        "warn_threshold": 1.40,   # a port VSWR above this reads as a problem
    },
    "gsm": {
        # GSM cells (GeranCell) unlock in two places: the BSC (cmedit set
        # state=ACTIVE) and the node (ldeb GsmSector=…,Trx). "Enabled" is the
        # combination of GeranCell=ACTIVE and every Trx timeslot ENABLED (tss).
        "enabled": True,
        "cli_py": "",   # blank → reuse integration_runner.CLI_PY
        # {cli}=python cmedit client, {site}=M<digits> site id, {fdn}=full GeranCell
        # FDN (cmedit set needs the FDN, not a filter), {state}=ACTIVE|HALTED.
        # The get is verbose (no -t) so the FDN is captured for the set.
        "state_get_template": '!python {cli} "cmedit get * GeranCell.(GeranCellid=={site}*,state)"',
        "state_set_template": '!python {cli} "cmedit set {fdn} state={state}"',
        "active_value": "ACTIVE",
        "locked_value": "HALTED",
        "set_success_patterns": ["SUCCESS"],
        "cmedit_timeout_s": 120,
        "tss_command": "get . tss",
        "gsmsector_list_command": "lst gsmsector",
        # {sector_mo}=full GsmSector RDN, e.g. CMPBAHIANMALAYBBUK-1.
        "trx_unlock_template": "ldeb GsmSector={sector_mo},Trx",
        "trx_unlock_confirm": True,
        "trx_confirm_answer": "y",
        "command_timeout_s": 120,
        "enable_timeout_s": 30,      # max 30s; operator can Lock meanwhile
        "poll_interval_s": 5,
        "band_groups": {"GSM900": "GSM", "GSM1800": "GSM"},
    },
    "report": {
        "screenshot_subdir": "CUTOVER",
        "filename_template": "{shortcode}_CUTOVER_{group}_{timestamp}.png",
        "title_template": "{shortcode} - Cut Over {group} - {nodes}",
        "max_width": 1600,
        "terminal_style": {
            "bg_color": [12, 12, 12],
            "text_color": [204, 204, 204],
            "header_color": [0, 255, 0],
            "font_size": 13,
            "font": "Consolas",
            "padding": 20,
            "line_spacing": 4,
        },
        "whatsapp": {
            "enabled": True,
            "mode": "semi_auto",
            "group_link": "",
            "caption_template": (
                "Cut Over {group} - {shortcode}\nNodes: {nodes}\n"
                "Cells enabled: {ok}/{total}\nTraffic OK: {traffic_ok}\n"
                "Alarms: {alarms}"
            ),
        },
    },
    "final_verification": {"enabled": True, "stop_on_failure": False, "steps": []},
}


def _nr_carrier_ref(cell, cfg: dict) -> str:
    """Related NRSectorCarrier MO for an NRCellDU, or ``""`` when disabled."""
    unlock = cfg.get("unlock", {})
    if cell.rat != "NR" or not unlock.get("nr_carrier_enabled", True):
        return ""
    live_ref = str(getattr(cell, "nr_sector_carrier_ref", "") or "").strip()
    if live_ref:
        return live_ref
    if not unlock.get("nr_carrier_allow_name_fallback", False):
        return ""
    if int(cell.band_number) < 0 or not str(cell.sector).strip():
        return ""
    return str(unlock.get(
        "nr_carrier_mo_template",
        "GNBDUFunction=1,NRSectorCarrier=N{band_number}_S{sector}",
    )).format(
        band_number=cell.band_number,
        sector=cell.sector,
        node=cell.node_name,
        cell_dn=cell.cell_dn,
    )


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_comment"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cutover_config(path: Optional[str] = None) -> dict:
    """Read the ``cutover`` block fresh from disk, merged over the defaults.

    Read at run start rather than at import: the commands here are unconfirmed
    and operators will iterate on them, so editing ``config.json`` and clicking
    Start should take effect without restarting the app.
    """
    if path is None:
        try:
            from integration_runner import _resolve_config_path
            path = _resolve_config_path()
        except Exception:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.json")
    raw = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("cutover", {}) or {}
    except Exception as exc:
        logger.warning("Could not read cutover config from %s: %s", path, exc)
    return _deep_merge(_DEFAULTS, raw)


# ──────────────────────────────────────────────────────────────────
# Per-node primitives
# ──────────────────────────────────────────────────────────────────
def run_cutover_discovery(ssh, node_name: str, log_cb: Callable[[str], None],
                          cfg: dict, wait_for_user=None) -> tuple:
    """List every cell on *node_name* with its band. Returns (ok, output, cells)."""
    disc = cfg["discovery"]
    out_all = ""

    log_cb(f"[{node_name}] listing LTE cell bands…")
    lte_out = ssh.run_amos_command_safe(
        disc["lte_band_command"], node_name, timeout=disc["command_timeout_s"])
    out_all += lte_out

    log_cb(f"[{node_name}] listing NR cell bands…")
    nr_out = ssh.run_amos_command_safe(
        disc["nr_band_command"], node_name, timeout=disc["command_timeout_s"])
    out_all += "\n" + nr_out

    cells = parse_cells_from_hgetc(
        lte_out, nr_out, node_name,
        band_groups=cfg["band_groups"],
        mo_types=tuple(disc["mo_types"]),
        nr_multiband_policy=disc["nr_multiband_policy"],
        include_unmapped=disc["include_unmapped_bands"],
        sector_regex=str(disc.get("sector_regex", "")),
    )
    if not cells:
        log_cb(f"[{node_name}] no cells found in the band listing.")
        return False, out_all, []

    by_group: dict = {}
    for c in cells:
        by_group.setdefault(c.group, 0)
        by_group[c.group] += 1
    summary = ", ".join(f"{g}={n}" for g, n in sorted(by_group.items()))
    log_cb(f"[{node_name}] found {len(cells)} cell(s): {summary}")
    return True, out_all, cells


def run_cutover_unlock(ssh, node_name: str, cells: list,
                       log_cb: Callable[[str], None], cfg: dict,
                       wait_for_user=None, dry_run: bool = False,
                       cancel_event: Optional[threading.Event] = None,
                       on_cell=None) -> tuple:
    """Send the unlock command for each cell. Returns (ok, combined_output).

    ``on_cell(cell, ok, output, error)`` is called after each command so the
    caller can update the UI without this function knowing about the UI.
    """
    unlock = cfg["unlock"]
    template = unlock["command_template"]
    err_pats = [p for p in unlock.get("error_patterns", []) if p]
    combined = ""
    any_ok = False
    carrier_done: dict = {}

    for cell in cells:
        if cancel_event is not None and cancel_event.is_set():
            return any_ok, combined

        # NRSectorCarrier is an administrative dependency of NRCellDU. Unlock
        # it first and only once when multiple NR cells share band + sector.
        carrier_ref = _nr_carrier_ref(cell, cfg)
        if cell.rat == "NR" and unlock.get("nr_carrier_enabled", True):
            if not carrier_ref:
                msg = (f"cannot derive NRSectorCarrier for {cell.mo_ref} "
                       f"(band={cell.band_number}, sector={cell.sector or '?'})")
                log_cb(f"[{node_name}] ✗ {msg}")
                if on_cell:
                    on_cell(cell, False, "", msg)
                if unlock.get("abort_group_on_first_error") and not any_ok:
                    return False, combined
                continue
            if carrier_ref not in carrier_done:
                carrier_cmd = str(unlock.get(
                    "nr_carrier_unlock_template", "ldeb {mo_ref}"
                )).format(mo_ref=carrier_ref, node=node_name,
                          band_number=cell.band_number, sector=cell.sector)
                if dry_run:
                    log_cb(f"[{node_name}] DRY RUN — would send: {carrier_cmd}")
                    combined += f"[DRY RUN] {carrier_cmd}\n"
                    carrier_done[carrier_ref] = True
                else:
                    log_cb(f"[{node_name}] {carrier_cmd}")
                    try:
                        if unlock.get("expects_confirm"):
                            carrier_out = ssh.run_amos_set_with_confirm(
                                carrier_cmd, node_name,
                                answer=unlock.get("confirm_answer", "y"),
                                timeout=unlock["command_timeout_s"])
                        else:
                            carrier_out = ssh.run_amos_command_safe(
                                carrier_cmd, node_name,
                                timeout=unlock["command_timeout_s"])
                    except Exception as exc:
                        carrier_out = ""
                        carrier_error = f"{type(exc).__name__}: {exc}"
                    else:
                        hit = next((p for p in err_pats if re.search(
                            re.escape(p), carrier_out, re.IGNORECASE)), None)
                        carrier_error = f"output matched {hit!r}" if hit else ""
                    combined += carrier_out + "\n"
                    carrier_done[carrier_ref] = not carrier_error
                    if carrier_error:
                        log_cb(f"[{node_name}] ✗ {carrier_ref}: {carrier_error}")
                if carrier_done.get(carrier_ref):
                    delay = unlock.get("inter_command_delay_s") or 0
                    if delay:
                        time.sleep(delay)
            if not carrier_done.get(carrier_ref):
                msg = f"NRSectorCarrier unlock failed: {carrier_ref}"
                if on_cell:
                    on_cell(cell, False, "", msg)
                if unlock.get("abort_group_on_first_error") and not any_ok:
                    return False, combined
                continue

        command = template.format(
            mo_type=cell.mo_type, cell_dn=cell.cell_dn,
            mo_ref=cell.mo_ref, node=node_name)

        if dry_run:
            log_cb(f"[{node_name}] DRY RUN — would send: {command}")
            combined += f"[DRY RUN] {command}\n"
            any_ok = True
            if on_cell:
                on_cell(cell, True, "[dry run]", "")
            continue

        log_cb(f"[{node_name}] {command}")
        try:
            if unlock.get("expects_confirm"):
                out = ssh.run_amos_set_with_confirm(
                    command, node_name,
                    answer=unlock.get("confirm_answer", "y"),
                    timeout=unlock["command_timeout_s"])
            else:
                out = ssh.run_amos_command_safe(
                    command, node_name, timeout=unlock["command_timeout_s"])
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log_cb(f"[{node_name}] ✗ {cell.mo_ref}: {msg}")
            if on_cell:
                on_cell(cell, False, "", msg)
            if unlock.get("abort_group_on_first_error") and not any_ok:
                return False, combined
            continue

        combined += out + "\n"
        hit = next((p for p in err_pats
                    if re.search(re.escape(p), out, re.IGNORECASE)), None)
        if hit:
            log_cb(f"[{node_name}] ✗ {cell.mo_ref}: output matched {hit!r}")
            if on_cell:
                on_cell(cell, False, out, f"matched {hit!r}")
            # The most likely first failure is a wrong command name. Stopping
            # after one saves sending 40 more that will fail the same way.
            if unlock.get("abort_group_on_first_error") and not any_ok:
                return False, combined
            continue

        any_ok = True
        if on_cell:
            on_cell(cell, True, out, "")
        delay = unlock.get("inter_command_delay_s") or 0
        if delay:
            time.sleep(delay)

    return any_ok, combined


def run_cutover_nr_carrier_refs(ssh, node_name: str,
                                log_cb: Callable[[str], None],
                                cfg: dict) -> tuple:
    """Read the authoritative NRCellDU → NRSectorCarrier references."""
    unlock = cfg["unlock"]
    command = unlock.get(
        "nr_carrier_lookup_command", "get nrcelldu sectorcarrier")
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=unlock["command_timeout_s"])
    return parse_nr_sector_carrier_refs(out), out


def run_cutover_st_cell(ssh, node_name: str, log_cb: Callable[[str], None],
                        cfg: dict, wait_for_user=None) -> tuple:
    """Run the status command(s). Returns (ok, output, rows).

    ``enable_poll.source`` selects where cell state comes from:

    * ``stzrc`` (default) — reuse the traffic command, whose LTECell/NRCell
      tables already carry an ``S`` state column. One command instead of two,
      which halves the polling load on a node that is busy mid-cutover.
    * ``st`` — run ``enable_poll.commands`` and parse them.
    """
    poll = cfg["enable_poll"]

    if poll.get("source", "stzrc") == "stzrc":
        traffic = cfg["traffic"]
        command = traffic["command"].format(node=node_name)
        out = ssh.run_amos_command_safe(
            command, node_name, timeout=traffic["command_timeout_s"])
        bad = looks_like_unknown_command(
            out, tuple(traffic.get("unknown_command_patterns", [])))
        if bad:
            log_cb(f"[{node_name}] ✗ {command!r} was rejected by moshell "
                   f"(matched {bad!r}).")
            return False, out, []
        stz = parse_stzrc(out)
        if stz.ok:
            for table, (total, up) in sorted(stz.totals.items()):
                log_cb(f"[{node_name}] {table}: {up}/{total} cell(s) up")
            return True, out, st_rows_from_stzrc(stz)
        # Not stzrc-shaped after all — fall through to the st commands, but say
        # WHY so a recurring fallback can be diagnosed. The usual cause is the
        # command returning before the cell tables printed (stzrc loads the whole
        # MO tree first, which on a big node can exceed traffic.command_timeout_s)
        # rather than a genuinely different format.
        txt = out or ""
        has_hdr = ("LTECell" in txt) or ("NRCell" in txt)
        has_total = "Total:" in txt and "Cells" in txt
        tail = " | ".join(
            ln.strip() for ln in txt.splitlines() if ln.strip())[-160:]
        if has_hdr and not has_total:
            why = ("a cell-table header is present but no 'Total: N Cells' line — "
                   "the output looks truncated (increase "
                   "cutover.traffic.command_timeout_s)")
        elif not has_hdr:
            why = (f"no LTECell/NRCell header in {len(txt)} char(s) of output — "
                   f"likely still loading MOs when it returned")
        else:
            why = "cell rows could not be parsed"
        log_cb(f"[{node_name}] {command!r}: {why}; falling back to the status "
               f"command(s). tail: …{tail}")

    commands = poll.get("commands") or ["st cell"]
    if isinstance(commands, str):
        commands = [commands]

    out_all = ""
    rows: list = []
    for command in commands:
        out = ssh.run_amos_command_safe(
            command, node_name, timeout=poll["command_timeout_s"])
        out_all += out + "\n"
        rows.extend(parse_st_cell_rows(
            out,
            mo_types=tuple(cfg["discovery"]["mo_types"]),
            row_regex=poll.get("row_regex", ""),
        ))
    return bool(rows), out_all, rows


def run_cutover_prestate(ssh, node_name: str, cells: list,
                         log_cb: Callable[[str], None], cfg: dict,
                         wait_for_user=None) -> tuple:
    """Snapshot cell state **before** anything is sent. Returns (ok, output).

    This is what makes rollback safe. A cell that is already UNLOCKED+ENABLED
    when the run starts may be carrying live customers — it is not ours to
    unlock, and above all not ours to re-lock later.
    """
    ok, out, rows = run_cutover_st_cell(ssh, node_name, log_cb, cfg)
    if not ok:
        log_cb(f"[{node_name}] ⚠ pre-state read did not return a cell table — "
               f"continuing without it (rollback ownership limited for this node).")
        return False, out

    mode = cfg["enable_poll"].get("match_mode", "suffix")
    # GSM (GeranCell) cells never appear in the LTE/NR st-cell table — they have
    # their own state path — so they must not count toward pre-state matching.
    node_cells = [c for c in cells
                  if c.node_name == node_name and c.rat != "GSM"]
    staged: dict = {}
    matched_keys: set = set()
    for row in rows:
        cell = match_row(node_cells, node_name, row, mode=mode)
        if cell is None:
            continue
        matched_keys.add(cell.key)
        staged[cell.key] = (row.admin_state, row.op_state)

    missing = [c for c in node_cells if c.key not in matched_keys]
    if missing:
        preview = ", ".join(c.mo_ref for c in missing[:8])
        if len(missing) > 8:
            preview += f", … (+{len(missing) - 8} more)"
        log_cb(
            f"[{node_name}] ⚠ pre-state matched "
            f"{len(matched_keys)}/{len(node_cells)} discovered cell(s) "
            f"(missing: {preview}) — continuing; rollback ownership is limited "
            f"for the unmatched cells."
        )
        return False, out

    already = 0
    for cell in node_cells:
        admin_state, op_state = staged[cell.key]
        cell.admin_state = admin_state
        cell.op_state = op_state
        if admin_state.upper() == "UNLOCKED":
            cell.was_unlocked_before = True
            if op_state.upper() == "ENABLED":
                cell.already_in_service = True
                already += 1

    if already:
        log_cb(f"[{node_name}] {already} cell(s) were already in service before "
               f"this run — they will not be unlocked, and rollback will not "
               f"touch them.")
    return True, out


def run_cutover_create_cv(ssh, node_name: str, log_cb: Callable[[str], None],
                          cfg: dict, wait_for_user=None,
                          mode: str = "pre") -> tuple:
    """Create the pre- or post-Cut Over CV using the SHM backup workflow.

    ``mode="post"`` names the CV ``Post_CutOver_DDMMYYYY_HHMM`` (the post-check
    counterpart of the pre-Cut Over CV), overridable via
    ``preparation.post_create_cv.name_template``.
    """
    from integration_runner import run_backup_cv

    if mode == "post":
        step_cfg = cfg["preparation"].get("post_create_cv", {})
        timestamp = datetime.now().strftime("%d%m%Y_%H%M")
        backup_name = step_cfg.get(
            "name_template", "Post_CutOver_{timestamp}"
        ).format(node=node_name, timestamp=timestamp)
    else:
        step_cfg = cfg["preparation"]["create_cv"]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = step_cfg.get(
            "name_template", "PreCutover_{node}_{timestamp}"
        ).format(node=node_name, timestamp=timestamp)
    return run_backup_cv(
        ssh, node_name, log_cb, wait_for_user, backup_name=backup_name,
    )


def run_cutover_modump(ssh, node_name: str, shortcode: str, log_dir: str,
                       log_cb: Callable[[str], None], cfg: dict,
                       wait_for_user=None) -> tuple:
    """Capture and download the pre-Cut Over modump."""
    from integration_runner import run_take_dump

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return run_take_dump(
        ssh, node_name, shortcode, log_dir, log_cb, wait_for_user,
        local_filename=f"{node_name}_modump_{timestamp}.zip",
    )


def run_cutover_prehc(ssh, node_name: str, log_cb: Callable[[str], None],
                      cfg: dict, wait_for_user=None, mode: str = "pre") -> tuple:
    """Run the configured pre/post HC Moshell script and return its raw output.

    ``mode="post"`` runs ``preparation.posthc.script_path`` instead of
    ``preparation.prehc.script_path`` — the post-cutover health check.
    """
    key = "posthc" if mode == "post" else "prehc"
    label = "postHC" if mode == "post" else "preHC"
    step_cfg = cfg["preparation"].get(key, {})
    script_path = str(step_cfg.get("script_path", "")).strip()
    if not script_path:
        # No HC script configured → INFO only, never a blocker. Cut Over
        # should still discover and show current cell status.
        msg = (f"[{node_name}] {label} has no script_path configured — skipped "
               f"(info only, not blocking). Set "
               f"cutover.preparation.{key}.script_path to enable it.")
        log_cb(f"ℹ {msg}")
        return True, msg
    if "\n" in script_path or "\r" in script_path:
        msg = f"[{node_name}] {label} script_path contains a newline."
        log_cb(f"✗ {msg}")
        return False, msg

    command = str(step_cfg.get(
        "command_template", "run {script_path}"
    )).format(
        script_path=script_path,
        node=node_name,
    )
    log_cb(f"[{node_name}] running {label} script: {script_path}")
    try:
        out = ssh.run_amos_command_safe(
            command, node_name,
            timeout=int(step_cfg.get("timeout_s", 900)),
        )
    except Exception as exc:
        msg = f"{label} failed: {type(exc).__name__}: {exc}"
        log_cb(f"[{node_name}] ✗ {msg}")
        return False, msg

    error_patterns = step_cfg.get(
        "error_patterns",
        ["ERROR", "Unknown command", "Syntax error", "command not found"],
    )
    matched = next(
        (pattern for pattern in error_patterns
         if re.search(pattern, out or "", re.IGNORECASE)),
        "",
    )
    if matched:
        # preHC is a health check, not a gate. Its output legitimately contains
        # the word "ERROR" (real alarms AND benign internal moshell noise like
        # "ERROR: mv .../pmxgLog... FAILED"), so a substring match must NOT block
        # the whole cut over. Flag it for review and continue, unless the
        # operator explicitly opts into blocking via preparation.prehc.blocking.
        if bool(step_cfg.get("blocking", False)):
            log_cb(f"[{node_name}] ✗ {label} output matched error pattern: {matched}")
            return False, out
        log_cb(f"[{node_name}] ⚠ {label} output matched '{matched}' — health check "
               f"flagged something (info only, not blocking). Review the HC "
               f"log; cell status will still be shown.")
        return True, out
    log_cb(f"[{node_name}] ✓ {label} completed.")
    return True, out


def run_cutover_download_hc_log(ssh, node_name: str, mode: str,
                                local_dir: str,
                                log_cb: Callable[[str], None]) -> str:
    """Download the moshell HC logfile the ``run`` produced for this node.

    The HC script writes its own logfile to
    ``~/Logfile/<YYYYMMDD>/<node>_Logfile_<date>_<time>_(Pre|Post)_HC.log``.
    We list today's ``~/Logfile/<date>/`` and pull the newest file for this
    node ending in ``_Pre_HC.log`` / ``_Post_HC.log``. Returns the local path
    or ``""`` (best-effort — a missing HC log never fails the run)."""
    label = "Post_HC" if mode == "post" else "Pre_HC"
    date = datetime.now().strftime("%Y%m%d")
    home = f"/home/shared/{getattr(ssh, 'username', '')}".rstrip("/")
    remote_dir = f"{home}/Logfile/{date}"
    # POST is a persistent per-site folder, so retain every button run instead
    # of replacing an earlier Post HC logfile from the same day.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = (f"{node_name}_Logfile_{stamp}_{label}.log"
                  if mode == "post"
                  else f"{node_name}_Logfile_{date}_{label}.log")
    local_path = os.path.join(local_dir, local_name)
    got = ssh.download_newest_logfile(
        remote_dir, local_path,
        name_contains=node_name, name_endswith=f"_{label}.log",
    )
    if got:
        log_cb(f"[{node_name}] {label} logfile downloaded: {got}")
        return got
    log_cb(f"[{node_name}] ⚠ no {label} logfile found under {remote_dir} "
           f"(searched *_{label}.log for {node_name}).")
    return ""


def parse_rats(output: str) -> tuple:
    """Parse ``pv $rats`` → (has_2g, has_4g, has_5g).

    moshell prints e.g. ``$rats = L`` (LTE only) or ``$rats = GLN`` (a
    combination). One letter is a standalone baseband, several letters a
    combination. G=GSM(2G), L=LTE(4G), N=NR(5G). ``$ratsconfig`` on the next
    line is ignored."""
    m = re.search(r"\$rats\s*=\s*([A-Za-z]+)", strip_ansi(output or ""))
    val = (m.group(1).upper() if m else "")
    return ("G" in val, "L" in val, "N" in val)


def run_cutover_rats(ssh, node_name: str, log_cb: Callable[[str], None],
                     cfg: dict) -> tuple:
    """Run ``pv $rats`` to identify the baseband's techs. Returns
    (has_2g, has_4g, has_5g, raw_output)."""
    trfs = cfg.get("trfs", {})
    command = str(trfs.get("rats_command", "pv $rats"))
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=int(trfs.get("rats_timeout_s", 60)))
    has_2g, has_4g, has_5g = parse_rats(out)
    return has_2g, has_4g, has_5g, out


def trfs_script_for_techs(has_2g: bool, has_4g: bool, has_5g: bool,
                          cfg: dict) -> str:
    """Pick the TRFS script path for a node's tech mix, or "" if unknown.

    * 2G present with 4G (± 5G)  → the all-tech script.
    * 2G only                    → the 2G-only script.
    * no 2G, but 4G and/or 5G    → the 4G/5G-only script.
    """
    scripts = cfg.get("trfs", {}).get("scripts", {})
    if has_2g and has_4g:
        return str(scripts.get("all", "")).strip()
    if has_2g and not has_4g and not has_5g:
        return str(scripts.get("gsm_only", "")).strip()
    if not has_2g and (has_4g or has_5g):
        return str(scripts.get("no2g", "")).strip()
    # 2G+5G without 4G is undocumented; the all-tech script is the safe superset.
    if has_2g:
        return str(scripts.get("all", "")).strip()
    return ""


def run_cutover_trfs(ssh, node_name: str, script_path: str,
                     local_dir: str, log_cb: Callable[[str], None],
                     cfg: dict) -> tuple:
    """Run one TRFS script on a node, wait for moshell to go idle, then pull
    the node's newest TRFS log folder. Returns (ok, local_folder_or_msg)."""
    trfs = cfg.get("trfs", {})
    command = str(trfs.get("command_template", "run {script_path}")).format(
        script_path=script_path, node=node_name)
    log_cb(f"[{node_name}] TRFS: {command}")
    try:
        out = ssh.run_amos_command_safe(
            command, node_name, timeout=int(trfs.get("timeout_s", 1800)))
    except Exception as exc:
        msg = f"TRFS script failed: {type(exc).__name__}: {exc}"
        log_cb(f"[{node_name}] ✗ {msg}")
        return False, msg

    # moshell keeps flushing the log after the prompt returns; give it a short
    # idle window before grabbing the folder so the log is complete.
    idle = float(trfs.get("idle_wait_s", 20))
    if idle > 0:
        log_cb(f"[{node_name}] TRFS script done — waiting {idle:.0f}s for "
               f"moshell to go idle before downloading the log folder…")
        time.sleep(idle)

    remote_parent = str(trfs.get(
        "remote_log_dir", "/home/shared/common/INTEGRATION_TEAM/TRFS"))
    got = ssh.download_newest_dir(
        remote_parent, local_dir, name_prefix=f"{node_name}_")
    if got:
        return True, got
    return False, (f"TRFS log folder for {node_name} not found under "
                   f"{remote_parent}")


def run_cutover_relock(ssh, node_name: str, cells: list,
                       log_cb: Callable[[str], None], cfg: dict,
                       wait_for_user=None, dry_run: bool = False,
                       cancel_event: Optional[threading.Event] = None,
                       on_cell=None, allow_any: bool = False) -> tuple:
    """Lock cells. Returns (ok, output).

    For rollback (``allow_any=False``) this refuses any cell that is not
    :attr:`~cutover_model.CutoverCell.is_relockable`, because re-locking a cell
    this run did not unlock could take a live cell out of service. When the
    operator explicitly asks to lock a group that is already up
    (``allow_any=True``, the Lock button), the guard is lifted and any passed
    cell is locked.
    """
    unlock = cfg["unlock"]
    template = (unlock.get("graceful_lock_template")
                if unlock.get("graceful_lock")
                else unlock.get("lock_command_template", "bl {mo_type}={cell_dn}"))
    combined = ""
    any_ok = False

    for cell in cells:
        if cancel_event is not None and cancel_event.is_set():
            break
        if not allow_any and not cell.is_relockable:
            log_cb(f"[{node_name}] refusing to re-lock {cell.mo_ref} — it was "
                   f"not unlocked by this run.")
            continue

        command = template.format(
            mo_type=cell.mo_type, cell_dn=cell.cell_dn,
            mo_ref=cell.mo_ref, node=node_name)

        if dry_run:
            log_cb(f"[{node_name}] DRY RUN — would send: {command}")
            combined += f"[DRY RUN] {command}\n"
            any_ok = True
            if on_cell:
                on_cell(cell, True, "[dry run]", "")
            continue

        log_cb(f"[{node_name}] {command}")
        try:
            if unlock.get("expects_confirm"):
                out = ssh.run_amos_set_with_confirm(
                    command, node_name, answer=unlock.get("confirm_answer", "y"),
                    timeout=unlock["command_timeout_s"])
            else:
                out = ssh.run_amos_command_safe(
                    command, node_name, timeout=unlock["command_timeout_s"])
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log_cb(f"[{node_name}] ✗ re-lock {cell.mo_ref}: {msg}")
            if on_cell:
                on_cell(cell, False, "", msg)
            continue

        combined += out + "\n"
        any_ok = True
        if on_cell:
            on_cell(cell, True, out, "")
        delay = unlock.get("inter_command_delay_s") or 0
        if delay:
            time.sleep(delay)

    return any_ok, combined


def run_cutover_radio_status(ssh, node_name: str, band_number: int,
                             log_cb: Callable[[str], None], cfg: dict,
                             wait_for_user=None) -> tuple:
    """Check the band's radio via ``st B<band>``. Returns (ok, output, summary).

    A cell reporting DEPENDENCY_LOCKED is almost always waiting on its radio /
    Carrier rather than on itself, so this turns a silent 15-minute timeout
    into an actionable message.
    """
    diag = cfg["diagnosis"]
    command = diag["radio_status_template"].format(
        band_number=band_number, node=node_name)
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=diag["command_timeout_s"])
    return True, out, parse_radio_status(out)


def run_cutover_barred_check(ssh, node_name: str, cell,
                             log_cb: Callable[[str], None], cfg: dict,
                             wait_for_user=None) -> tuple:
    """Read the cell's barring state. Returns (ok, output, barred|None).

    A barred cell can be UNLOCKED and ENABLED and still never attract a UE, so
    checking this before the traffic wait avoids burning the whole timeout on a
    cell that was never going to report traffic.
    """
    diag = cfg["diagnosis"]
    command = diag["barred_command_template"].format(
        mo_type=cell.mo_type, cell_dn=cell.cell_dn,
        mo_ref=cell.mo_ref, node=node_name)
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=diag["command_timeout_s"])
    return True, out, parse_barred_state(out)


def run_cutover_traffic(ssh, node_name: str, log_cb: Callable[[str], None],
                        cfg: dict, wait_for_user=None) -> tuple:
    """Run the traffic command. Returns (ok, output, UeParseResult)."""
    traffic = cfg["traffic"]
    command = traffic["command"].format(node=node_name)
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=traffic["command_timeout_s"])

    bad = looks_like_unknown_command(
        out, tuple(traffic.get("unknown_command_patterns", [])))
    if bad:
        log_cb(f"[{node_name}] ✗ traffic command {command!r} was rejected by "
               f"moshell (matched {bad!r}). Set cutover.traffic.command in "
               f"config.json.")
        return False, out, None

    res = parse_ue_counts(
        out,
        mo_types=tuple(cfg["discovery"]["mo_types"]),
        ue_column_names=tuple(traffic.get("ue_column_names", ())),
        ue_regex=traffic.get("ue_regex", ""),
    )
    return True, out, res


def run_cutover_vswr(ssh, node_name: str, log_cb: Callable[[str], None],
                     cfg: dict, wait_for_user=None) -> tuple:
    """Run ``sdirc`` and parse per-RF-port VSWR. Returns (ok, output, VswrResult)."""
    v = cfg.get("vswr", {})
    command = str(v.get("command", "sdirc")).format(node=node_name)
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=int(v.get("command_timeout_s", 600)))
    res = parse_sdir_vswr(out)
    if not res.ok:
        log_cb(f"[{node_name}] VSWR ({command}): {res.warning}")
    return res.ok, out, res


# ──────────────────────────────────────────────────────────────────
# GSM primitives
# ──────────────────────────────────────────────────────────────────
def _gsm_cli_py(cfg: dict) -> str:
    cli = str(cfg.get("gsm", {}).get("cli_py", "")).strip()
    if cli:
        return cli
    from integration_runner import CLI_PY
    return CLI_PY


def _gsm_site_id(shortcode: str) -> str:
    """Shortcode → GSM cell id prefix (MIN2839 → M2839), reusing the
    integration step's rule."""
    from integration_runner import _shortcode_to_cell_id
    return _shortcode_to_cell_id(shortcode)


def run_cutover_gsm_states(ssh, node_name: str, shortcode: str,
                           log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """Read the site's GeranCells from the BSC (verbose, so FDNs are captured).
    Returns (info, out) where info maps cell_id → ``{"fdn": …, "state": …}``."""
    g = cfg["gsm"]
    command = g["state_get_template"].format(
        cli=_gsm_cli_py(cfg), site=_gsm_site_id(shortcode))
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=int(g.get("cmedit_timeout_s", 120)))
    return parse_gerancell(out, shortcode), out


def run_cutover_gsm_fetch_fdn(ssh, node_name: str, cell_id: str, shortcode: str,
                              log_cb: Callable[[str], None], cfg: dict) -> str:
    """Resolve one GeranCell's full FDN via a verbose get (fallback when the
    FDN was not captured at discovery). Returns "" if not found."""
    g = cfg["gsm"]
    command = g["state_get_template"].format(
        cli=_gsm_cli_py(cfg), site=cell_id)
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=int(g.get("cmedit_timeout_s", 120)))
    info = parse_gerancell(out, shortcode).get(cell_id.upper(), {})
    return info.get("fdn", "")


def run_cutover_gsm_tss(ssh, node_name: str,
                        log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """Read GsmSector/Trx timeslot state on the node. Returns (sectors, out)."""
    g = cfg["gsm"]
    out = ssh.run_amos_command_safe(
        g["tss_command"], node_name, timeout=int(g.get("command_timeout_s", 120)))
    return parse_tss(out), out


def run_cutover_gsm_sector_list(ssh, node_name: str,
                                log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """``lst gsmsector`` → per-Trx adm/op, plus the full sector RDNs. Returns
    (parsed, full_names, out) where full_names maps sector suffix → RDN."""
    g = cfg["gsm"]
    out = ssh.run_amos_command_safe(
        g["gsmsector_list_command"], node_name,
        timeout=int(g.get("command_timeout_s", 120)))
    parsed = parse_gsmsector_list(out)
    full = {}
    for m in re.finditer(r"GsmSector=([^,\s]+)", strip_ansi(out)):
        rdn = m.group(1)
        raw_suffix = rdn.rsplit("-", 1)[-1] if "-" in rdn else rdn
        last_digit = re.search(r"(\d)$", raw_suffix)
        suffix = last_digit.group(1) if last_digit else raw_suffix
        full[suffix] = rdn
    return parsed, full, out


def run_cutover_gsm_set_state(ssh, node_name: str, fdn: str, state: str,
                              log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """BSC ``cmedit set <fdn> state=<state>`` for one GeranCell. Returns (ok, out)."""
    g = cfg["gsm"]
    if not fdn:
        return False, "no GeranCell FDN resolved for set"
    command = g["state_set_template"].format(
        cli=_gsm_cli_py(cfg), fdn=fdn, state=state)
    # Do not expose the cli.py path or the complete ENM FDN in the operator
    # log.  The exact command still goes to SSH; the UI only needs the target
    # GeranCell and requested state.
    cell_id = fdn.rsplit("GeranCell=", 1)[-1].split(",", 1)[0].strip()
    log_cb(f"Set GeranCell={cell_id or '?'} {str(state).upper()}")
    out = ssh.run_amos_command_safe(
        command, node_name, timeout=int(g.get("cmedit_timeout_s", 120)))
    up = out.upper()
    if "ERROR" in up or "FAIL" in up:
        return False, out
    ok = any(p.upper() in up for p in g.get("set_success_patterns", ["SUCCESS"]))
    return ok, out


def run_cutover_gsm_trx_unlock(ssh, node_name: str, sector_mo: str,
                               log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """Node ``ldeb GsmSector=<sector_mo>,Trx`` — unlock every Trx of a sector.
    Returns (ok, out)."""
    g = cfg["gsm"]
    command = g["trx_unlock_template"].format(sector_mo=sector_mo, node=node_name)
    log_cb(f"[{node_name}] {command}")
    if g.get("trx_unlock_confirm", True):
        out = ssh.run_amos_set_with_confirm(
            command, node_name, answer=g.get("trx_confirm_answer", "y"),
            timeout=int(g.get("command_timeout_s", 120)))
    else:
        out = ssh.run_amos_command_safe(
            command, node_name, timeout=int(g.get("command_timeout_s", 120)))
    up = out.upper()
    ok = not ("ERROR" in up or "SYNTAX ERROR" in up or "NOT FOUND" in up)
    return ok, out


def run_cutover_alarms(ssh, node_name: str, log_cb: Callable[[str], None],
                       cfg: dict, wait_for_user=None) -> tuple:
    """Run the alarm command. Returns (ok, output, total_alarms)."""
    alarm = cfg["alarm"]
    out = ssh.run_amos_command_safe(
        alarm["command"], node_name, timeout=alarm["command_timeout_s"])
    total, by_sev, none_active = parse_alarm_summary(
        out, tuple(alarm.get("no_alarm_patterns", ())))
    if none_active:
        log_cb(f"[{node_name}] no active alarms.")
    else:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_sev.items()))
        log_cb(f"[{node_name}] {total} active alarm(s){' — ' + detail if detail else ''}")
    return True, out, total


def parse_fm_alarm_supervision(output: str) -> dict:
    """Parse NetworkElement, supervision id, and active from a cmedit table."""
    states = {}
    for line in (output or "").splitlines():
        match = re.match(r"^\s*(\S+)\s+\d+\s+(true|false)\s*$", line,
                         re.IGNORECASE)
        if match:
            states[match.group(1).upper()] = match.group(2).lower() == "true"
    return states


def run_cutover_activate_alarm(ssh, node_name: str,
                              log_cb: Callable[[str], None], cfg: dict) -> tuple:
    """Set FM alarm supervision active for one node. Returns (ok, output)."""
    a = cfg.get("alarm", {})
    cmd = a.get("activate_command", (
        '!python {cli} "cmedit set {node} fmalarmsupervision active=true"'
    )).format(
        cli=_gsm_cli_py(cfg), node=node_name)
    log_cb(f"[{node_name}] setting FM alarm supervision active=true…")
    out = ssh.run_amos_command_safe(
        cmd, node_name, timeout=int(a.get("activate_timeout_s", 60)))
    up = (out or "").upper()
    ok = not any(pat in up for pat in (
        "ERROR", "SYNTAX ERROR", "COMMAND NOT FOUND", "FAILED"))
    return ok, out


def run_cutover_final_step(ssh, node_name: str, step: FinalStepState,
                           log_cb: Callable[[str], None], cfg: dict,
                           wait_for_user=None) -> tuple:
    """Run one configured verification step. Returns (ok, output, detail)."""
    command = step.command.format(node=node_name)
    log_cb(f"[{node_name}] {step.label}: {command}")
    out = ssh.run_amos_command_safe(command, node_name, timeout=step.timeout_s)

    if step.fail_regex:
        try:
            if re.search(step.fail_regex, out, re.IGNORECASE):
                return False, out, f"matched fail pattern {step.fail_regex!r}"
        except re.error:
            pass
    if step.expect_regex:
        try:
            if not re.search(step.expect_regex, out, re.IGNORECASE):
                return False, out, f"expected {step.expect_regex!r}, not found"
        except re.error:
            pass
        return True, out, "expected pattern found"
    return True, out, "informational"


# ──────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────
class CutoverEngine:
    """Owns the threads, deadlines and shared state for one cut-over run."""

    def __init__(self, form: dict, log_cb: Optional[Callable[[str], None]] = None,
                 cfg: Optional[dict] = None,
                 confirm_cb: Optional[Callable[[str, list], bool]] = None,
                 wait_for_user: Optional[Callable[[str], bool]] = None,
                 live_sink_factory: Optional[Callable[[str], Callable]] = None,
                 log_dir: Optional[str] = None):
        self.form = form or {}
        self.cfg = cfg or load_cutover_config()
        self._confirm_cb = confirm_cb
        self._wait_for_user = wait_for_user
        self._live_sink_factory = live_sink_factory
        self._external_log_cb = log_cb

        self.log_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()

        # Cut Over may span two LTE/NR BBs plus a dedicated GSM BB. Keep one
        # AMOS session per distinct node; previously gsm_node_name was omitted,
        # so GsmSector discovery could only search B01/B02 and silently had no
        # target for the Trx ldeb on B03.
        node_names = []
        seen_nodes = set()
        for field in ("node_name", "node2_name", "gsm_node_name"):
            node = str(self.form.get(field, "")).strip()
            key = node.upper()
            if node and key not in seen_nodes:
                node_names.append(node)
                seen_nodes.add(key)
        self.run = CutoverRun(
            shortcode=str(self.form.get("shortcode", "")).strip(),
            node_names=node_names,
            cfg=self.cfg,
        )
        for name in self.cfg["group_order"]:
            self.run.groups[name] = GroupState(name=name)
        self.run.groups[UNMAPPED] = GroupState(name=UNMAPPED)
        if self.cfg.get("gsm", {}).get("enabled", True):
            self.run.groups[GSM] = GroupState(name=GSM)

        for raw in (self.cfg.get("final_verification", {}).get("steps") or []):
            self.run.final_steps.append(FinalStepState(
                key=raw.get("key", ""), label=raw.get("label", raw.get("key", "")),
                command=raw.get("command", ""), scope=raw.get("scope", "per_node"),
                timeout_s=int(raw.get("timeout_s", 120)),
                expect_regex=raw.get("expect_regex", ""),
                fail_regex=raw.get("fail_regex", ""),
                screenshot=bool(raw.get("screenshot", False)),
            ))

        self.log_dir = log_dir or self._default_log_dir()
        self._created_at = cutover_persistence.now_iso()
        self._preparation_started_at = (
            datetime.now().strftime("%Y%m%d_%H%M%S")
            + "_" + uuid.uuid4().hex[:6]
        )
        self._precutover_root = os.path.join(self.log_dir, "PRE_CUTOVER")
        self._run_dir = os.path.join(
            self._precutover_root, self._preparation_started_at,
        )
        self._checkpoint_path = os.path.join(self._run_dir, "checkpoint.json")
        self._persistence_enabled = bool(
            self.cfg.get("persistence", {}).get("enabled", True)
        )
        self.recovery_checkpoint = (
            cutover_persistence.find_unfinished(
                self._precutover_root, self.run.shortcode, self.run.node_names,
            ) if self._persistence_enabled else None
        )
        self._checkpoint_event = threading.Event()
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread = None
        self._persistence_started = False
        self._manifest_finalized = False
        self._persistence_lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._threads: list = []
        # Set when a manual traffic gate is waiting on the operator.
        self._traffic_gate: dict = {}
        # Background status monitor: after a non-blocking unlock the enable wait
        # runs here (NOT under _action_lock) so the operator can keep unlocking
        # other groups/sectors while cells are polled every few seconds.
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._monitor_lock = threading.Lock()
        # Separate thread for the SLOW background reads (stzrc traffic + sdirc
        # VSWR) so the fast status monitor above stays responsive.
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_stop = threading.Event()
        self._bg_lock = threading.Lock()
        # node -> time.monotonic() of the last sdirc VSWR / stzrc traffic read,
        # so those (heavy) commands run at their own interval rather than every
        # status poll.
        self._vswr_last: dict = {}
        self._traffic_last: dict = {}
        # Site-wide FM supervision is checked exactly once, immediately before
        # the first confirmed unlock command.
        self._alarm_activation_checked = False
        # HC mode for the current preparation run: "pre" (Pre HC) or "post"
        # (Post HC — post-cutover check, creates a Post_CutOver CV).
        self._hc_mode = "pre"
        # LABEL.mos runs once per session, before the first TRFS script.
        self._trfs_label_done = False

    # ── logging ──────────────────────────────────────────────────
    def _default_log_dir(self) -> str:
        try:
            from app_path import get_app_dir
            base = get_app_dir()
        except Exception:
            base = os.getcwd()
        return os.path.join(base, "LOG", self.run.shortcode or "CUTOVER")

    def log(self, msg: str) -> None:
        stamped = f"[{datetime.now():%H:%M:%S}] {msg}"
        try:
            self.log_queue.put_nowait(stamped)
        except Exception:
            pass
        if self._external_log_cb:
            try:
                self._external_log_cb(stamped)
            except Exception:
                pass
        logger.info("[cutover] %s", msg)

    def emit(self, event: CutoverEvent) -> None:
        try:
            self.event_queue.put_nowait(event)
        except Exception:
            pass

    # ── public commands ──────────────────────────────────────────
    def is_busy(self) -> bool:
        return self._action_lock.locked()

    def start_discovery(self, skip_preparation: bool = False,
                        skip_modump: bool = False,
                        hc_mode: str = "pre") -> None:
        # skip_preparation=True → the "Start Unlock" path: skip CV backup,
        # modump and preHC, and go straight to identifying cells + their live
        # status so the operator can unlock directly.
        # skip_modump=True → the "Start HC" path: run preparation but skip the
        # (slow) modump capture — CV backup and preHC still run.
        self._skip_preparation = bool(skip_preparation)
        self._skip_modump = bool(skip_modump)
        self._hc_mode = "post" if hc_mode == "post" else "pre"
        self._ensure_persistence_started()
        self._spawn(self._discovery_worker, f"cutover-{self._hc_mode}hc")

    def start_posthc(self) -> None:
        """Post HC: the post-cutover counterpart of Pre HC. Creates a
        Post_CutOver CV, runs the postHC script (and downloads its log), then
        re-reads cell status. Skips the slow modump, like Pre HC."""
        self.start_discovery(skip_modump=True, hc_mode="post")

    def run_trfs_log(self) -> None:
        """Run the TRFS logging scripts on every node (LABEL.mos once first),
        then download each node's newest TRFS log folder."""
        self._spawn(self._trfs_worker, "cutover-trfs")

    def recover(self, mode: str = "resume") -> None:
        """Reconnect and reconcile an unfinished run before resume/rollback."""
        if mode not in ("resume", "rollback"):
            raise ValueError(f"Unsupported recovery mode: {mode}")
        checkpoint = self.recovery_checkpoint
        if not checkpoint:
            self.log("No unfinished Cut Over checkpoint was found.")
            return
        self._spawn(lambda: self._recovery_worker(checkpoint, mode),
                    f"cutover-recovery-{mode}")

    def recovery_info(self) -> dict:
        if not self.recovery_checkpoint:
            return {}
        try:
            return cutover_persistence.load(self.recovery_checkpoint)
        except Exception:
            return {}

    def close_recovery_as_incomplete(self) -> str:
        checkpoint = self.recovery_checkpoint
        if not checkpoint:
            return ""
        data = cutover_persistence.load(checkpoint)
        path = cutover_persistence.finalize(checkpoint, data, "INCOMPLETE")
        self.recovery_checkpoint = None
        return path

    def unlock_group(self, group: str, sector: Optional[str] = None) -> None:
        # sector=None → the whole band group; sector="1" → only that sector's
        # cells in the group (per-(band group × sector) unlock).
        tag = f"cutover-{group}" + (f"-S{sector}" if sector else "")
        self._spawn(lambda: self._grouped_action([group], sector), tag)

    def unlock_all(self) -> None:
        self._spawn(lambda: self._grouped_action(list(self.cfg["group_order"])),
                    "cutover-all")

    def unlock_sector(self, sector: str) -> None:
        """Unlock one physical sector across every radio group, including GSM.

        Other sectors are deliberately excluded from the target set.
        """
        groups = list(self.cfg["group_order"])
        if self.cfg.get("gsm", {}).get("enabled", True):
            groups.append(GSM)
        self._spawn(lambda: self._grouped_action(groups, str(sector)),
                    f"cutover-all-S{sector}")

    def relock_group(self, group: str, sector: Optional[str] = None) -> None:
        """Roll back one group (optionally one sector) — only cells this
        session unlocked."""
        tag = f"cutover-relock-{group}" + (f"-S{sector}" if sector else "")
        self._spawn(lambda: self._relock_action([group], sector), tag)

    def relock_all(self) -> None:
        """Roll back everything this session unlocked, highest band first."""
        order = list(reversed(list(self.cfg["group_order"])))
        self._spawn(lambda: self._relock_action(order), "cutover-relock-all")

    def lock_group(self, group: str, sector: Optional[str] = None) -> None:
        """Lock a group (optionally one sector) that is already up — the Lock
        button shown once a group has nothing left to unlock. Unlike rollback,
        this locks any currently-unlocked cell, not only ones this run unlocked."""
        tag = f"cutover-lock-{group}" + (f"-S{sector}" if sector else "")
        self._spawn(lambda: self._lock_action([group], sector), tag)

    def run_final_verification(self) -> None:
        self._spawn(self._final_verify_worker, "cutover-verify")

    def share_evidence(self, group: str) -> None:
        """Build the traffic + alarm screenshot for a group on demand and hand
        it off to WhatsApp — the manual counterpart to the old auto-report, so
        evidence still works with the non-blocking unlock flow."""
        self._spawn(lambda: self._report_worker(group),
                    f"cutover-evidence-{group}")

    def _report_worker(self, group: str) -> None:
        run = self.run
        cells = run.cells_of(group)
        by_node: dict = {}
        for c in cells:
            by_node.setdefault(c.node_name, []).append(c)
        if not by_node:
            self.log(f"{group}: no cells to build evidence for.")
            return
        self.log(f"{group}: collecting fresh traffic + alarms per node…")
        previous_phase = run.phase
        run.set_phase(RunPhase.REPORTING, active_group=group)
        try:
            self._report_phase(group, by_node)
        finally:
            if run.phase == RunPhase.REPORTING:
                run.set_phase(previous_phase, active_group="")

    def confirm_traffic(self, group: str, ok: bool) -> None:
        """Resolve a manual traffic gate raised by an unparseable UE column."""
        gate = self._traffic_gate.get(group)
        if gate:
            gate["ok"] = ok
            gate["event"].set()

    def cancel(self) -> None:
        self.run.cancel_event.set()
        self._stop_monitor()
        self.log("Cancel requested. Cells already unlocked stay unlocked — "
                 "cut over does not roll back.")
        for gate in self._traffic_gate.values():
            gate["ok"] = False
            gate["event"].set()
        self._force_disconnect()

    def shutdown(self) -> None:
        self._persist_checkpoint()
        self._stop_monitor()
        self._checkpoint_stop.set()
        self._checkpoint_event.set()
        self.run.cancel_event.set()
        for gate in self._traffic_gate.values():
            gate["ok"] = False
            gate["event"].set()
        for name, sess in list(self.run.sessions.items()):
            try:
                if sess.in_amos:
                    sess.ssh.exit_amos()
            except Exception:
                pass
            try:
                sess.ssh.disconnect()
            except Exception:
                pass
            for extra in ("read_ssh", "bg_ssh"):
                obj = getattr(sess, extra, None)
                if obj is not None:
                    try:
                        obj.disconnect()
                    except Exception:
                        pass
                    setattr(sess, extra, None)
            sess.connected = False
        self.run.sessions.clear()

    # ── checkpoint + manifest ───────────────────────────────────
    def _ensure_persistence_started(self) -> None:
        if not self._persistence_enabled or self._persistence_started:
            return
        self._persistence_started = True
        self.run.on_change = self._checkpoint_event.set

        def _checkpoint_loop():
            interval = float(self.cfg.get("persistence", {}).get(
                "checkpoint_interval_s", 0.5
            ))
            while not self._checkpoint_stop.is_set():
                self._checkpoint_event.wait()
                if self._checkpoint_stop.is_set():
                    break
                # Debounce bursts of cell/status mutations into one small
                # atomic write, so polling does not create disk churn.
                self._checkpoint_stop.wait(max(0.1, interval))
                self._checkpoint_event.clear()
                if self._checkpoint_stop.is_set():
                    break
                self._persist_checkpoint()

        self._checkpoint_thread = threading.Thread(
            target=_checkpoint_loop,
            name="cutover-checkpoint",
            daemon=True,
        )
        self._checkpoint_thread.start()
        self._checkpoint_event.set()

    def _persist_checkpoint(self) -> str:
        if (not self._persistence_enabled or not self._persistence_started
                or self._manifest_finalized):
            return ""
        with self._persistence_lock:
            # A checkpoint writer may have passed the fast check just before
            # finalization acquired this lock. Re-check here so it cannot
            # recreate checkpoint.json after the manifest retires it.
            if self._manifest_finalized:
                return ""
            try:
                with self.run.lock:
                    data = cutover_persistence.snapshot(
                        self.run, self._preparation_started_at,
                        self._created_at, self.cfg,
                    )
                cutover_persistence.write_json_atomic(self._checkpoint_path, data)
                return self._checkpoint_path
            except Exception as exc:
                logger.warning("Could not persist Cut Over checkpoint: %s", exc)
                return ""

    def _finalize_manifest(self, verdict: str) -> str:
        if not self._persistence_enabled or self._manifest_finalized:
            return ""
        with self._persistence_lock:
            with self.run.lock:
                data = cutover_persistence.snapshot(
                    self.run, self._preparation_started_at,
                    self._created_at, self.cfg,
                )
            cutover_persistence.write_json_atomic(self._checkpoint_path, data)
            path = cutover_persistence.finalize(
                self._checkpoint_path, data, verdict,
            )
            self._manifest_finalized = True
        self._checkpoint_stop.set()
        self._checkpoint_event.set()
        self.run.on_change = None
        self.log(f"Immutable run manifest saved: {path}")
        return path

    # ── internals ────────────────────────────────────────────────
    def _spawn(self, target, name: str) -> bool:
        if not self._action_lock.acquire(blocking=False):
            self.log("Another cut-over action is already running — ignoring.")
            return False

        def _wrapped():
            try:
                target()
            except Exception as exc:
                logger.exception("Cut-over worker crashed")
                self.log(f"✗ {name} crashed: {type(exc).__name__}: {exc}")
                self.run.set_phase(RunPhase.FAILED)
            finally:
                self._action_lock.release()

        t = threading.Thread(target=_wrapped, name=name, daemon=True)
        self._threads.append(t)
        t.start()
        return True

    def _force_disconnect(self) -> None:
        """Close channels so threads blocked in recv() unwind immediately."""
        for sess in list(self.run.sessions.values()):
            for ssh in (sess.ssh, sess.read_ssh, sess.bg_ssh):
                if ssh is None:
                    continue
                for attr in ("shell", "client"):
                    obj = getattr(ssh, attr, None)
                    if obj is None:
                        continue
                    try:
                        if attr == "client":
                            tr = obj.get_transport()
                            if tr:
                                tr.close()
                        obj.close()
                    except Exception:
                        pass

    def _wait(self, seconds: float) -> bool:
        """Interruptible sleep. Returns True if cancelled."""
        return self.run.cancel_event.wait(max(0.0, seconds))

    def _save_preparation_log(self, node_name: str, step: str,
                              output: str) -> str:
        """Persist one pre-Cut Over step output under a unique run folder."""
        safe_node = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_name) or "NODE"
        safe_step = re.sub(r"[^A-Za-z0-9_.-]+", "_", step) or "STEP"
        post = getattr(self, "_hc_mode", "pre") == "post"
        folder = os.path.join(self.log_dir, "POST") if post else self._run_dir
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = (f"{safe_node}_{safe_step}_{stamp}.log"
                    if post else f"{safe_node}_{safe_step}.log")
        path = os.path.join(folder, filename)
        with open(path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(output or "")
            if output and not output.endswith("\n"):
                handle.write("\n")
        with self.run.lock:
            self.run.artifacts[f"{node_name}:{step}"] = path
            self.run.touch()
        self.log(f"[{node_name}] {step} log saved: {path}")
        return path

    def _run_preparation_for_node(self, node_name: str,
                                  sess: NodeSession) -> bool:
        """Run CV, modump and preHC in order; all enabled steps must pass."""
        prep = self.cfg.get("preparation", {})
        if not prep.get("enabled", True):
            self.log(f"[{node_name}] pre-Cut Over preparation is disabled.")
            return True

        # preHC is a health check, not a gate: when no script is configured it
        # is skipped (info) rather than run — so a missing reference file never
        # blocks discovery / showing the current cell status.
        mode = getattr(self, "_hc_mode", "pre")
        hc_key = "posthc" if mode == "post" else "prehc"
        hc_label = "POSTHC" if mode == "post" else "PREHC"
        hc_cfg = prep.get(hc_key, {})
        hc_enabled = bool(hc_cfg.get("enabled", True))
        if hc_enabled and not str(hc_cfg.get("script_path", "")).strip():
            self.log(f"[{node_name}] ℹ {hc_label} has no script_path — skipped "
                     f"(info only, not blocking).")
            hc_enabled = False

        steps = [
            (
                "CREATE_CV",
                prep.get("create_cv", {}).get("enabled", True),
                lambda: run_cutover_create_cv(
                    sess.ssh, node_name, self.log, self.cfg,
                    self._wait_for_user, mode=mode,
                ),
            ),
            (
                "MODUMP",
                (prep.get("modump", {}).get("enabled", True)
                 and not getattr(self, "_skip_modump", False)
                 and mode != "post"),
                lambda: run_cutover_modump(
                    sess.ssh, node_name, self.run.shortcode,
                    self._precutover_root,
                    self.log, self.cfg, self._wait_for_user,
                ),
            ),
            (
                hc_label,
                hc_enabled,
                lambda: run_cutover_prehc(
                    sess.ssh, node_name, self.log, self.cfg,
                    self._wait_for_user, mode=mode,
                ),
            ),
        ]

        all_ok = True
        for label, enabled, action in steps:
            if not enabled:
                self.log(f"[{node_name}] {label} skipped by config.")
                continue
            if self.run.is_cancelled():
                return False
            phase_word = "Post-Cut Over" if mode == "post" else "Pre-Cut Over"
            self.log(f"[{node_name}] ── {phase_word}: {label} ──")
            try:
                ok, output = action()
            except Exception as exc:
                ok = False
                output = f"{type(exc).__name__}: {exc}\n"
                self.log(f"[{node_name}] ✗ {label} crashed: {output.strip()}")
            try:
                self._save_preparation_log(node_name, label, output)
            except Exception as exc:
                ok = False
                self.log(f"[{node_name}] ✗ could not save {label} log: {exc}")
            if label == hc_label and ok:
                # The HC script writes its own moshell logfile to
                # ~/Logfile/<date>/…_(Pre|Post)_HC.log — download it too, not
                # just our capture of the command echo. Best-effort.
                try:
                    hc_dir = (os.path.join(self.log_dir, "POST")
                              if mode == "post" else self._run_dir)
                    os.makedirs(hc_dir, exist_ok=True)
                    got = run_cutover_download_hc_log(
                        sess.ssh, node_name, mode, hc_dir, self.log)
                    if got:
                        with self.run.lock:
                            self.run.artifacts[f"{node_name}:{hc_label}_LOG"] = got
                            self.run.touch()
                except Exception as exc:
                    self.log(f"[{node_name}] ⚠ HC logfile download failed: "
                             f"{type(exc).__name__}: {exc}")
            if label == "MODUMP" and ok:
                match = re.search(r"\[SFTP\]\s+Downloaded\s+→\s+(.+)", output or "")
                if match:
                    with self.run.lock:
                        self.run.artifacts[f"{node_name}:MODUMP_ZIP"] = (
                            match.group(1).strip()
                        )
                        self.run.touch()
            if ok:
                self.log(f"[{node_name}] ✓ {label} passed.")
            else:
                all_ok = False
                self.log(f"[{node_name}] ✗ {label} failed.")
                if prep.get("stop_on_failure", True):
                    break
        return all_ok

    # ── TRFS logging ─────────────────────────────────────────────
    def _trfs_worker(self) -> None:
        """Run LABEL.mos once, then the per-node TRFS script, and download each
        node's newest TRFS log folder.

        Independent of Pre HC: connects the nodes itself and identifies each
        baseband's techs with ``pv $rats`` (G=2G, L=4G, N=5G), so it never
        needs prior discovery."""
        run = self.run
        trfs = self.cfg.get("trfs", {})
        if not trfs.get("enabled", True):
            self.log("TRFS logging is disabled in config.")
            return
        run.cancel_event.clear()
        missing = self._ensure_sessions()
        if missing:
            self.log(f"✗ TRFS: could not connect to {', '.join(missing)}.")
        if not run.sessions:
            self.log("✗ TRFS: no node session available.")
            return

        local_dir = os.path.join(self.log_dir, "TRFS")
        os.makedirs(local_dir, exist_ok=True)
        cmd_tmpl = str(trfs.get("command_template", "run {script_path}"))
        label_script = str(trfs.get(
            "label_script",
            "/home/shared/ms260229/INOC/SCRIPTS/DM/LABEL.mos")).strip()
        results_lock = threading.Lock()
        results: dict = {}

        def _worker(node_name, sess):
            if run.is_cancelled():
                return
            try:
                has_2g, has_4g, has_5g, _rats_out = run_cutover_rats(
                    sess.ssh, node_name, self.log, self.cfg)
            except Exception as exc:
                self.log(f"[{node_name}] ✗ TRFS: 'pv $rats' failed "
                         f"({type(exc).__name__}: {exc}) — skipped.")
                return
            script = trfs_script_for_techs(has_2g, has_4g, has_5g, self.cfg)
            techs = "+".join(t for t, on in
                             (("2G", has_2g), ("4G", has_4g), ("5G", has_5g))
                             if on) or "none"
            if not script:
                self.log(f"[{node_name}] ⚠ TRFS: no script for techs {techs} — "
                         f"skipped. Check cutover.trfs.scripts in config.json.")
                return

            # LABEL.mos runs once per session, before the first TRFS script.
            if label_script:
                run_label = False
                with results_lock:
                    if not self._trfs_label_done:
                        self._trfs_label_done = True
                        run_label = True
                if run_label:
                    lbl_cmd = cmd_tmpl.format(script_path=label_script,
                                              node=node_name)
                    self.log(f"[{node_name}] TRFS: {lbl_cmd} (LABEL, first run)")
                    try:
                        sess.ssh.run_amos_command_safe(
                            lbl_cmd, node_name,
                            timeout=int(trfs.get("timeout_s", 1800)))
                    except Exception as exc:
                        self.log(f"[{node_name}] ⚠ LABEL.mos failed: "
                                 f"{type(exc).__name__}: {exc} — continuing "
                                 f"with the TRFS script.")

            self.log(f"[{node_name}] TRFS techs {techs} → {script}")
            ok, detail = run_cutover_trfs(
                sess.ssh, node_name, script, local_dir, self.log, self.cfg)
            with results_lock:
                results[node_name] = (ok, detail)
                if ok:
                    with run.lock:
                        run.artifacts[f"{node_name}:TRFS_LOG"] = detail
                        run.touch()

        self.log(f"TRFS logging starting on {len(run.sessions)} node(s)…")
        self._run_per_node(dict(run.sessions), _worker)

        done = sum(1 for ok, _ in results.values() if ok)
        self.log(f"TRFS logging finished: {done}/{len(results)} node folder(s) "
                 f"downloaded to {local_dir}")
        # Per-node lines for the completion popup: node → folder, or the reason
        # it did not download.
        lines = []
        for node_name, (ok, detail) in sorted(results.items()):
            if ok:
                lines.append(f"✓ {node_name} → {os.path.basename(detail)}")
            else:
                lines.append(f"✗ {node_name}: {detail}")
        self.emit(CutoverEvent(
            kind="trfs_done",
            png_path=local_dir,
            message=(f"TRFS logs: {done}/{len(results)} node folder(s) saved.\n"
                     + "\n".join(lines))))

    # ── recovery ─────────────────────────────────────────────────
    def _recovery_worker(self, checkpoint: str, mode: str) -> None:
        run = self.run
        try:
            data = cutover_persistence.load(checkpoint)
        except Exception as exc:
            run.error = f"Cannot read recovery checkpoint: {exc}"
            run.set_phase(RunPhase.FAILED)
            self.log(f"✗ {run.error}")
            return

        expected_hash = str(data.get("config_sha256", ""))
        current_hash = cutover_persistence.config_hash(self.cfg)
        if expected_hash and expected_hash != current_hash:
            if self.cfg.get("persistence", {}).get(
                    "require_config_match", False):
                run.error = (
                    "Recovery blocked: Cut Over configuration differs from "
                    "the unfinished run. Restore the same configuration "
                    "before resuming."
                )
                run.set_phase(RunPhase.FAILED)
                self.log(f"✗ {run.error}")
                return
            self.log(
                "⚠ Cut Over configuration changed since this checkpoint. "
                "Continuing with live reconciliation; no previous write will "
                "be replayed automatically. Review every confirmation before "
                "sending a new Lock/Unlock command."
            )

        # A checkpoint written before discovery contains no recoverable cell
        # ownership or progress. Treating it as a successful resume used to
        # produce READY with zero rows, leaving every action disabled. Retire
        # the empty record and return to IDLE so the operator can run Start HC
        # or Start Unlock and perform fresh discovery.
        if not data.get("cells"):
            try:
                manifest = cutover_persistence.finalize(
                    checkpoint, data, "INCOMPLETE_EMPTY")
                self.log(f"Empty recovery checkpoint retired: {manifest}")
            except Exception as exc:
                self.log(f"Could not retire empty recovery checkpoint: {exc}")
                run.error = "Recovery checkpoint contains no discovered cells."
                run.set_phase(RunPhase.FAILED)
                return
            self.recovery_checkpoint = None
            run.error = ""
            run.set_phase(RunPhase.IDLE, active_group="")
            self.log(
                "No discovered cells were saved in the unfinished run. "
                "Start again with Start HC, or use Start Unlock to skip "
                "preparation and rediscover live state."
            )
            return

        self._preparation_started_at = str(data.get(
            "run_id", os.path.basename(os.path.dirname(checkpoint))
        ))
        self._created_at = str(data.get("created_at", self._created_at))
        self._run_dir = os.path.dirname(checkpoint)
        self._precutover_root = os.path.dirname(self._run_dir)
        self._checkpoint_path = checkpoint
        cutover_persistence.restore(run, data)
        for name in list(self.cfg["group_order"]) + [UNMAPPED]:
            run.groups.setdefault(name, GroupState(name=name))
        self._ensure_persistence_started()
        run.set_phase(RunPhase.RECOVERING, active_group="")
        self.log(
            f"Recovering unfinished Cut Over {self._preparation_started_at} "
            f"in {mode} mode. No write will be replayed before live readback."
        )

        def _recover_connect(node_name, _unused):
            sess = self._connect_node(node_name)
            if sess is not None:
                with run.lock:
                    run.sessions[node_name] = sess

        self._run_per_node({n: None for n in run.node_names},
                           _recover_connect)
        missing_sessions = [n for n in run.node_names if n not in run.sessions]
        if missing_sessions:
            run.error = "Recovery connection failed: " + ", ".join(missing_sessions)
            run.set_phase(RunPhase.FAILED)
            self.log(f"✗ {run.error}")
            return

        for key, path in run.artifacts.items():
            if not key.endswith(":ALARM_BASELINE") or not os.path.isfile(path):
                continue
            node_name = key.split(":", 1)[0]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    run.alarm_baseline[node_name] = fh.read()
            except Exception:
                pass

        if not self._reconcile_recovered_cells():
            return

        if mode == "rollback":
            run.set_phase(RunPhase.READY, active_group="")
            self._relock_action(list(reversed(list(self.cfg["group_order"]))))
            remaining = sum(
                len(run.relockable_cells_of(g)) for g in self.cfg["group_order"]
            )
            if remaining:
                self.log(
                    f"Recovery rollback incomplete: {remaining} cell(s) still "
                    "require operator action."
                )
                run.set_phase(RunPhase.READY, active_group="")
            else:
                run.set_phase(RunPhase.DONE, active_group="")
                self._finalize_manifest("ROLLED_BACK")
            return

        # Recovery must finish after authoritative live state reconciliation.
        # Traffic and sdirc/VSWR are slow read-only assurance and must never
        # hold the UI in RECOVERING (which disables every operator action).
        # Resume them on the background sessions after READY instead.
        run.error = ""
        run.set_phase(RunPhase.READY, active_group="")
        self._ensure_monitor()
        self._ensure_bg_monitor()
        self.log(
            "✓ Recovery reconciliation complete. Previously attempted cells "
            "were not replayed; remaining untouched cells may be continued. "
            "Traffic and VSWR assurance is continuing in the background."
        )

    def _reconcile_recovered_cells(self) -> bool:
        """Read every saved cell and classify it without replaying a write."""
        run = self.run
        mode = self.cfg["enable_poll"].get("match_mode", "suffix")
        for node_name, sess in run.sessions.items():
            node_cells = [c for c in run.cells if c.node_name == node_name]
            radio_cells = [c for c in node_cells if c.rat != "GSM"]
            gsm_cells = [c for c in node_cells if c.rat == "GSM"]
            staged = {}
            if radio_cells:
                nr_cells = [c for c in radio_cells if c.rat == "NR"]
                if (nr_cells and self.cfg["unlock"].get(
                        "nr_carrier_enabled", True)):
                    try:
                        refs, _ = run_cutover_nr_carrier_refs(
                            sess.ssh, node_name, self.log, self.cfg)
                        for cell in nr_cells:
                            ref = refs.get(cell.cell_dn.upper(), "")
                            if ref:
                                run.set_cell(cell, nr_sector_carrier_ref=ref)
                    except Exception as exc:
                        self.log(f"[{node_name}] recovery sectorCarrierRef "
                                 f"lookup failed: {type(exc).__name__}: {exc}")
                try:
                    ok, _out, rows = run_cutover_st_cell(
                        sess.ssh, node_name, self.log, self.cfg,
                    )
                except Exception as exc:
                    ok, rows = False, []
                    self.log(f"[{node_name}] recovery readback failed: {exc}")
                if ok:
                    for row in rows:
                        cell = match_row(
                            radio_cells, node_name, row, mode=mode)
                        if cell is not None:
                            staged[cell.key] = row
            missing = [c.mo_ref for c in radio_cells if c.key not in staged]
            if missing:
                run.error = (
                    f"Recovery blocked: live state matched "
                    f"{len(staged)}/{len(radio_cells)} LTE/NR cells on "
                    f"{node_name}."
                )
                run.set_phase(RunPhase.FAILED)
                self.log(f"✗ {run.error} Missing: {', '.join(missing[:8])}")
                return False

            for cell in radio_cells:
                row = staged[cell.key]
                admin = (row.admin_state or "").upper()
                op = (row.op_state or "").upper()
                common = {
                    "admin_state": row.admin_state,
                    "op_state": row.op_state,
                    "avail_status": row.avail_status,
                    "ue_count": None,
                    "traffic_samples": 0,
                }
                if cell.was_unlocked_before or cell.already_in_service:
                    run.set_cell(
                        cell, CellStatus.ALREADY_IN_SERVICE,
                        status_detail="pre-existing service — never touched",
                        **common,
                    )
                elif cell.was_unlocked_by_run:
                    if admin == "LOCKED":
                        run.set_cell(
                            cell, CellStatus.RELOCKED,
                            status_detail="live readback: locked", **common,
                        )
                    elif admin == "UNLOCKED" and op == "ENABLED":
                        run.set_cell(
                            cell, CellStatus.ENABLED,
                            status_detail="recovered: enabled; traffic recheck pending",
                            **common,
                        )
                    elif admin == "UNLOCKED":
                        run.set_cell(
                            cell, CellStatus.UNLOCK_SENT,
                            status_detail="recovered: unlocked; enable recheck pending",
                            **common,
                        )
                    else:
                        run.set_cell(
                            cell, CellStatus.ERROR,
                            status_detail=f"ambiguous live state: {admin}/{op}",
                            **common,
                        )
                elif admin == "UNLOCKED":
                    # The checkpoint proves this run never attempted the cell;
                    # another actor changed it, so neither resume nor rollback
                    # may claim ownership.
                    run.set_cell(
                        cell, CellStatus.ALREADY_IN_SERVICE,
                        status_detail="changed outside this run — never touched",
                        already_in_service=True, **common,
                    )
                else:
                    run.set_cell(
                        cell, CellStatus.PENDING,
                        status_detail="reconciled: untouched", **common,
                    )

            # GeranCell is an ENM/BSC object and never appears in the radio
            # node's hgetc administrativeState/operationalState output.  The
            # old recovery code nevertheless demanded it there, which made a
            # GSM-only node fail with "live state matched 0/N cells" and left
            # every action disabled. Reconcile it through its own BSC query.
            if gsm_cells:
                try:
                    info, _out = run_cutover_gsm_states(
                        sess.ssh, node_name, run.shortcode, self.log, self.cfg)
                except Exception as exc:
                    info = {}
                    self.log(f"[{node_name}] GSM recovery readback failed: {exc}")
                gsm_missing = [c.mo_ref for c in gsm_cells
                               if c.cell_dn.upper() not in info]
                if gsm_missing:
                    matched = len(gsm_cells) - len(gsm_missing)
                    run.error = (
                        f"Recovery blocked: live state matched "
                        f"{matched}/{len(gsm_cells)} GSM cells on {node_name}."
                    )
                    run.set_phase(RunPhase.FAILED)
                    self.log(f"✗ {run.error} Missing: "
                             f"{', '.join(gsm_missing[:8])}")
                    return False

                active = str(self.cfg["gsm"].get(
                    "active_value", "ACTIVE")).upper()
                for cell in gsm_cells:
                    rec = info[cell.cell_dn.upper()]
                    state = str(rec.get("state", "")).upper()
                    common = {
                        "geran_state": state,
                        "gsm_fdn": rec.get("fdn") or cell.gsm_fdn,
                    }
                    if cell.was_unlocked_before or cell.already_in_service:
                        run.set_cell(
                            cell, CellStatus.ALREADY_IN_SERVICE,
                            status_detail=state,
                            **common)
                    elif cell.was_unlocked_by_run:
                        if state == active:
                            run.set_cell(
                                cell, CellStatus.ENABLED,
                                status_detail=state,
                                **common)
                        else:
                            run.set_cell(
                                cell, CellStatus.RELOCKED,
                                status_detail=state,
                                **common)
                    elif state == active:
                        run.set_cell(
                            cell, CellStatus.ALREADY_IN_SERVICE,
                            status_detail=state,
                            already_in_service=True, **common)
                    else:
                        run.set_cell(
                            cell, CellStatus.PENDING,
                            status_detail=state, **common)
        self._persist_checkpoint()
        return True

    def _assure_recovered_cells(self) -> None:
        """Re-run read-only enable/traffic assurance for attempted live cells."""
        run = self.run
        for group in self.cfg["group_order"]:
            cells = [
                c for c in run.cells_of(group)
                if c.was_unlocked_by_run
                and c.status in (CellStatus.UNLOCK_SENT, CellStatus.ENABLED)
            ]
            if not cells:
                continue
            by_node = {}
            for cell in cells:
                by_node.setdefault(cell.node_name, []).append(cell)
            run.set_phase(RunPhase.RECOVERING, active_group=group)
            if any(c.status == CellStatus.UNLOCK_SENT for c in cells):
                self._wait_enable_phase(group, by_node)
            self._wait_traffic_phase(group, by_node)
            pending = len(run.unlockable_cells_of(group))
            if pending:
                run.set_group(
                    group, GroupStatus.PENDING,
                    message=f"recovered; {pending} untouched cell(s) remain",
                )
            else:
                self._finish_group(group)

    # ── discovery ────────────────────────────────────────────────
    def _connect_node(self, node_name: str) -> Optional[NodeSession]:
        from integration_runner import IntegrationSSH

        form = self.form
        ssh = IntegrationSSH(
            host=str(form.get("host", "")).strip(),
            port=int(form.get("port", 5023) or 5023),
            username=str(form.get("username", "")).strip(),
            password=str(form.get("password", "")),
            log_callback=lambda m: logger.debug("[%s] %s", node_name, m),
        )
        sess = NodeSession(node_name=node_name, ssh=ssh)
        try:
            if self._live_sink_factory:
                ssh.set_live_sink(self._live_sink_factory(node_name))
            self.log(f"[{node_name}] connecting…")
            ssh.connect(timeout=30)
            sess.connected = True
            self.log(f"[{node_name}] entering AMOS…")
            ssh.enter_amos(node_name,
                           timeout=self.cfg["discovery"]["amos_timeout_s"])
            sess.in_amos = True
        except Exception as exc:
            sess.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"[{node_name}] ✗ connection failed: {sess.last_error}")
            return None

        # Second, read-only session for background status/traffic/VSWR polling,
        # so the monitor never shares the primary PTY with unlock. Best-effort:
        # on any failure the monitor falls back to the primary (action-locked).
        if self.cfg.get("separate_read_session", True):
            try:
                r = IntegrationSSH(
                    host=str(form.get("host", "")).strip(),
                    port=int(form.get("port", 5023) or 5023),
                    username=str(form.get("username", "")).strip(),
                    password=str(form.get("password", "")),
                    log_callback=lambda m: logger.debug("[%s:read] %s",
                                                        node_name, m),
                )
                r.connect(timeout=30)
                r.enter_amos(node_name,
                             timeout=self.cfg["discovery"]["amos_timeout_s"])
                sess.read_ssh = r
                self.log(f"[{node_name}] read-only status session ready "
                         f"(unlock and polling won't contend).")
            except Exception as exc:
                self.log(f"[{node_name}] ⚠ no separate read session "
                         f"({type(exc).__name__}: {exc}) — polling will share "
                         f"the primary session.")
                sess.read_ssh = None
        return sess

    def _ensure_sessions(self) -> list:
        """Connect any node that has no live session yet. Returns the list of
        nodes still missing a session. Used by actions (TRFS) that can run
        without the full Pre HC / discovery flow."""
        run = self.run

        def _connect(node_name, _unused):
            if run.is_cancelled() or node_name in run.sessions:
                return
            sess = self._connect_node(node_name)
            if sess is not None:
                with run.lock:
                    run.sessions[node_name] = sess

        todo = {n: None for n in run.node_names if n not in run.sessions}
        if todo:
            self._run_per_node(todo, _connect)
        return [n for n in run.node_names if n not in run.sessions]

    def _discovery_worker(self) -> None:
        run = self.run
        run.cancel_event.clear()
        run.set_phase(RunPhase.PREPARING)
        self.log(f"Cut Over starting for {', '.join(run.node_names) or '(no nodes)'}")
        if self.cfg.get("dry_run"):
            self.log("DRY RUN is enabled — no unlock command will be sent.")

        # Connect first because every intended node must complete the three
        # pre-Cut Over safeguards before discovery can become READY.
        def _connect(node_name, _unused):
            if run.is_cancelled():
                return
            sess = self._connect_node(node_name)
            if sess is not None:
                with run.lock:
                    run.sessions[node_name] = sess

        self._run_per_node({n: None for n in run.node_names}, _connect)

        missing_sessions = [
            node for node in run.node_names if node not in run.sessions
        ]
        if missing_sessions:
            run.error = (
                "Pre-Cut Over blocked: could not connect to "
                + ", ".join(missing_sessions)
            )
            self.log(f"✗ {run.error}")
            run.set_phase(RunPhase.FAILED)
            self.emit(CutoverEvent(kind="discovery_done", message=run.error))
            return

        if getattr(self, "_skip_preparation", False):
            self.log("⏩ Start Unlock: skipping preparation (CV backup, modump "
                     "and preHC) — going straight to cell discovery.")
        else:
            preparation_failed = []
            prep_lock = threading.Lock()

            def _prepare(node_name, sess):
                if not self._run_preparation_for_node(node_name, sess):
                    with prep_lock:
                        preparation_failed.append(node_name)

            self._run_per_node(dict(run.sessions), _prepare)
            if preparation_failed:
                run.error = (
                    "Pre-Cut Over blocked: preparation failed for "
                    + ", ".join(preparation_failed)
                )
                self.log(f"✗ {run.error}")
                run.set_phase(RunPhase.FAILED)
                self.emit(CutoverEvent(kind="discovery_done", message=run.error))
                return
            self.log("✓ Pre-Cut Over preparation completed on every node.")
        run.set_phase(RunPhase.DISCOVERING)

        all_cells: list = []
        cells_lock = threading.Lock()

        def _discover(node_name, sess):
            if run.is_cancelled():
                return
            try:
                ok, _out, cells = run_cutover_discovery(
                    sess.ssh, node_name, self.log, self.cfg)
                if ok:
                    nr_cells = [c for c in cells if c.rat == "NR"]
                    if (nr_cells and self.cfg["unlock"].get(
                            "nr_carrier_enabled", True)):
                        try:
                            refs, _ref_out = run_cutover_nr_carrier_refs(
                                sess.ssh, node_name, self.log, self.cfg)
                        except Exception as exc:
                            refs = {}
                            self.log(
                                f"[{node_name}] NR sector-carrier lookup failed: "
                                f"{type(exc).__name__}: {exc}")
                        matched = 0
                        for cell in nr_cells:
                            ref = refs.get(cell.cell_dn.upper(), "")
                            if ref:
                                cell.nr_sector_carrier_ref = ref
                                matched += 1
                        if matched != len(nr_cells):
                            self.log(
                                f"[{node_name}] ⚠ sectorCarrierRef matched "
                                f"{matched}/{len(nr_cells)} NRCellDU(s); unmatched "
                                f"NR cells will be blocked from unlock.")
                    with cells_lock:
                        all_cells.extend(cells)
            except Exception as exc:
                self.log(f"[{node_name}] ✗ discovery failed: "
                         f"{type(exc).__name__}: {exc}")

        self._run_per_node(dict(run.sessions), _discover)

        if self.cfg.get("gsm", {}).get("enabled", True):
            try:
                all_cells.extend(self._discover_gsm())
            except Exception as exc:
                self.log(f"✗ GSM discovery failed: "
                         f"{type(exc).__name__}: {exc}")

        if not all_cells:
            run.error = ("No cells were discovered on any node. Check the "
                         "discovery commands in config.json.")
            self.log(f"✗ {run.error}")
            run.set_phase(RunPhase.FAILED)
            self.emit(CutoverEvent(kind="discovery_done", message=run.error))
            return

        with run.lock:
            run.cells = all_cells
            run.by_key = {c.key: c for c in all_cells}
            for grp in run.groups.values():
                grp.cell_keys = []
            for c in all_cells:
                grp = run.groups.get(c.group)
                if grp is None:
                    grp = GroupState(name=c.group)
                    run.groups[c.group] = grp
                grp.cell_keys.append(c.key)
                if c.group == UNMAPPED:
                    c.status = CellStatus.SKIPPED
                    c.status_detail = "band not mapped to a group"
            run.touch()

        # Read each cell's current status right after discovery — the fast
        # ``hgetc … administrativeState|operationalState`` (NOT stzrc) — so the
        # operator immediately sees UNLOCKED/ENABLED vs LOCKED/DISABLED per cell.
        # Traffic (stzrc) and VSWR (sdirc) are read afterwards, in the
        # background, off the separate read session. No "already in service"
        # concept: a cell that is up simply shows ENABLED and can be locked.
        poll = self.cfg["enable_poll"]
        def _initial_status(node_name, sess):
            if run.is_cancelled():
                return
            try:
                rssh = sess.read_ssh or sess.ssh
                _ok, _out, rows = run_cutover_st_cell(
                    rssh, node_name, self.log, self.cfg)
                self._apply_st_rows(node_name, rows, poll)
                tot = [c for c in run.cells
                       if c.node_name == node_name and c.rat != "GSM"]
                if tot:
                    up = sum(1 for c in tot if c.status == CellStatus.ENABLED)
                    self.log(f"[{node_name}] cell status: {up}/{len(tot)} "
                             f"UNLOCKED/ENABLED")
            except Exception as exc:
                self.log(f"[{node_name}] status read failed: "
                         f"{type(exc).__name__}: {exc}")

        self._run_per_node(dict(run.sessions), _initial_status)
        # GSM status (GeranCell + tss) for any node hosting GSM cells.
        for node_name in {c.node_name for c in run.cells if c.rat == "GSM"}:
            if run.is_cancelled():
                break
            try:
                self._gsm_combine_status(node_name)
            except Exception as exc:
                self.log(f"[{node_name}] GSM status read failed: "
                         f"{type(exc).__name__}: {exc}")

        # Alarm baseline, so the evidence can distinguish alarms this cut over
        # caused from ones the site already had.
        if self.cfg["alarm"].get("baseline_before_unlock", True):
            def _alarm_baseline(node_name, sess):
                if run.is_cancelled():
                    return
                try:
                    _ok, out, total = run_cutover_alarms(
                        sess.ssh, node_name, self.log, self.cfg)
                    run.alarm_baseline[node_name] = out
                    self._save_preparation_log(
                        node_name, "ALARM_BASELINE", out,
                    )
                    self.log(f"[{node_name}] alarm baseline: {total} active "
                             f"before cut over.")
                except Exception as exc:
                    self.log(f"[{node_name}] alarm baseline failed: "
                             f"{type(exc).__name__}: {exc}")

            self._run_per_node(dict(run.sessions), _alarm_baseline)

        unmapped = len(run.cells_of(UNMAPPED))
        in_service = sum(1 for c in run.cells if c.already_in_service)
        msg = f"Discovered {len(all_cells)} cell(s) across {len(run.sessions)} node(s)."
        if unmapped:
            msg += (f" {unmapped} in an unmapped band — shown but never "
                    f"unlocked; add the band to cutover.band_groups to include it.")
        if in_service:
            msg += f" {in_service} already in service."
        self.log(msg)
        run.set_phase(RunPhase.READY)
        self.emit(CutoverEvent(kind="discovery_done", message=msg))
        if getattr(self, "_hc_mode", "pre") == "post":
            post_dir = os.path.join(self.log_dir, "POST")
            self.emit(CutoverEvent(
                kind="posthc_done",
                png_path=post_dir,
                message=(f"Post HC completed on {len(run.sessions)} node(s). "
                         "CV output, Post HC command logs, and downloaded "
                         "Post_HC logfiles were saved."),
            ))

        # A site can already be fully unlocked and carrying traffic before this
        # run (nothing to unlock). Read live status now so those cells show
        # ENABLED + UE straight away instead of sitting "pending", then keep the
        # background monitor refreshing status/traffic/VSWR without waiting for
        # an unlock click. This belongs to the non-blocking (background) flow.
        if self.cfg["enable_poll"].get("background_monitor", True):
            try:
                self._initial_status_sweep()
            except Exception as exc:
                self.log(f"initial status read failed: "
                         f"{type(exc).__name__}: {exc}")
            self._ensure_monitor()       # fast status poll
            self._ensure_bg_monitor()    # slow traffic + VSWR, in parallel

    def _initial_status_sweep(self) -> None:
        """One immediate status read per node at READY, so already-enabled cells
        are reflected without an unlock action."""
        run = self.run
        poll = self.cfg["enable_poll"]
        for node_name in list(run.sessions.keys()):
            if run.is_cancelled():
                break
            sess = run.sessions.get(node_name)
            if sess is None or sess.degraded:
                continue
            try:
                _ok, _out, rows = run_cutover_st_cell(
                    sess.ssh, node_name, self.log, self.cfg)
                self._apply_st_rows(node_name, rows, poll)
            except Exception as exc:
                self.log(f"[{node_name}] status read failed: "
                         f"{type(exc).__name__}: {exc}")

    # ── GSM (GeranCell) discovery + unlock ───────────────────────
    def _discover_gsm(self) -> list:
        """List the site's GeranCells (BSC) and bind each to its GsmSector on
        the node, so GSM cells can be unlocked and their combined state read."""
        run = self.run
        cfg = self.cfg
        shortcode = run.shortcode
        if not shortcode:
            self.log("GSM discovery skipped — the run has no site shortcode.")
            return []

        info: dict = {}
        for node_name, sess in run.sessions.items():
            try:
                info, _out = run_cutover_gsm_states(
                    sess.ssh, node_name, shortcode, self.log, cfg)
            except Exception as exc:
                self.log(f"[{node_name}] GeranCell state read failed: "
                         f"{type(exc).__name__}: {exc}")
                continue
            if info:
                break
        if not info:
            self.log(f"No GeranCell found for site {shortcode} — no GSM to track.")
            return []

        gsm_node, sector_full = None, {}
        for node_name, sess in run.sessions.items():
            try:
                _parsed, full, _out = run_cutover_gsm_sector_list(
                    sess.ssh, node_name, self.log, cfg)
            except Exception:
                full = {}
            if full:
                gsm_node, sector_full = node_name, full
                break
        if gsm_node is None:
            gsm_node = next(iter(run.sessions), "")
            self.log("No GsmSector MO on any node — GSM cells will show BSC "
                     "state only (Trx/tss checks unavailable).")

        cells = []
        for cell_id, rec in sorted(info.items()):
            state = rec.get("state", "")
            suffix = gsm_sector_suffix(cell_id, shortcode)
            c = CutoverCell(
                node_name=gsm_node, mo_type="GeranCell", cell_dn=cell_id,
                rat="GSM", band_key=gsm_band_of(cell_id, shortcode) or "GSM900",
                group=GSM, sector=suffix, geran_state=state,
                gsm_fdn=rec.get("fdn", ""),
                gsm_sector_mo=sector_full.get(suffix, ""))
            c.status = CellStatus.PENDING
            c.status_detail = f"GeranCell {state}"
            cells.append(c)
        self.log(f"GSM: {len(cells)} GeranCell(s) for {shortcode} on "
                 f"{gsm_node or '(no node)'}.")
        return cells

    def _run_gsm_group(self, sector: Optional[str] = None,
                       skip_confirmation: bool = False) -> None:
        run = self.run
        cfg = self.cfg
        g = cfg["gsm"]
        cells = run.unlockable_cells_of(GSM, sector)
        if not cells:
            self.log("No GSM cells to unlock.")
            return

        if (not skip_confirmation and self.cfg.get("require_confirmation")
                and self._confirm_cb):
            lines = [f"cmedit set GeranCell={c.cell_dn} state={g['active_value']}"
                     f"    ({c.node_name}, {c.band_key})" for c in cells]
            if not self._confirm_cb(GSM, lines):
                self.log("GSM unlock cancelled at confirmation — nothing sent.")
                return

        node_name = cells[0].node_name
        sess = run.sessions.get(node_name)
        run.set_group(GSM, GroupStatus.RUNNING, started_at=time.monotonic(),
                      message="")
        run.set_phase(RunPhase.UNLOCKING, active_group=GSM)
        where = f" S{sector}" if sector else ""
        self.log(f"── GSM{where}: unlocking {len(cells)} GeranCell(s) ──")
        if sess is None:
            self.log(f"✗ GSM node {node_name} has no session.")
            run.set_group(GSM, GroupStatus.FAILED, message="no session")
            run.set_phase(RunPhase.READY, active_group="")
            return

        dry = bool(cfg.get("dry_run"))
        self._start_group_logs(GSM, [node_name])
        try:
            for c in cells:
                c.was_unlocked_by_run = True
                run.touch(c)
            self._persist_checkpoint()

            # 1) BSC: set each GeranCell ACTIVE.
            for c in cells:
                if run.is_cancelled():
                    break
                if dry:
                    run.set_cell(c, CellStatus.UNLOCK_SENT,
                                 status_detail="[dry run] set ACTIVE",
                                 t_unlock_sent=time.monotonic())
                    continue
                try:
                    fdn = c.gsm_fdn or run_cutover_gsm_fetch_fdn(
                        sess.ssh, node_name, c.cell_dn, run.shortcode,
                        self.log, cfg)
                    if fdn and not c.gsm_fdn:
                        run.set_cell(c, gsm_fdn=fdn)
                    ok, out = run_cutover_gsm_set_state(
                        sess.ssh, node_name, fdn, g["active_value"],
                        self.log, cfg)
                except Exception as exc:
                    ok, out = False, f"{type(exc).__name__}: {exc}"
                if ok:
                    run.set_cell(c, CellStatus.UNLOCK_SENT,
                                 status_detail="GeranCell set ACTIVE",
                                 t_unlock_sent=time.monotonic(),
                                 geran_state=g["active_value"])
                else:
                    run.set_cell(c, CellStatus.UNLOCK_FAILED,
                                 status_detail="cmedit set failed",
                                 last_error=(out or "")[-200:])

            # 2) Node: unlock the Trx of each sector that isn't fully enabled.
            if not dry and not run.is_cancelled():
                try:
                    lst, _full, _out = run_cutover_gsm_sector_list(
                        sess.ssh, node_name, self.log, cfg)
                except Exception:
                    lst = {}
                need = {c.gsm_sector_mo for c in cells
                        if c.gsm_sector_mo and c.status == CellStatus.UNLOCK_SENT}
                for sector_mo in sorted(need):
                    if run.is_cancelled():
                        break
                    suffix = sector_mo.rsplit("-", 1)[-1]
                    trx = lst.get(suffix, {})
                    if trx and all(t.get("op") == "ENABLED" for t in trx.values()):
                        continue     # already all enabled — no ldeb needed
                    try:
                        run_cutover_gsm_trx_unlock(
                            sess.ssh, node_name, sector_mo, self.log, cfg)
                    except Exception as exc:
                        self.log(f"[{node_name}] ✗ Trx unlock {sector_mo}: "
                                 f"{type(exc).__name__}: {exc}")

            # 3) Assurance continues in the background. Do not hold the global
            # action lock for the full GSM enable timeout: the operator must be
            # able to Lock/Re-lock an ACTIVE GeranCell immediately when its Trx
            # stays disabled.
            self._start_gsm_assurance(cells, node_name)
        finally:
            self._stop_group_logs([node_name])
        run.set_phase(
            RunPhase.CANCELLED if run.is_cancelled() else RunPhase.READY,
            active_group="")

    def _start_gsm_assurance(self, cells: list, node_name: str) -> None:
        targets = list(cells)

        def _worker():
            self._gsm_wait_enable(targets, node_name)
            # A Lock/Re-lock action may finish while assurance is polling. Do
            # not overwrite its CANCELLED group result afterward.
            if any(c.status != CellStatus.RELOCKED for c in targets):
                self._finish_gsm_group()

        t = threading.Thread(target=_worker, name=f"cutover-gsm-{node_name}",
                             daemon=True)
        self._threads.append(t)
        t.start()

    def _gsm_wait_enable(self, cells: list, node_name: str) -> None:
        run = self.run
        cfg = self.cfg
        g = cfg["gsm"]
        if cfg.get("dry_run"):
            for c in cells:
                if c.status == CellStatus.UNLOCK_SENT:
                    run.set_cell(c, CellStatus.ENABLED,
                                 status_detail="[dry run] enabled",
                                 t_enabled=time.monotonic())
            return

        targets = [c for c in cells if c.status == CellStatus.UNLOCK_SENT]
        for c in targets:
            run.set_cell(c, CellStatus.WAITING_ENABLE,
                         status_detail="waiting for GeranCell + Trx…")
        if not targets:
            return

        started = time.monotonic()
        deadline = started + float(g.get("enable_timeout_s", 180))
        while True:
            if run.is_cancelled():
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return
            self._gsm_combine_status(node_name)
            pending = [c for c in targets
                       if c.status in (CellStatus.UNLOCK_SENT,
                                       CellStatus.WAITING_ENABLE)]
            if not pending:
                enabled = sum(1 for c in targets
                              if c.status == CellStatus.ENABLED)
                self.log(f"GSM assurance finished: {enabled}/{len(targets)} "
                         f"GeranCell(s) enabled.")
                return
            if time.monotonic() >= deadline:
                stuck = sorted({(c.gsm_sector_mo or c.sector) for c in pending})
                for c in pending:
                    run.set_cell(
                        c, CellStatus.ENABLE_TIMEOUT,
                        status_detail=(f"still disabled after "
                                       f"{int(time.monotonic() - started)}s"))
                msg = (f"GSM still disabled after "
                       f"{int(g.get('enable_timeout_s', 180))}s: "
                       + ", ".join(f"GsmSector={s}" for s in stuck if s))
                self.log(f"✗ {msg}")
                self.emit(CutoverEvent(kind="diagnostic", group=GSM, message=msg))
                return
            if self._wait(float(g.get("poll_interval_s", 15))):
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return

    def _gsm_combine_status(self, node_name: str) -> None:
        """Fold GeranCell state (BSC) + GsmSector/Trx TS state (tss) onto the
        node's GSM cells. A cell is ENABLED only when its GeranCell is ACTIVE
        **and** every timeslot of its GsmSector reads ENABLED."""
        run = self.run
        cfg = self.cfg
        sess = run.sessions.get(node_name)
        if sess is None or sess.degraded:
            return
        active_val = str(cfg["gsm"].get("active_value", "ACTIVE")).upper()
        status_ssh = sess.read_ssh or sess.ssh
        # The read session has its own PTY but is shared by background readers.
        # Without one, briefly borrow the action lock before touching primary.
        fallback_action_lock = sess.read_ssh is None
        acquired = False
        if fallback_action_lock:
            acquired = self._action_lock.acquire(blocking=False)
            if not acquired:
                return
        try:
            with sess.read_lock:
                try:
                    info, _ = run_cutover_gsm_states(
                        status_ssh, node_name, run.shortcode, self.log, cfg)
                except Exception:
                    info = {}
                try:
                    tss, _ = run_cutover_gsm_tss(
                        status_ssh, node_name, self.log, cfg)
                except Exception:
                    tss = {}
                try:
                    trx_by_sector, _full, _ = run_cutover_gsm_sector_list(
                        status_ssh, node_name, self.log, cfg)
                except Exception:
                    trx_by_sector = {}
        finally:
            if acquired:
                self._action_lock.release()

        for c in run.cells:
            if c.rat != "GSM" or c.node_name != node_name:
                continue
            rec = info.get(c.cell_dn.upper(), {})
            st = rec.get("state", c.geran_state)
            if rec.get("fdn") and not c.gsm_fdn:
                c.gsm_fdn = rec["fdn"]
            sec = tss.get(c.sector)
            trx = trx_by_sector.get(c.sector, {})
            geran_ok = (st or "").upper() == active_val
            trx_total = len(trx)
            trx_enabled = sum(
                1 for row in trx.values()
                if (row.get("adm") or "").upper() == "UNLOCKED"
                and (row.get("op") or "").upper() == "ENABLED"
            )
            trx_ok = bool(trx_total and trx_enabled == trx_total)
            # tss is the most granular assurance.  Some locked/starting nodes
            # return no timeslots, so use the direct Trx adm/op table then.
            radio_ok = (bool(sec and sec.get("all_enabled"))
                        if sec and sec.get("total", 0) else trx_ok)
            detail = f"GeranCell {st or '?'}"
            if trx_total:
                detail += f" · TRX {trx_enabled}/{trx_total}"
            if sec is not None:
                detail += f" · TS {sec['enabled']}/{sec['total']}"
            elif not c.gsm_sector_mo:
                detail += " · no GsmSector"
            if geran_ok and radio_ok:
                if c.status != CellStatus.ENABLED:
                    run.set_cell(c, CellStatus.ENABLED, status_detail=detail,
                                 t_enabled=time.monotonic(),
                                 geran_state=st or c.geran_state)
                else:
                    run.set_cell(c, status_detail=detail,
                                 geran_state=st or c.geran_state)
            else:
                # Always publish the latest readback, including after Lock or
                # Re-lock.  Previously those rows retained the write result
                # ("GeranCell set HALTED") forever because only pending/unlock
                # states were refreshed here.
                run.set_cell(c, status_detail=detail,
                             geran_state=st or c.geran_state)

    def _finish_gsm_group(self) -> None:
        run = self.run
        cells = run.cells_of(GSM)
        ok = sum(1 for c in cells if c.status == CellStatus.ENABLED)
        failed = sum(1 for c in cells if c.status in TERMINAL_FAIL)
        if run.is_cancelled():
            status = GroupStatus.CANCELLED
        elif ok and not failed:
            status = GroupStatus.DONE
        elif ok:
            status = GroupStatus.DONE_WITH_FAILURES
        else:
            status = GroupStatus.FAILED
        msg = f"{ok}/{len(cells)} GeranCell(s) enabled"
        if failed:
            msg += f", {failed} not OK"
        run.set_group(GSM, status, finished_at=time.monotonic(), message=msg)
        self.log(f"── GSM: {status.value} — {msg} ──")
        self.emit(CutoverEvent(kind="group_done", group=GSM, message=msg))

    def _relock_gsm(self, sector: Optional[str] = None) -> None:
        run = self.run
        cfg = self.cfg
        g = cfg["gsm"]
        cells = run.relockable_cells_of(GSM, sector)
        if not cells:
            self.log("Nothing to roll back for GSM.")
            return
        node_name = cells[0].node_name
        sess = run.sessions.get(node_name)
        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = [f"cmedit set GeranCell={c.cell_dn} state={g['locked_value']}"
                     f"    ({c.node_name})" for c in cells]
            if not self._confirm_cb("ROLL BACK GSM", lines):
                self.log("GSM rollback cancelled — nothing sent.")
                return
        run.set_phase(RunPhase.UNLOCKING, active_group=GSM)
        dry = bool(cfg.get("dry_run"))
        self.log(f"── Rolling back {len(cells)} GeranCell(s) (set "
                 f"{g['locked_value']}) ──")
        for c in cells:
            if run.is_cancelled():
                break
            if dry:
                run.set_cell(c, CellStatus.RELOCKED,
                             status_detail=f"[dry run] set {g['locked_value']}",
                             admin_state="LOCKED")
                continue
            if sess is None:
                break
            try:
                fdn = c.gsm_fdn or run_cutover_gsm_fetch_fdn(
                    sess.ssh, node_name, c.cell_dn, run.shortcode, self.log, cfg)
                ok, out = run_cutover_gsm_set_state(
                    sess.ssh, node_name, fdn, g["locked_value"], self.log, cfg)
            except Exception as exc:
                ok, out = False, f"{type(exc).__name__}: {exc}"
            if ok:
                run.set_cell(c, CellStatus.RELOCKED,
                             status_detail=f"GeranCell set {g['locked_value']}",
                             geran_state=g["locked_value"])
            else:
                run.set_cell(c, CellStatus.ERROR,
                             status_detail="set state failed",
                             last_error=(out or "")[-200:])
        done = sum(1 for c in cells if c.status == CellStatus.RELOCKED)
        run.set_group(GSM, GroupStatus.CANCELLED,
                      message=f"rolled back {done}/{len(cells)}")
        run.set_phase(RunPhase.READY, active_group="")
        self.emit(CutoverEvent(kind="group_done", group=GSM,
                               message=f"Rolled back {done} GeranCell(s)."))

    # ── group orchestration ──────────────────────────────────────
    def _grouped_action(self, groups: list,
                        sector: Optional[str] = None) -> None:
        run = self.run
        if run.phase != RunPhase.READY:
            self.log(
                f"Unlock blocked: Cut Over is not READY (phase={run.phase.value})."
            )
            return
        run.cancel_event.clear()

        targets = [g for g in groups if run.unlockable_cells_of(g, sector)]
        if not targets:
            where = f" (sector S{sector})" if sector else ""
            self.log(f"No unlockable cells in the selected group(s){where}.")
            return

        self._check_endc_anchor(targets, sector)

        # One confirmation covering everything this click will do.
        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = []
            shown_nr_carriers = set()
            for g in targets:
                for c in run.unlockable_cells_of(g, sector):
                    if g == GSM:
                        lines.append(
                            f"cmedit set GeranCell={c.cell_dn} "
                            f"state={self.cfg['gsm']['active_value']}"
                            f"    ({c.node_name}, {c.band_key})")
                    else:
                        carrier_ref = _nr_carrier_ref(c, self.cfg)
                        carrier_key = (c.node_name, carrier_ref)
                        if carrier_ref and carrier_key not in shown_nr_carriers:
                            carrier_cmd = str(self.cfg["unlock"].get(
                                "nr_carrier_unlock_template", "ldeb {mo_ref}"
                            )).format(
                                mo_ref=carrier_ref, node=c.node_name,
                                band_number=c.band_number, sector=c.sector)
                            lines.append(
                                carrier_cmd
                                + f"    ({c.node_name}, NR carrier dependency)")
                            shown_nr_carriers.add(carrier_key)
                        lines.append(
                            self.cfg["unlock"]["command_template"].format(
                                mo_type=c.mo_type, cell_dn=c.cell_dn,
                                mo_ref=c.mo_ref, node=c.node_name)
                            + f"    ({c.node_name}, {c.band_key})")
            cap = self.cfg.get("max_cells_per_unlock") or 0
            if cap and len(lines) > cap:
                self.log(f"✗ {len(lines)} cells exceeds max_cells_per_unlock={cap}.")
                return
            if not self._confirm_cb(", ".join(targets), lines):
                self.log("Cancelled at the confirmation dialog — nothing was sent.")
                return

        # The first point where an unlock is definitely going to be sent.
        self._ensure_alarm_supervision_before_first_unlock()

        for group in targets:
            if run.is_cancelled():
                break
            self._run_group(group, sector, skip_confirmation=(group == GSM))
            grp = run.groups[group]
            if (grp.status in (GroupStatus.FAILED,)
                    and self.cfg.get("stop_on_group_failure")):
                self.log(f"Stopping after {group} failed (stop_on_group_failure).")
                break

        run.set_phase(RunPhase.CANCELLED if run.is_cancelled() else RunPhase.READY,
                      active_group="")

    def _check_endc_anchor(self, groups: list,
                           sector: Optional[str] = None) -> None:
        """Warn when NR cells are about to be unlocked with no LTE anchor up.

        NR needs its LTE anchor in service. LB/MB/HB cuts across RAT — an
        NR2600 cell sits in HB while its L1800 anchor sits in MB — so unlocking
        a high group first can produce NR cells that can never take traffic,
        for a reason that is purely ordering and looks like a fault.
        """
        endc = self.cfg.get("endc", {})
        if not endc.get("warn_nr_without_anchor", True):
            return
        run = self.run

        nr_pending = [c for g in groups
                      for c in run.unlockable_cells_of(g, sector)
                      if c.rat == "NR"]
        if not nr_pending:
            return
        lte_up = [c for c in run.cells
                  if c.rat == "LTE"
                  and (c.already_in_service
                       or c.status in (CellStatus.ENABLED, CellStatus.TRAFFIC_OK,
                                       CellStatus.WAITING_TRAFFIC))]
        if lte_up:
            return
        lte_total = sum(1 for c in run.cells if c.rat == "LTE")
        if not lte_total:
            return
        self.log(
            f"⚠ {len(nr_pending)} NR cell(s) are about to be unlocked but no "
            f"LTE cell is in service yet. NR needs its LTE anchor up — these "
            f"cells will likely show 0 UEs until an LTE group is unlocked.")
        self.emit(CutoverEvent(
            kind="diagnostic",
            message=(f"{len(nr_pending)} NR cell(s) are being unlocked while no "
                     f"LTE anchor is in service.\n\nNR traffic depends on the "
                     f"LTE anchor, so these cells will probably report 0 UEs. "
                     f"Consider unlocking the LTE band group first.")))

    def _relock_action(self, groups: list,
                       sector: Optional[str] = None) -> None:
        run = self.run
        run.cancel_event.clear()

        if groups == [GSM]:
            self._relock_gsm(sector)
            return

        targets = [(g, run.relockable_cells_of(g, sector)) for g in groups]
        targets = [(g, cells) for g, cells in targets if cells]
        if not targets:
            self.log("Nothing to roll back — no cell in these group(s) was "
                     "unlocked by this run.")
            return

        all_cells = [c for _g, cells in targets for c in cells]
        unlock = self.cfg["unlock"]
        template = (unlock.get("graceful_lock_template")
                    if unlock.get("graceful_lock")
                    else unlock.get("lock_command_template"))

        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = [
                template.format(mo_type=c.mo_type, cell_dn=c.cell_dn,
                                mo_ref=c.mo_ref, node=c.node_name)
                + f"    ({c.node_name}, {c.band_key})"
                for c in all_cells
            ]
            label = "ROLL BACK " + ", ".join(g for g, _ in targets)
            if not self._confirm_cb(label, lines):
                self.log("Rollback cancelled — nothing was sent.")
                return

        run.set_phase(RunPhase.UNLOCKING)
        dry = bool(self.cfg.get("dry_run"))
        self.log(f"── Rolling back {len(all_cells)} cell(s) ──")

        for group, cells in targets:
            by_node: dict = {}
            for c in cells:
                by_node.setdefault(c.node_name, []).append(c)

            def _worker(node_name: str, node_cells: list):
                sess = run.sessions.get(node_name)
                if sess is None:
                    return

                def _on_cell(cell, ok, out, err):
                    if ok:
                        run.set_cell(cell, CellStatus.RELOCKED,
                                     status_detail="re-locked",
                                     admin_state="LOCKED",
                                     op_state="DISABLED", ue_count=None)
                    else:
                        run.set_cell(cell, CellStatus.ERROR,
                                     status_detail=(err or "re-lock failed")[:60],
                                     last_error=err or "")

                try:
                    run_cutover_relock(
                        sess.ssh, node_name, node_cells, self.log, self.cfg,
                        dry_run=dry, cancel_event=run.cancel_event,
                        on_cell=_on_cell)
                except Exception as exc:
                    self.log(f"[{node_name}] ✗ rollback failed: "
                             f"{type(exc).__name__}: {exc}")

            self._run_per_node(by_node, _worker)
            done = sum(1 for c in cells if c.status == CellStatus.RELOCKED)
            run.set_group(group, GroupStatus.CANCELLED,
                          message=f"rolled back {done}/{len(cells)}")
            self.log(f"── {group}: rolled back {done}/{len(cells)} cell(s) ──")

        run.set_phase(RunPhase.READY, active_group="")
        self.emit(CutoverEvent(kind="group_done",
                               message=f"Rolled back {len(all_cells)} cell(s)."))

    def _lock_action(self, groups: list,
                     sector: Optional[str] = None) -> None:
        """Lock a group that is already up (the Lock button). Locks any
        currently-unlocked cell — not only ones this run unlocked — so a site
        that was already unlocked can be taken back down."""
        run = self.run
        run.cancel_event.clear()

        if run.phase != RunPhase.READY:
            self.log(f"Lock blocked: Cut Over is not READY "
                     f"(phase={run.phase.value}).")
            return

        if groups == [GSM]:
            self._lock_gsm(sector)
            return

        targets = [(g, run.lockable_cells_of(g, sector)) for g in groups]
        targets = [(g, cells) for g, cells in targets if cells]
        if not targets:
            self.log("Nothing to lock — no cell in these group(s) is unlocked.")
            return

        all_cells = [c for _g, cells in targets for c in cells]
        unlock = self.cfg["unlock"]
        template = (unlock.get("graceful_lock_template")
                    if unlock.get("graceful_lock")
                    else unlock.get("lock_command_template"))

        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = [
                template.format(mo_type=c.mo_type, cell_dn=c.cell_dn,
                                mo_ref=c.mo_ref, node=c.node_name)
                + f"    ({c.node_name}, {c.band_key})"
                for c in all_cells
            ]
            label = "LOCK " + ", ".join(g for g, _ in targets)
            if not self._confirm_cb(label, lines):
                self.log("Lock cancelled — nothing was sent.")
                return

        run.set_phase(RunPhase.UNLOCKING)
        dry = bool(self.cfg.get("dry_run"))
        self.log(f"── Locking {len(all_cells)} cell(s) ──")

        for group, cells in targets:
            by_node: dict = {}
            for c in cells:
                by_node.setdefault(c.node_name, []).append(c)

            def _worker(node_name: str, node_cells: list):
                sess = run.sessions.get(node_name)
                if sess is None:
                    return

                def _on_cell(cell, ok, out, err):
                    if ok:
                        run.set_cell(cell, CellStatus.RELOCKED,
                                     status_detail="locked",
                                     admin_state="LOCKED",
                                     op_state="DISABLED", ue_count=None)
                    else:
                        run.set_cell(cell, CellStatus.ERROR,
                                     status_detail=(err or "lock failed")[:60],
                                     last_error=err or "")

                try:
                    run_cutover_relock(
                        sess.ssh, node_name, node_cells, self.log, self.cfg,
                        dry_run=dry, cancel_event=run.cancel_event,
                        on_cell=_on_cell, allow_any=True)
                except Exception as exc:
                    self.log(f"[{node_name}] ✗ lock failed: "
                             f"{type(exc).__name__}: {exc}")

            self._run_per_node(by_node, _worker)
            done = sum(1 for c in cells if c.status == CellStatus.RELOCKED)
            run.set_group(group, GroupStatus.CANCELLED,
                          message=f"locked {done}/{len(cells)}")
            self.log(f"── {group}: locked {done}/{len(cells)} cell(s) ──")

        run.set_phase(RunPhase.READY, active_group="")
        self.emit(CutoverEvent(kind="group_done",
                               message=f"Locked {len(all_cells)} cell(s)."))

    def _lock_gsm(self, sector: Optional[str] = None) -> None:
        """Lock GSM cells that are up (set GeranCell HALTED) — the GSM Lock
        button. Locks any currently-active GeranCell, not only this run's."""
        run = self.run
        cfg = self.cfg
        g = cfg["gsm"]
        cells = run.lockable_cells_of(GSM, sector)
        if not cells:
            self.log("Nothing to lock — no active GeranCell.")
            return
        node_name = cells[0].node_name
        sess = run.sessions.get(node_name)
        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = [f"cmedit set GeranCell={c.cell_dn} state={g['locked_value']}"
                     f"    ({c.node_name})" for c in cells]
            if not self._confirm_cb("LOCK GSM", lines):
                self.log("GSM lock cancelled — nothing sent.")
                return
        run.set_phase(RunPhase.UNLOCKING, active_group=GSM)
        dry = bool(cfg.get("dry_run"))
        self.log(f"── Locking {len(cells)} GeranCell(s) (set "
                 f"{g['locked_value']}) ──")
        for c in cells:
            if run.is_cancelled():
                break
            if dry:
                run.set_cell(c, CellStatus.RELOCKED,
                             status_detail=f"[dry run] set {g['locked_value']}",
                             admin_state="LOCKED")
                continue
            if sess is None:
                break
            try:
                fdn = c.gsm_fdn or run_cutover_gsm_fetch_fdn(
                    sess.ssh, node_name, c.cell_dn, run.shortcode, self.log, cfg)
                ok, out = run_cutover_gsm_set_state(
                    sess.ssh, node_name, fdn, g["locked_value"], self.log, cfg)
            except Exception as exc:
                ok, out = False, f"{type(exc).__name__}: {exc}"
            if ok:
                run.set_cell(c, CellStatus.RELOCKED,
                             status_detail=f"GeranCell set {g['locked_value']}",
                             geran_state=g["locked_value"])
            else:
                run.set_cell(c, CellStatus.ERROR, status_detail="set state failed",
                             last_error=(out or "")[-200:])
        done = sum(1 for c in cells if c.status == CellStatus.RELOCKED)
        run.set_group(GSM, GroupStatus.CANCELLED,
                      message=f"locked {done}/{len(cells)}")
        run.set_phase(RunPhase.READY, active_group="")
        self.emit(CutoverEvent(kind="group_done", group=GSM,
                               message=f"Locked {done} GeranCell(s)."))

    def _run_group(self, group: str, sector: Optional[str] = None,
                   skip_confirmation: bool = False) -> None:
        if group == GSM:
            self._run_gsm_group(sector, skip_confirmation=skip_confirmation)
            return
        run = self.run
        grp = run.groups[group]
        cells = run.unlockable_cells_of(group, sector)
        run.set_group(group, GroupStatus.RUNNING, started_at=time.monotonic(),
                      message="")
        run.set_phase(RunPhase.UNLOCKING, active_group=group)
        where = f" S{sector}" if sector else ""
        self.log(f"── {group}{where}: unlocking {len(cells)} cell(s) ──")

        by_node: dict = {}
        for c in cells:
            by_node.setdefault(c.node_name, []).append(c)
        # LTE before NR within the group — NR cannot take traffic until its
        # anchor is up, so sending it second costs nothing and avoids a
        # confusing 0-UE window.
        if self.cfg.get("endc", {}).get("lte_before_nr", True):
            for node_cells in by_node.values():
                node_cells.sort(key=lambda c: 0 if c.rat == "LTE" else 1)

        self._start_group_logs(group, by_node.keys())
        bg = self.cfg["enable_poll"].get("background_monitor", True)
        try:
            unlocked_any = self._unlock_phase(group, by_node)
            if not unlocked_any or run.is_cancelled():
                self._finish_group(group)
                return

            if bg:
                # Non-blocking enable wait: hand the polling to the background
                # monitor and return immediately so the operator can unlock
                # other groups/sectors. The monitor flips each cell to ENABLED
                # when it comes up, and flags a cell that stays UNLOCKED/DISABLED
                # past ``stuck_after_s`` so a real problem is obvious.
                now = time.monotonic()
                for node_cells in by_node.values():
                    for c in node_cells:
                        if c.status == CellStatus.UNLOCK_SENT:
                            run.set_cell(
                                c, CellStatus.WAITING_ENABLE,
                                status_detail="unlocked — waiting for enable",
                                t_unlock_sent=c.t_unlock_sent or now)
                run.set_group(group, GroupStatus.RUNNING,
                              message="unlocked — monitoring in background")
                self.log(f"{group}: unlocked — monitoring status in the "
                         f"background; you can unlock other groups now.")
                self._ensure_monitor()
                self._ensure_bg_monitor()
                return

            run.set_phase(RunPhase.WAIT_ENABLE, active_group=group)
            enabled = self._wait_enable_phase(group, by_node)
            if not enabled or run.is_cancelled():
                self._finish_group(group)
                return

            run.set_phase(RunPhase.WAIT_TRAFFIC, active_group=group)
            self._wait_traffic_phase(group, by_node)

            run.set_phase(RunPhase.REPORTING, active_group=group)
            self._report_phase(group, by_node)
        finally:
            self._stop_group_logs(by_node.keys())
            if not bg:
                self._finish_group(group)

    def _start_group_logs(self, group: str, node_names) -> None:
        """Tee every byte of this group to a file — the audit trail that
        matters when someone asks what was unlocked on a production site."""
        session_dir = os.path.join(self.log_dir, "CUTOVER")
        try:
            os.makedirs(session_dir, exist_ok=True)
        except Exception:
            return
        for node_name in node_names:
            sess = self.run.sessions.get(node_name)
            if not sess:
                continue
            try:
                sess.ssh.start_step_log(os.path.join(
                    session_dir, f"CUTOVER_{group}_{node_name}.log"))
            except Exception:
                pass

    def _stop_group_logs(self, node_names) -> None:
        for node_name in node_names:
            sess = self.run.sessions.get(node_name)
            if not sess:
                continue
            try:
                sess.ssh.stop_step_log()
            except Exception:
                pass

    def _finish_group(self, group: str) -> None:
        run = self.run
        cells = run.cells_of(group)
        ok = sum(1 for c in cells if c.status == CellStatus.TRAFFIC_OK)
        failed = sum(1 for c in cells if c.status in TERMINAL_FAIL)

        if run.is_cancelled():
            status = GroupStatus.CANCELLED
        elif ok and not failed:
            status = GroupStatus.DONE
        elif ok or any(c.status in (CellStatus.ENABLED, CellStatus.TRAFFIC_UNKNOWN)
                       for c in cells):
            status = GroupStatus.DONE_WITH_FAILURES
        else:
            status = GroupStatus.FAILED

        msg = f"{ok}/{len(cells)} cell(s) carrying traffic"
        if failed:
            msg += f", {failed} failed"
        run.set_group(group, status, finished_at=time.monotonic(), message=msg)
        self.log(f"── {group}: {status.value} — {msg} ──")
        self.emit(CutoverEvent(kind="group_done", group=group, message=msg))

    # ── phase 1: unlock ──────────────────────────────────────────
    def _unlock_phase(self, group: str, by_node: dict) -> bool:
        run = self.run
        dry = bool(self.cfg.get("dry_run"))
        results: dict = {}

        def _worker(node_name: str, cells: list):
            sess = run.sessions.get(node_name)
            if sess is None:
                for c in cells:
                    run.set_cell(c, CellStatus.ERROR, status_detail="no session")
                results[node_name] = False
                return

            def _on_cell(cell, ok, out, err):
                if ok:
                    run.set_cell(cell, CellStatus.UNLOCK_SENT,
                                 status_detail="unlock sent",
                                 # ldeb succeeded, so do not keep rendering the
                                 # stale pre-command LOCKED state while the
                                 # status poll waits for operational ENABLED.
                                 admin_state=(cell.admin_state if dry
                                              else "UNLOCKED"),
                                 t_unlock_sent=time.monotonic(),
                                 unlock_output=(out or "")[-2000:],
                                 attempts=cell.attempts + 1)
                else:
                    run.set_cell(cell, CellStatus.UNLOCK_FAILED,
                                 status_detail=(err or "unlock failed")[:60],
                                 last_error=err or "",
                                 unlock_output=(out or "")[-2000:],
                                 attempts=cell.attempts + 1)

            for c in cells:
                c.unlock_command = self.cfg["unlock"]["command_template"].format(
                    mo_type=c.mo_type, cell_dn=c.cell_dn,
                    mo_ref=c.mo_ref, node=node_name)
                c.was_unlocked_by_run = True
                run.touch(c)
            # The ownership marker must reach disk before the first write. If
            # the process dies after sending but before receiving output,
            # recovery still knows that blind replay is forbidden.
            self._persist_checkpoint()
            try:
                ok, _out = run_cutover_unlock(
                    sess.ssh, node_name, cells, self.log, self.cfg,
                    dry_run=dry, cancel_event=run.cancel_event, on_cell=_on_cell)
                results[node_name] = ok
            except Exception as exc:
                self.log(f"[{node_name}] ✗ unlock failed: "
                         f"{type(exc).__name__}: {exc}")
                results[node_name] = False

        self._run_per_node(by_node, _worker)

        any_ok = any(results.values())
        if not any_ok:
            self.log(f"✗ {group}: no cell was unlocked successfully. "
                     f"Check cutover.unlock.command_template in config.json.")
        return any_ok

    def _run_per_node(self, by_node: dict, worker) -> None:
        """Run *worker(node, cells)* for each node, parallel or sequential."""
        if self.cfg["unlock"].get("parallel_nodes") and len(by_node) > 1:
            threads = [
                threading.Thread(target=worker, args=(n, c),
                                 name=f"cutover-{n}", daemon=True)
                for n, c in by_node.items()
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for n, c in by_node.items():
                worker(n, c)

    # ── phase 2: wait for cells to enable ────────────────────────
    def _wait_enable_phase(self, group: str, by_node: dict) -> bool:
        run = self.run
        poll = self.cfg["enable_poll"]
        grp = run.groups[group]

        # Use the explicit action set, not ``is_unlockable``: during recovery
        # these cells are deliberately marked as already attempted so the
        # write cannot be replayed, but their read-only enable assurance still
        # has to continue.
        targets = [
            c for node_cells in by_node.values() for c in node_cells
            if c.status == CellStatus.UNLOCK_SENT
        ]
        for c in targets:
            run.set_cell(c, CellStatus.WAITING_ENABLE, status_detail="waiting…")
        if not targets:
            return False

        if self.cfg.get("dry_run"):
            self.log(f"{group}: DRY RUN — skipping the enable wait.")
            for c in targets:
                run.set_cell(c, CellStatus.ENABLED, status_detail="[dry run]",
                             t_enabled=time.monotonic())
            return True

        started = time.monotonic()
        deadline = started + poll["timeout_s"]
        run.set_group(group, enable_deadline=deadline)
        unmatched_streak: dict = {n: 0 for n in by_node}
        polls = 0

        while True:
            if run.is_cancelled():
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return False
            polls += 1
            run.set_group(group, poll_count=polls)

            for node_name in list(by_node.keys()):
                if run.is_cancelled():
                    break
                sess = run.sessions.get(node_name)
                if sess is None or sess.degraded:
                    continue
                t0 = time.monotonic()
                try:
                    _ok, _out, rows = run_cutover_st_cell(
                        sess.ssh, node_name, self.log, self.cfg)
                    sess.consecutive_failures = 0
                except Exception as exc:
                    sess.consecutive_failures += 1
                    self.log(f"[{node_name}] status poll failed "
                             f"({sess.consecutive_failures}/3): "
                             f"{type(exc).__name__}: {exc}")
                    if sess.consecutive_failures >= 3:
                        sess.degraded = True
                        self.log(f"[{node_name}] marking node degraded — "
                                 f"stopping its status polling.")
                    continue

                elapsed = time.monotonic() - t0
                # A transparent reconnect re-runs `lt all`, which can take
                # minutes. Give that time back rather than letting one
                # reconnect eat the whole enable window.
                credit = poll.get("reconnect_credit_threshold_s", 60)
                if credit and elapsed > credit:
                    deadline += elapsed
                    run.set_group(group, enable_deadline=deadline)
                    self.log(f"[{node_name}] status poll took {elapsed:.0f}s "
                             f"(likely a reconnect) — extending the deadline.")

                matched = self._apply_st_rows(node_name, rows, poll)
                if rows and matched == 0:
                    unmatched_streak[node_name] += 1
                    if unmatched_streak[node_name] >= poll.get("max_unmatched_polls", 3):
                        msg = (f"[{node_name}] status output had {len(rows)} row(s) "
                               f"but none matched the discovered cells. Check "
                               f"cutover.enable_poll.match_mode / row_regex.")
                        self.log(f"✗ {msg}")
                        self.emit(CutoverEvent(kind="diagnostic", group=group,
                                               message=msg))
                        sess.degraded = True
                else:
                    unmatched_streak[node_name] = 0

            pending = [c for c in targets if c.status == CellStatus.WAITING_ENABLE]
            if not pending:
                self.log(f"{group}: all {len(targets)} cell(s) enabled.")
                return True

            if time.monotonic() >= deadline or polls >= poll.get("max_polls", 200):
                # Before reporting a bare timeout, say WHY where we can. The
                # usual cause is the band's radio still being locked, which no
                # amount of further waiting on the cell will fix.
                self._diagnose_stuck(pending)
                self._mark_remaining(
                    pending, CellStatus.ENABLE_TIMEOUT,
                    f"not enabled after {int(time.monotonic() - started)}s")
                enabled = [c for c in targets if c.status == CellStatus.ENABLED]
                self.log(f"✗ {group}: enable timeout — {len(enabled)}/{len(targets)} "
                         f"cell(s) came up.")
                if not enabled:
                    return False
                ratio = len(enabled) / max(1, len(targets))
                if ratio < poll.get("min_enabled_ratio", 0.0):
                    return False
                return self._ask_partial(
                    group,
                    f"{len(targets) - len(enabled)} of {len(targets)} {group} cell(s) "
                    f"did not reach ENABLED within "
                    f"{int(time.monotonic() - started)}s.\n\n"
                    f"Continue with the {len(enabled)} that did?")

            if all(run.sessions[n].degraded for n in by_node
                   if n in run.sessions):
                self._mark_remaining(pending, CellStatus.ERROR,
                                     "all nodes degraded")
                return False

            wait_s = poll["interval_s"]
            if (time.monotonic() - started) > poll.get("backoff_after_s", 120):
                wait_s = poll.get("interval_max_s", 60)
            if self._wait(wait_s):
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return False

    # ── background status monitor (non-blocking enable wait) ─────
    def _ensure_monitor(self) -> None:
        """Start the background status monitor if it isn't already running."""
        with self._monitor_lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            t = threading.Thread(target=self._monitor_loop,
                                 name="cutover-monitor", daemon=True)
            self._monitor_thread = t
            t.start()

    def _stop_monitor(self) -> None:
        self._monitor_stop.set()
        self._bg_stop.set()

    # ── background (slow) traffic + VSWR reader ──────────────────
    def _bg_ssh_for(self, sess):
        """(ssh, lock) for the slow background reads. A dedicated ``bg_ssh`` has
        its own PTY → no lock, fully parallel to status. Otherwise share
        ``read_ssh`` (or the primary) under ``read_lock`` so a slow stzrc/sdirc
        never corrupts a fast status poll."""
        if sess.bg_ssh is not None:
            return sess.bg_ssh, None
        if sess.read_ssh is not None:
            return sess.read_ssh, sess.read_lock
        return sess.ssh, sess.read_lock

    def _ensure_bg_session(self, node_name: str) -> None:
        """Open the node's dedicated background session lazily (off the discovery
        path), so status stays fast and traffic/VSWR run on their own PTY."""
        if not self.cfg.get("separate_read_session", True):
            return
        sess = self.run.sessions.get(node_name)
        if (sess is None or sess.bg_ssh is not None or sess.bg_failed):
            return
        from integration_runner import IntegrationSSH
        form = self.form
        try:
            r = IntegrationSSH(
                host=str(form.get("host", "")).strip(),
                port=int(form.get("port", 5023) or 5023),
                username=str(form.get("username", "")).strip(),
                password=str(form.get("password", "")),
                log_callback=lambda m: logger.debug("[%s:bg] %s", node_name, m),
            )
            r.connect(timeout=30)
            r.enter_amos(node_name,
                         timeout=self.cfg["discovery"]["amos_timeout_s"])
            sess.bg_ssh = r
            self.log(f"[{node_name}] background traffic/VSWR session ready "
                     f"(status stays fast).")
        except Exception as exc:
            sess.bg_failed = True
            self.log(f"[{node_name}] ⚠ no separate traffic/VSWR session "
                     f"({type(exc).__name__}: {exc}) — it will share the status "
                     f"session.")

    def _ensure_bg_monitor(self) -> None:
        with self._bg_lock:
            if self._bg_thread and self._bg_thread.is_alive():
                return
            self._bg_stop.clear()
            t = threading.Thread(target=self._bg_loop,
                                 name="cutover-bg", daemon=True)
            self._bg_thread = t
            t.start()

    def _bg_loop(self) -> None:
        """Poll stzrc (traffic) + sdirc (VSWR) for every node, each on its own
        interval, on the dedicated background session — independent of the fast
        status monitor so status never waits on a slow read."""
        run = self.run
        while not self._bg_stop.is_set():
            if run.cancel_event.is_set():
                break
            if run.phase not in self._MONITOR_ACTIVE_PHASES:
                break
            for node_name in {c.node_name for c in list(run.cells)}:
                if self._bg_stop.is_set() or run.cancel_event.is_set():
                    break
                self._ensure_bg_session(node_name)
                self._maybe_refresh_traffic(node_name)
                self._maybe_refresh_vswr(node_name)
            self._bg_stop.wait(5)
        with self._bg_lock:
            self._bg_thread = None

    _MONITOR_WATCH = (
        CellStatus.UNLOCK_SENT, CellStatus.WAITING_ENABLE,
        CellStatus.ENABLE_TIMEOUT, CellStatus.BLOCKED_BY_DEPENDENCY,
    )

    #: Phases in which the always-on status monitor keeps polling. It stops once
    #: the run is finished/cancelled so it never spins forever after teardown.
    _MONITOR_ACTIVE_PHASES = (
        RunPhase.READY, RunPhase.UNLOCKING, RunPhase.WAIT_ENABLE,
        RunPhase.WAIT_TRAFFIC, RunPhase.REPORTING,
    )

    def _monitor_loop(self) -> None:
        """Always-on status monitor. Every ``monitor_interval_s`` it reads each
        node's cell status and folds it in (``_apply_st_rows`` — which flips a
        cell to ENABLED and records its UE count, including cells that were
        already unlocked before this run), refreshes VSWR, and flags a cell that
        stays UNLOCKED/DISABLED past ``stuck_after_s``. It runs off the action
        lock so unlocking stays possible, and keeps going while the run is live
        so an already-enabled site shows real status without any unlock click."""
        run = self.run
        poll = self.cfg["enable_poll"]
        interval = float(poll.get("monitor_interval_s", 5))
        stuck_after = float(poll.get("stuck_after_s", 60))
        while not self._monitor_stop.is_set():
            if run.cancel_event.is_set():
                break
            if run.phase not in self._MONITOR_ACTIVE_PHASES:
                break                       # run finished/cancelled — exit
            # Poll every non-GSM cell's node. This includes cells still PENDING
            # because the site was pre-unlocked.
            nodes = [(n, run.sessions.get(n))
                     for n in {c.node_name for c in list(run.cells)
                               if c.rat != "GSM"}]
            nodes = [(n, s) for n, s in nodes if s is not None and not s.degraded]
            # Nodes with a separate read session poll on their own PTY and need
            # no lock. If any node must fall back to its primary PTY, the sweep
            # takes the action lock so it never overlaps an unlock; non-blocking,
            # so a running unlock just defers this tick.
            need_lock = any(s.read_ssh is None for _n, s in nodes)
            acquired = False
            if need_lock:
                acquired = self._action_lock.acquire(blocking=False)
                if not acquired:
                    self._monitor_stop.wait(interval)
                    continue
            try:
                for node_name, sess in nodes:
                    if run.cancel_event.is_set():
                        break
                    # Status runs on the fast read session, serialised with any
                    # background reader that fell back to it (read_lock). Traffic
                    # and VSWR are handled by the separate _bg_loop, so a slow
                    # stzrc/sdirc never delays this status poll.
                    rssh = sess.read_ssh or sess.ssh
                    try:
                        with sess.read_lock:
                            _ok, _out, rows = run_cutover_st_cell(
                                rssh, node_name, self.log, self.cfg)
                        self._apply_st_rows(node_name, rows, poll)
                    except Exception as exc:
                        self.log(f"[{node_name}] monitor poll failed: "
                                 f"{type(exc).__name__}: {exc}")
            finally:
                if acquired:
                    self._action_lock.release()

            # GSM assurance already performs this same read every five seconds
            # while an unlock is waiting.  Outside that short window, keep
            # reading GeranCell plus Trx/timeslot state so Lock/Re-lock results
            # are confirmed from live state as well.  Different GSM BBs are
            # checked in parallel when parallel_nodes is enabled.
            gsm_by_node = {
                n: cells for n in {c.node_name for c in list(run.cells)
                                   if c.rat == "GSM"}
                if (cells := [c for c in list(run.cells)
                              if c.rat == "GSM" and c.node_name == n])
                and not any(c.status in (CellStatus.UNLOCK_SENT,
                                         CellStatus.WAITING_ENABLE)
                            for c in cells)
            }

            def _poll_gsm(node_name, _cells):
                try:
                    self._gsm_combine_status(node_name)
                except Exception as exc:
                    self.log(f"[{node_name}] GSM monitor poll failed: "
                             f"{type(exc).__name__}: {exc}")

            self._run_per_node(gsm_by_node, _poll_gsm)
            now = time.monotonic()
            watched = [c for c in list(run.cells)
                       if c.was_unlocked_by_run and c.status in self._MONITOR_WATCH]
            for c in watched:
                if c.status not in (CellStatus.WAITING_ENABLE,
                                    CellStatus.UNLOCK_SENT):
                    continue
                if not c.t_unlock_sent or (now - c.t_unlock_sent) <= stuck_after:
                    continue
                admin = (c.admin_state or "").upper()
                op = (c.op_state or "").upper()
                if admin == "UNLOCKED" and op and op != "ENABLED":
                    run.set_cell(
                        c, CellStatus.ENABLE_TIMEOUT,
                        status_detail=(f"unlocked but still {op} after "
                                       f"{int(now - c.t_unlock_sent)}s — check "
                                       f"the radio/dependency"))
            self.emit(CutoverEvent(kind="progress"))
            self._monitor_stop.wait(interval)
        with self._monitor_lock:
            self._monitor_thread = None

    def _apply_st_rows(self, node_name: str, rows: list, poll: dict) -> int:
        """Fold status rows into the cells. Returns how many rows matched."""
        run = self.run
        mode = poll.get("match_mode", "suffix")
        need_unlocked = poll.get("require_admin_unlocked", True)
        good_states = {s.upper() for s in poll.get("enabled_op_states", ["ENABLED"])}
        matched = 0

        for row in rows:
            cell = match_row(run.cells, node_name, row, mode=mode)
            if cell is None:
                continue
            matched += 1
            fields = {
                "admin_state": row.admin_state,
                "op_state": row.op_state,
                "avail_status": row.avail_status,
                "t_last_seen": time.monotonic(),
            }
            row_ue = getattr(row, "ue_count", None)
            if row_ue is not None:
                fields["ue_count"] = row_ue
            is_up = row.op_state.upper() in good_states
            if need_unlocked and row.admin_state:
                is_up = is_up and row.admin_state.upper() == "UNLOCKED"

            # ENABLE_TIMEOUT / BLOCKED_BY_DEPENDENCY are recoverable while the
            # monitor runs: a cell flagged stuck that later comes up flips back
            # to ENABLED instead of staying red forever.
            if is_up and cell.status in (CellStatus.WAITING_ENABLE,
                                         CellStatus.UNLOCK_SENT,
                                         CellStatus.ENABLE_TIMEOUT,
                                         CellStatus.BLOCKED_BY_DEPENDENCY,
                                         CellStatus.PENDING):
                # Show the actual states, e.g. "UNLOCKED/ENABLED". A PENDING cell
                # flipping here means the site was already unlocked before this
                # run — reflect it instead of leaving the row grey.
                adm = (row.admin_state or "UNLOCKED").upper()
                op = (row.op_state or "ENABLED").upper()
                run.set_cell(cell, CellStatus.ENABLED,
                             status_detail=f"{adm}/{op}",
                             t_enabled=time.monotonic(), **fields)
            else:
                run.set_cell(cell, **fields)
        return matched

    def _maybe_refresh_vswr(self, node_name: str, force: bool = False) -> None:
        """Run ``sdirc`` for a node and attach per-port VSWR to its cells.

        Skips unless VSWR is enabled, the node has at least one ENABLED cell to
        annotate, and ``vswr.interval_s`` has elapsed since the last capture
        (``force`` overrides the interval). Runs on the same single PTY as the
        status polls, so callers must already hold that node's turn."""
        v = self.cfg.get("vswr", {})
        if not v.get("enabled", True):
            return
        run = self.run
        sess = run.sessions.get(node_name)
        if sess is None or sess.degraded:
            return
        has_vswr_candidate = any(
            c.node_name == node_name
            and (c.rat == "GSM" or c.status in (
                CellStatus.ENABLED, CellStatus.WAITING_TRAFFIC,
                CellStatus.TRAFFIC_OK))
            for c in run.cells)
        if not has_vswr_candidate:
            return
        now = time.monotonic()
        last = self._vswr_last.get(node_name, 0.0)
        if not force and last and (now - last) < float(v.get("interval_s", 300)):
            return
        self._vswr_last[node_name] = now
        self.log(f"[{node_name}] reading VSWR (sdirc)…")
        rssh, lock = self._bg_ssh_for(sess)
        try:
            if lock is not None:
                with lock:
                    ok, _out, res = run_cutover_vswr(
                        rssh, node_name, self.log, self.cfg)
            else:
                ok, _out, res = run_cutover_vswr(
                    rssh, node_name, self.log, self.cfg)
        except Exception as exc:
            self.log(f"[{node_name}] VSWR read failed: "
                     f"{type(exc).__name__}: {exc}")
            return
        if ok:
            self._apply_vswr(node_name, res)

    def _maybe_refresh_traffic(self, node_name: str, force: bool = False) -> None:
        """Read UE (traffic) via ``stzrc`` on the read session and fold it onto
        the node's cells, on its own interval. Status (ENABLED/LOCKED) comes
        from the fast hgetc poll; this only adds the UE count, so it can run
        slowly in the background without holding up the status view."""
        traffic = self.cfg.get("traffic", {})
        run = self.run
        sess = run.sessions.get(node_name)
        if sess is None or sess.degraded:
            return
        has_enabled = any(
            c.node_name == node_name and c.rat != "GSM"
            and c.status in (CellStatus.ENABLED, CellStatus.WAITING_TRAFFIC,
                             CellStatus.TRAFFIC_OK)
            for c in run.cells)
        if not has_enabled:
            return
        now = time.monotonic()
        last = self._traffic_last.get(node_name, 0.0)
        if not force and last and (now - last) < float(traffic.get("interval_s", 20)):
            return
        self._traffic_last[node_name] = now
        rssh, lock = self._bg_ssh_for(sess)
        try:
            if lock is not None:
                with lock:
                    ok, _out, res = run_cutover_traffic(
                        rssh, node_name, self.log, self.cfg)
            else:
                ok, _out, res = run_cutover_traffic(
                    rssh, node_name, self.log, self.cfg)
        except Exception as exc:
            self.log(f"[{node_name}] traffic read failed: "
                     f"{type(exc).__name__}: {exc}")
            return
        if not ok or res is None or not res.ok:
            return
        mode = self.cfg["enable_poll"].get("match_mode", "suffix")
        threshold = max(1, int(traffic.get("ue_threshold", 1)))
        for c in run.cells:
            if c.node_name != node_name or c.rat == "GSM":
                continue
            ue = ue_for_cell(res.counts, c, mode=mode)
            if ue is None:
                continue
            fields = {"ue_count": ue, "ue_peak": max(c.ue_peak, ue)}
            if c.is_live_enabled and ue >= threshold:
                run.set_cell(c, CellStatus.TRAFFIC_OK,
                             status_detail=f"traffic {ue} UE",
                             t_traffic_ok=time.monotonic(), **fields)
            else:
                run.set_cell(c, **fields)

    def _ensure_alarm_supervision_before_first_unlock(self) -> None:
        """Check FM supervision once on the first unlock; set only false nodes."""
        a = self.cfg.get("alarm", {})
        if not a.get("activate_enabled", True):
            return
        if self._alarm_activation_checked:
            return
        self._alarm_activation_checked = True
        run = self.run
        usable = [(name, sess) for name, sess in run.sessions.items()
                  if sess is not None and not sess.degraded]
        if not usable:
            self.log("⚠ FM alarm supervision check skipped — no live session.")
            return
        context_node, sess = usable[0]
        check = a.get("activate_check_command", (
            '!python {cli} "cmedit get {shortcode}* '
            'fmalarmsupervision.active -t"')).format(
                cli=_gsm_cli_py(self.cfg), shortcode=run.shortcode)
        self.log("Checking FM alarm supervision before the first unlock…")
        try:
            out = sess.ssh.run_amos_command_safe(
                check, context_node, timeout=int(a.get("activate_timeout_s", 60)))
        except Exception as exc:
            self.log("⚠ FM alarm supervision check failed — no setting was "
                     f"changed ({type(exc).__name__}: {exc}).")
            return
        states = parse_fm_alarm_supervision(out)
        if not states:
            self.log("⚠ FM alarm supervision response could not be parsed — "
                     "no setting was changed.")
            return
        false_nodes = [name for name in run.node_names
                       if states.get(name.upper()) is False]
        true_nodes = [name for name in run.node_names
                      if states.get(name.upper()) is True]
        if true_nodes:
            self.log("FM alarm supervision already active: "
                     + ", ".join(true_nodes))
        if not false_nodes:
            self.log("✓ FM alarm supervision check complete — nothing to set.")
            return
        if self.cfg.get("dry_run"):
            self.log("DRY RUN — would set FM alarm supervision active=true for "
                     + ", ".join(false_nodes))
            return
        for node_name in false_nodes:
            try:
                ok, _out = run_cutover_activate_alarm(
                    sess.ssh, node_name, self.log, self.cfg)
            except Exception as exc:
                ok = False
                self.log(f"[{node_name}] FM alarm supervision set failed: "
                         f"{type(exc).__name__}: {exc}")
            if ok:
                self.log(f"[{node_name}] ✓ FM alarm supervision set active=true.")
            else:
                self.log(f"[{node_name}] ⚠ could not set FM alarm supervision.")

    def _apply_vswr(self, node_name: str, res) -> None:
        """Fold a :class:`VswrResult` onto the node's cells by DN."""
        run = self.run
        by_cell = res.by_cell or {}
        by_gsm_token = getattr(res, "by_gsm_token", {}) or {}
        worst_thr = float(self.cfg.get("vswr", {}).get("warn_threshold", 1.5))
        annotated = 0
        for cell in run.cells:
            if cell.node_name != node_name:
                continue
            dn = cell.cell_dn.upper()
            if cell.rat == "GSM":
                # M8239L1 -> L1; M8239S2 also accepts sdirc's GT=...-2.
                match = re.search(r"[89]([^89]+)$", dn, re.IGNORECASE)
                token = match.group(1).upper() if match else ""
                ports = by_gsm_token.get(token)
                if not ports and token.startswith("S"):
                    ports = by_gsm_token.get(token[1:])
            else:
                ports = by_cell.get(dn)
                if ports is None:                # tolerate a differing prefix
                    ports = next((p for k, p in by_cell.items()
                                  if k.endswith(dn) or dn.endswith(k)), None)
            if not ports:
                continue
            run.set_cell(cell, vswr_ports=list(ports))
            annotated += 1
        if annotated:
            flagged = sum(
                1 for c in run.cells
                if c.node_name == node_name and (c.vswr_worst or 0) > worst_thr)
            note = f" — {flagged} above {worst_thr}" if flagged else ""
            self.log(f"[{node_name}] VSWR updated for {annotated} cell(s){note}.")

    def _diagnose_stuck(self, cells: list) -> None:
        """Explain cells that never enabled, instead of a bare timeout.

        Checks the band's radio (``st B<band>``) once per band. If the radio is
        itself locked, the cell was never going to come up and the operator
        needs to unlock the radio — a message worth far more than "timeout".
        """
        if not self.cfg.get("diagnosis", {}).get("enabled", True):
            return
        run = self.run
        checked: set = set()

        for cell in cells:
            if cell.avail_status.upper() == "DEPENDENCY_LOCKED":
                run.set_cell(cell, dependency_locked=True)

            key = (cell.node_name, cell.band_number)
            if key in checked or cell.band_number < 0:
                continue
            checked.add(key)
            sess = run.sessions.get(cell.node_name)
            if sess is None or sess.degraded:
                continue
            try:
                _ok, _out, summary = run_cutover_radio_status(
                    sess.ssh, cell.node_name, cell.band_number,
                    self.log, self.cfg)
            except Exception as exc:
                self.log(f"[{cell.node_name}] radio check failed: "
                         f"{type(exc).__name__}: {exc}")
                continue

            if summary["total"] and (summary["locked"] or summary["disabled"]):
                msg = (f"[{cell.node_name}] B{cell.band_number} radio is "
                       f"{summary['locked']} locked / {summary['disabled']} "
                       f"disabled of {summary['total']} — the cells cannot come "
                       f"up until the radio is unlocked.")
                self.log(f"✗ {msg}")
                self.emit(CutoverEvent(kind="diagnostic", message=msg))
                for c in cells:
                    if (c.node_name == cell.node_name
                            and c.band_number == cell.band_number):
                        run.set_cell(c, CellStatus.BLOCKED_BY_DEPENDENCY,
                                     dependency_locked=True,
                                     status_detail=f"B{c.band_number} radio locked")

    def _check_barred(self, cells: list) -> list:
        """Drop cells that are barred — they can never attract a UE.

        Returns the cells still worth waiting on. Without this, a barred cell
        burns the entire traffic timeout and reports nothing about the cause.
        """
        diag = self.cfg.get("diagnosis", {})
        if not diag.get("enabled", True) or not diag.get(
                "check_barred_before_traffic", True):
            return cells
        run = self.run
        keep = []
        for cell in cells:
            sess = run.sessions.get(cell.node_name)
            if sess is None or sess.degraded:
                keep.append(cell)
                continue
            try:
                _ok, _out, barred = run_cutover_barred_check(
                    sess.ssh, cell.node_name, cell, self.log, self.cfg)
            except Exception:
                keep.append(cell)
                continue
            run.set_cell(cell, cell_barred=barred)
            if barred is True:
                self.log(f"✗ [{cell.node_name}] {cell.mo_ref} is BARRED — no UE "
                         f"can camp on it, so it will never report traffic.")
                run.set_cell(cell, CellStatus.BARRED,
                             status_detail="barred — no UE can camp")
            else:
                keep.append(cell)
        return keep

    def _mark_remaining(self, cells: list, status: CellStatus, detail: str) -> None:
        for c in cells:
            if c.status not in TERMINAL_FAIL and c.status != CellStatus.TRAFFIC_OK:
                self.run.set_cell(c, status, status_detail=detail)

    def _ask_partial(self, group: str, message: str) -> bool:
        if not self._wait_for_user:
            return True
        try:
            return bool(self._wait_for_user(message))
        except Exception:
            return True

    # ── phase 3: wait for traffic ────────────────────────────────
    def _wait_traffic_phase(self, group: str, by_node: dict) -> None:
        run = self.run
        traffic = self.cfg["traffic"]
        grp = run.groups[group]

        targets = [c for c in run.cells_of(group) if c.status == CellStatus.ENABLED]
        if not targets:
            return
        # A barred cell is up but unreachable to UEs — find that out now
        # rather than after a full timeout.
        targets = self._check_barred(targets)
        if not targets:
            return
        for c in targets:
            run.set_cell(c, CellStatus.WAITING_TRAFFIC, status_detail="waiting for UE…")

        if self.cfg.get("dry_run"):
            self.log(f"{group}: DRY RUN — skipping the traffic wait.")
            for c in targets:
                run.set_cell(c, CellStatus.TRAFFIC_OK, status_detail="[dry run]")
            return

        threshold = int(traffic.get("ue_threshold", 1))
        use_peak = bool(traffic.get("use_peak", True))
        need_samples = max(1, int(traffic.get("required_consecutive_samples", 2)))
        started = time.monotonic()
        deadline = started + traffic["timeout_s"]
        run.set_group(group, traffic_deadline=deadline)
        parse_failed_once = False

        while True:
            if run.is_cancelled():
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return

            for node_name in list(by_node.keys()):
                sess = run.sessions.get(node_name)
                if sess is None or sess.degraded:
                    continue
                try:
                    ok, out, res = run_cutover_traffic(
                        sess.ssh, node_name, self.log, self.cfg)
                except Exception as exc:
                    self.log(f"[{node_name}] traffic poll failed: "
                             f"{type(exc).__name__}: {exc}")
                    continue

                run.set_group(group, traffic_output=out,
                              traffic_command=traffic["command"])
                if not ok:
                    # Command rejected by moshell — no point polling further.
                    self._mark_remaining(
                        [c for c in targets if c.node_name == node_name],
                        CellStatus.ERROR, "traffic command rejected")
                    sess.degraded = True
                    continue

                if res is None or not res.ok:
                    parse_failed_once = True
                    if res is not None and res.warning:
                        self.log(f"[{node_name}] {res.warning}")
                    continue

                match_mode = self.cfg["enable_poll"].get("match_mode", "suffix")
                for c in targets:
                    if c.node_name != node_name:
                        continue
                    ue = ue_for_cell(res.counts, c, mode=match_mode)
                    if ue is None:
                        continue
                    peak = max(c.ue_peak, ue)
                    effective = peak if use_peak else ue
                    # Require N consecutive samples at/above threshold so a
                    # single transient UE does not end the gate early.
                    samples = c.traffic_samples + 1 if ue >= threshold else 0
                    fields = {"ue_count": ue, "ue_peak": peak,
                              "traffic_samples": samples}
                    confirmed = (effective >= threshold and samples >= need_samples)
                    if confirmed and c.status == CellStatus.WAITING_TRAFFIC:
                        run.set_cell(c, CellStatus.TRAFFIC_OK,
                                     status_detail=f"UE {ue}",
                                     t_traffic_ok=time.monotonic(), **fields)
                    else:
                        detail = f"UE {ue}"
                        if ue >= threshold and samples < need_samples:
                            detail += f" ({samples}/{need_samples} samples)"
                        run.set_cell(c, status_detail=detail, **fields)

                # VSWR (sdirc) alongside traffic, on its own long interval.
                self._maybe_refresh_vswr(node_name)

            pending = [c for c in targets if c.status == CellStatus.WAITING_TRAFFIC]
            if not pending:
                self.log(f"{group}: all {len(targets)} cell(s) carrying traffic.")
                return

            # The UE column could not be located anywhere. Rather than invent a
            # number, capture the output and let the operator judge it.
            if parse_failed_once and not any(c.ue_count is not None for c in targets):
                policy = traffic.get("on_parse_failure", "manual_confirm")
                if policy == "pass":
                    for c in pending:
                        run.set_cell(c, CellStatus.TRAFFIC_OK,
                                     status_detail="UE unparseable (on_parse_failure=pass)")
                    return
                if policy == "fail":
                    self._mark_remaining(pending, CellStatus.TRAFFIC_TIMEOUT,
                                         "UE column unparseable")
                    return
                self._manual_traffic_gate(group, pending, by_node)
                return

            if time.monotonic() >= deadline:
                self._mark_remaining(
                    pending, CellStatus.TRAFFIC_TIMEOUT,
                    f"no traffic after {int(time.monotonic() - started)}s")
                self.log(f"✗ {group}: traffic timeout for {len(pending)} cell(s).")
                return

            if self._wait(traffic["interval_s"]):
                self._mark_remaining(targets, CellStatus.CANCELLED, "cancelled")
                return

    def _manual_traffic_gate(self, group: str, pending: list, by_node: dict) -> None:
        """UE column unreadable: render what we have and ask the operator."""
        run = self.run
        for c in pending:
            run.set_cell(c, CellStatus.TRAFFIC_UNKNOWN,
                         status_detail="UE column unreadable — check screenshot")

        self._collect_alarms(by_node)
        png = self._render_group_png(group)
        gate = {"event": threading.Event(), "ok": False}
        self._traffic_gate[group] = gate
        self.emit(CutoverEvent(
            kind="confirm_traffic", group=group, png_path=png,
            message=("The UE column could not be read from the traffic output, "
                     "so traffic cannot be confirmed automatically.\n\n"
                     "Check the captured output, then confirm whether these "
                     "cells are carrying traffic.")))
        gate["event"].wait()
        self._traffic_gate.pop(group, None)

        if gate["ok"]:
            for c in pending:
                run.set_cell(c, CellStatus.TRAFFIC_OK,
                             status_detail="confirmed by operator")
        else:
            for c in pending:
                run.set_cell(c, CellStatus.TRAFFIC_TIMEOUT,
                             status_detail="not confirmed")

    # ── phase 4: evidence ────────────────────────────────────────
    def _collect_alarms(self, by_node: dict) -> None:
        for node_name in by_node:
            sess = self.run.sessions.get(node_name)
            if sess is None or sess.degraded:
                continue
            try:
                _ok, out, total = run_cutover_alarms(
                    sess.ssh, node_name, self.log, self.cfg)
                sess.alarm_output = out
                sess.alarm_count = total
            except Exception as exc:
                self.log(f"[{node_name}] alarm check failed: "
                         f"{type(exc).__name__}: {exc}")

    def _render_group_png(self, group: str,
                          node_evidence: Optional[dict] = None) -> str:
        run = self.run
        grp = run.groups[group]
        report = self.cfg["report"]
        try:
            from config_loader import TerminalStyle
            from terminal_renderer import render_multi_command_screenshot
        except Exception as exc:
            self.log(f"Could not import the screenshot renderer: {exc}")
            return ""

        alarm_cmd = self.cfg["alarm"]["command"]
        pairs = []
        if node_evidence is not None:
            # Keep traffic and alarms adjacent for each BB. This makes a
            # multi-node screenshot auditable: every alarm block is visibly
            # associated with the node where its traffic command ran.
            for node_name in run.node_names:
                item = node_evidence.get(node_name)
                if not item:
                    continue
                traffic_out = item.get("traffic_output", "")
                if traffic_out:
                    pairs.append((f"[{node_name}] {item['traffic_command']}",
                                  traffic_out))
                alarm_out = item.get("alarm_output", "")
                if alarm_out:
                    baseline = run.alarm_baseline.get(node_name)
                    if baseline:
                        new = diff_alarms(baseline, alarm_out)
                        alarm_out += ("\n\n--- NEW since cut over started ---\n"
                                      + ("\n".join(new) if new else
                                         "(none — no new alarms)"))
                    pairs.append((f"[{node_name}] {alarm_cmd}", alarm_out))
        else:
            # Compatibility path used by the manual traffic confirmation gate.
            if grp.traffic_output:
                pairs.append((grp.traffic_command or
                              self.cfg["traffic"]["command"],
                              grp.traffic_output))
            for node_name, sess in run.sessions.items():
                if sess.alarm_output:
                    pairs.append((f"[{node_name}] {alarm_cmd}",
                                  sess.alarm_output))
        if not pairs:
            return ""

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(self.log_dir, report["screenshot_subdir"])
        filename = report["filename_template"].format(
            shortcode=run.shortcode or "SITE", group=group, timestamp=ts)
        path = os.path.join(out_dir, filename)
        evidence_nodes = ([n for n in run.node_names if n in node_evidence]
                          if node_evidence is not None else run.node_names)
        title = report["title_template"].format(
            shortcode=run.shortcode or "SITE", group=group,
            nodes=", ".join(evidence_nodes))

        style = TerminalStyle(**report["terminal_style"])
        try:
            with _render_lock:
                render_multi_command_screenshot(
                    pairs, style=style, save_path=path, title=title,
                    max_width=report.get("max_width", 1600))
        except Exception as exc:
            self.log(f"✗ Could not render the {group} screenshot: "
                     f"{type(exc).__name__}: {exc}")
            return ""

        run.set_group(group, screenshot_path=path)
        self.log(f"{group}: screenshot saved to {path}")
        return path

    def _report_phase(self, group: str, by_node: dict) -> None:
        run = self.run
        evidence = {}
        evidence_lock = threading.Lock()
        traffic_cmd = self.cfg["traffic"]["command"]
        alarm_cmd = self.cfg["alarm"]["command"]

        def _collect_node(node_name, _cells):
            sess = run.sessions.get(node_name)
            if sess is None or sess.degraded:
                self.log(f"[{node_name}] evidence skipped: no healthy session.")
                return
            item = {
                "traffic_command": traffic_cmd.format(node=node_name),
                "traffic_output": "",
                "alarm_output": "",
                "alarm_count": 0,
            }
            try:
                _ok, out, _parsed = run_cutover_traffic(
                    sess.ssh, node_name, self.log, self.cfg)
                item["traffic_output"] = out or ""
            except Exception as exc:
                item["traffic_output"] = (
                    f"Traffic collection failed: {type(exc).__name__}: {exc}")
                self.log(f"[{node_name}] traffic evidence failed: "
                         f"{type(exc).__name__}: {exc}")
            try:
                _ok, out, total = run_cutover_alarms(
                    sess.ssh, node_name, self.log, self.cfg)
                item["alarm_output"] = out or ""
                item["alarm_count"] = total or 0
                sess.alarm_output = out
                sess.alarm_count = total
            except Exception as exc:
                item["alarm_output"] = (
                    f"Alarm collection failed: {type(exc).__name__}: {exc}")
                self.log(f"[{node_name}] alarm evidence failed: "
                         f"{type(exc).__name__}: {exc}")
            with evidence_lock:
                evidence[node_name] = item

        # One worker per BB: traffic then alarm are sequential within a node,
        # while independent BB sessions are collected concurrently.
        self._run_per_node(by_node, _collect_node)

        # Retain one combined copy for persistence/backward compatibility.
        combined_traffic = "\n\n".join(
            f"--- {node} ---\n{evidence[node]['traffic_output']}"
            for node in run.node_names
            if node in evidence and evidence[node]["traffic_output"]
        )
        run.set_group(group, traffic_output=combined_traffic,
                      traffic_command=traffic_cmd)
        png = self._render_group_png(group, evidence)
        if not png:
            return
        wa = self.cfg["report"]["whatsapp"]
        if not wa.get("enabled", True):
            return

        counts = run.group_counts(group)
        alarms = sum(item.get("alarm_count", 0) for item in evidence.values())
        caption = wa["caption_template"].format(
            group=group, shortcode=run.shortcode or "",
            nodes=", ".join(run.node_names), ok=counts["enabled"],
            total=counts["total"], traffic_ok=counts["traffic_ok"], alarms=alarms)
        self.emit(CutoverEvent(kind="handoff", group=group, png_path=png,
                               caption=caption))

    # ── final verification ───────────────────────────────────────
    def _final_verify_worker(self) -> None:
        run = self.run
        fv = self.cfg.get("final_verification", {})
        if not fv.get("enabled", True) or not run.final_steps:
            self.log("No post-cutover verification steps are configured "
                     "(cutover.final_verification.steps is empty).")
            run.set_phase(RunPhase.DONE)
            self._finalize_manifest("COMPLETED_NO_FINAL_CHECKS")
            self.emit(CutoverEvent(kind="run_done",
                                   message="No verification configured."))
            return

        run.cancel_event.clear()
        run.set_phase(RunPhase.FINAL_VERIFY)
        self.log("── Post-cutover verification ──")

        for step in run.final_steps:
            if run.is_cancelled():
                step.status = "skipped"
                step.detail = "cancelled"
                run.touch()
                continue
            step.status = "running"
            run.touch()

            nodes = list(run.sessions.keys())
            if step.scope == "once":
                nodes = nodes[:1]

            outputs, failures = [], []
            for node_name in nodes:
                sess = run.sessions.get(node_name)
                if sess is None or sess.degraded:
                    continue
                try:
                    ok, out, detail = run_cutover_final_step(
                        sess.ssh, node_name, step, self.log, self.cfg)
                except Exception as exc:
                    ok, out, detail = False, "", f"{type(exc).__name__}: {exc}"
                outputs.append(f"--- {node_name} ---\n{out}")
                if not ok:
                    failures.append(f"{node_name}: {detail}")

            step.output = "\n\n".join(outputs)
            if failures:
                step.status = "fail"
                step.detail = "; ".join(failures)[:200]
                self.log(f"✗ {step.label}: {step.detail}")
            else:
                step.status = "pass"
                step.detail = "ok"
                self.log(f"✓ {step.label}")
            run.touch()

            if failures and fv.get("stop_on_failure"):
                self.log("Stopping verification (stop_on_failure).")
                break

        failed = sum(1 for s in run.final_steps if s.status == "fail")
        msg = (f"Verification finished — {len(run.final_steps) - failed} passed, "
               f"{failed} failed.")
        self.log(msg)
        run.set_phase(RunPhase.DONE)
        self._finalize_manifest(
            "COMPLETED" if failed == 0 else "COMPLETED_WITH_FAILURES"
        )
        self.emit(CutoverEvent(kind="run_done", message=msg))
