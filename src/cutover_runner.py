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
from datetime import datetime
from typing import Callable, Optional

from cutover_model import (
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
    looks_like_unknown_command,
    match_row,
    parse_alarm_summary,
    parse_barred_state,
    parse_cells_from_hgetc,
    parse_radio_status,
    parse_st_cell_rows,
    parse_stzrc,
    parse_ue_counts,
    st_rows_from_stzrc,
    strip_ansi,
    ue_for_cell,
)

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
        "enabled": True,
        "skip_already_in_service": True,
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
        "source": "stzrc",
        "commands": ["st cell"],
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
        "command_timeout_s": 120,
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

    for cell in cells:
        if cancel_event is not None and cancel_event.is_set():
            return any_ok, combined

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
        # Not stzrc-shaped after all — fall through to the st commands.
        log_cb(f"[{node_name}] {command!r} output had no cell table; "
               f"falling back to the status command(s).")

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
        log_cb(f"[{node_name}] could not read pre-state — every cell will be "
               f"treated as not-previously-unlocked.")
        return False, out

    mode = cfg["enable_poll"].get("match_mode", "suffix")
    already = 0
    for row in rows:
        cell = match_row(cells, node_name, row, mode=mode)
        if cell is None:
            continue
        cell.admin_state = row.admin_state
        cell.op_state = row.op_state
        if row.admin_state.upper() == "UNLOCKED":
            cell.was_unlocked_before = True
            if row.op_state.upper() == "ENABLED":
                cell.already_in_service = True
                already += 1

    if already:
        log_cb(f"[{node_name}] {already} cell(s) were already in service before "
               f"this run — they will not be unlocked, and rollback will not "
               f"touch them.")
    return True, out


def run_cutover_relock(ssh, node_name: str, cells: list,
                       log_cb: Callable[[str], None], cfg: dict,
                       wait_for_user=None, dry_run: bool = False,
                       cancel_event: Optional[threading.Event] = None,
                       on_cell=None) -> tuple:
    """Roll back: lock the cells **this session** unlocked. Returns (ok, output).

    The caller is responsible for passing only relockable cells; this function
    additionally refuses any cell that fails the check, because getting this
    wrong takes a live cell out of service.
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
        if not cell.is_relockable:
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

        node_names = [
            n for n in (
                str(self.form.get("node_name", "")).strip(),
                str(self.form.get("node2_name", "")).strip(),
            ) if n
        ]
        self.run = CutoverRun(
            shortcode=str(self.form.get("shortcode", "")).strip(),
            node_names=node_names,
            cfg=self.cfg,
        )
        for name in self.cfg["group_order"]:
            self.run.groups[name] = GroupState(name=name)
        self.run.groups[UNMAPPED] = GroupState(name=UNMAPPED)

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
        self._action_lock = threading.Lock()
        self._threads: list = []
        # Set when a manual traffic gate is waiting on the operator.
        self._traffic_gate: dict = {}

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

    def start_discovery(self) -> None:
        self._spawn(self._discovery_worker, "cutover-discovery")

    def unlock_group(self, group: str) -> None:
        self._spawn(lambda: self._grouped_action([group]), f"cutover-{group}")

    def unlock_all(self) -> None:
        self._spawn(lambda: self._grouped_action(list(self.cfg["group_order"])),
                    "cutover-all")

    def relock_group(self, group: str) -> None:
        """Roll back one group — only cells this session unlocked."""
        self._spawn(lambda: self._relock_action([group]), f"cutover-relock-{group}")

    def relock_all(self) -> None:
        """Roll back everything this session unlocked, highest band first."""
        order = list(reversed(list(self.cfg["group_order"])))
        self._spawn(lambda: self._relock_action(order), "cutover-relock-all")

    def run_final_verification(self) -> None:
        self._spawn(self._final_verify_worker, "cutover-verify")

    def confirm_traffic(self, group: str, ok: bool) -> None:
        """Resolve a manual traffic gate raised by an unparseable UE column."""
        gate = self._traffic_gate.get(group)
        if gate:
            gate["ok"] = ok
            gate["event"].set()

    def cancel(self) -> None:
        self.run.cancel_event.set()
        self.log("Cancel requested. Cells already unlocked stay unlocked — "
                 "cut over does not roll back.")
        for gate in self._traffic_gate.values():
            gate["ok"] = False
            gate["event"].set()
        self._force_disconnect()

    def shutdown(self) -> None:
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
            sess.connected = False
        self.run.sessions.clear()

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
            ssh = sess.ssh
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
        return sess

    def _discovery_worker(self) -> None:
        run = self.run
        run.cancel_event.clear()
        run.set_phase(RunPhase.DISCOVERING)
        self.log(f"Cut Over starting for {', '.join(run.node_names) or '(no nodes)'}")
        if self.cfg.get("dry_run"):
            self.log("DRY RUN is enabled — no unlock command will be sent.")

        all_cells: list = []
        for node_name in run.node_names:
            if run.is_cancelled():
                break
            sess = self._connect_node(node_name)
            if sess is None:
                continue
            run.sessions[node_name] = sess
            try:
                ok, _out, cells = run_cutover_discovery(
                    sess.ssh, node_name, self.log, self.cfg)
                if ok:
                    all_cells.extend(cells)
            except Exception as exc:
                self.log(f"[{node_name}] ✗ discovery failed: "
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

        # Pre-state, BEFORE anything is sent. This is what lets rollback be
        # safe later: cells already in service are not ours to unlock, and
        # above all not ours to re-lock.
        if self.cfg.get("prestate", {}).get("enabled", True):
            for node_name, sess in run.sessions.items():
                if run.is_cancelled():
                    break
                try:
                    run_cutover_prestate(sess.ssh, node_name, run.cells,
                                         self.log, self.cfg)
                except Exception as exc:
                    self.log(f"[{node_name}] pre-state check failed: "
                             f"{type(exc).__name__}: {exc}")
            with run.lock:
                for c in run.cells:
                    if c.already_in_service:
                        c.status = CellStatus.ALREADY_IN_SERVICE
                        c.status_detail = "already in service — not touched"
                run.touch()

        # Alarm baseline, so the evidence can distinguish alarms this cut over
        # caused from ones the site already had.
        if self.cfg["alarm"].get("baseline_before_unlock", True):
            for node_name, sess in run.sessions.items():
                if run.is_cancelled():
                    break
                try:
                    _ok, out, total = run_cutover_alarms(
                        sess.ssh, node_name, self.log, self.cfg)
                    run.alarm_baseline[node_name] = out
                    self.log(f"[{node_name}] alarm baseline: {total} active "
                             f"before cut over.")
                except Exception as exc:
                    self.log(f"[{node_name}] alarm baseline failed: "
                             f"{type(exc).__name__}: {exc}")

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

    # ── group orchestration ──────────────────────────────────────
    def _grouped_action(self, groups: list) -> None:
        run = self.run
        if run.phase in (RunPhase.IDLE, RunPhase.DISCOVERING):
            self.log("Discovery has not finished yet.")
            return
        run.cancel_event.clear()

        targets = [g for g in groups if run.unlockable_cells_of(g)]
        if not targets:
            self.log("No unlockable cells in the selected group(s).")
            return

        self._check_endc_anchor(targets)

        # One confirmation covering everything this click will do.
        if self.cfg.get("require_confirmation") and self._confirm_cb:
            lines = []
            for g in targets:
                for c in run.unlockable_cells_of(g):
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

        for group in targets:
            if run.is_cancelled():
                break
            self._run_group(group)
            grp = run.groups[group]
            if (grp.status in (GroupStatus.FAILED,)
                    and self.cfg.get("stop_on_group_failure")):
                self.log(f"Stopping after {group} failed (stop_on_group_failure).")
                break

        run.set_phase(RunPhase.CANCELLED if run.is_cancelled() else RunPhase.READY,
                      active_group="")

    def _check_endc_anchor(self, groups: list) -> None:
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

        nr_pending = [c for g in groups for c in run.unlockable_cells_of(g)
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

    def _relock_action(self, groups: list) -> None:
        run = self.run
        run.cancel_event.clear()

        targets = [(g, run.relockable_cells_of(g)) for g in groups]
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
                                     admin_state="LOCKED", ue_count=None)
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

    def _run_group(self, group: str) -> None:
        run = self.run
        grp = run.groups[group]
        cells = run.unlockable_cells_of(group)
        run.set_group(group, GroupStatus.RUNNING, started_at=time.monotonic(),
                      message="")
        run.set_phase(RunPhase.UNLOCKING, active_group=group)
        self.log(f"── {group}: unlocking {len(cells)} cell(s) ──")

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
        try:
            unlocked_any = self._unlock_phase(group, by_node)
            if not unlocked_any or run.is_cancelled():
                self._finish_group(group)
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

        targets = [c for c in run.unlockable_cells_of(group)
                   if c.status == CellStatus.UNLOCK_SENT]
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
            is_up = row.op_state.upper() in good_states
            if need_unlocked and row.admin_state:
                is_up = is_up and row.admin_state.upper() == "UNLOCKED"

            if is_up and cell.status in (CellStatus.WAITING_ENABLE,
                                         CellStatus.UNLOCK_SENT):
                run.set_cell(cell, CellStatus.ENABLED, status_detail="enabled",
                             t_enabled=time.monotonic(), **fields)
            else:
                run.set_cell(cell, **fields)
        return matched

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

    def _render_group_png(self, group: str) -> str:
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
        # Show the full alarm list, but call out what is NEW since the
        # baseline — a pre-existing alarm is not evidence about this cut over.
        parts = []
        for n, s in run.sessions.items():
            if not s.alarm_output:
                continue
            block = f"--- {n} ---\n{s.alarm_output}"
            baseline = run.alarm_baseline.get(n)
            if baseline:
                new = diff_alarms(baseline, s.alarm_output)
                block += ("\n--- NEW since cut over started ---\n"
                          + ("\n".join(new) if new
                             else "(none — no new alarms)"))
            parts.append(block)
        alarm_out = "\n\n".join(parts)

        pairs = []
        if grp.traffic_output:
            pairs.append((grp.traffic_command or self.cfg["traffic"]["command"],
                          grp.traffic_output))
        if alarm_out:
            pairs.append((alarm_cmd, alarm_out))
        if not pairs:
            return ""

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(self.log_dir, report["screenshot_subdir"])
        filename = report["filename_template"].format(
            shortcode=run.shortcode or "SITE", group=group, timestamp=ts)
        path = os.path.join(out_dir, filename)
        title = report["title_template"].format(
            shortcode=run.shortcode or "SITE", group=group,
            nodes=", ".join(run.node_names))

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
        self._collect_alarms(by_node)
        png = self._render_group_png(group)
        if not png:
            return
        wa = self.cfg["report"]["whatsapp"]
        if not wa.get("enabled", True):
            return

        counts = run.group_counts(group)
        alarms = sum(s.alarm_count or 0 for s in run.sessions.values())
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
        self.emit(CutoverEvent(kind="run_done", message=msg))
