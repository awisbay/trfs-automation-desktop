"""
Cut Over — data model.

Dataclasses and enums only: no I/O, no paramiko, no flet. Everything here is
safe to import from a unit test without a node, an SSH gateway or a GUI.

The concurrency contract this model exists to support:

  * Worker threads mutate :class:`CutoverRun` **only** under ``run.lock``, and
    only through :meth:`CutoverRun.set_cell` / :meth:`CutoverRun.touch`, which
    bump ``version`` and record which rows changed.
  * Worker threads never call ``page.update()`` and never touch a Flet control.
  * Exactly one asyncio flush loop in the GUI reads ``version`` / ``dirty_cells``
    and repaints.

All timestamps are ``time.monotonic()`` — a desktop that sleeps mid-cutover
makes wall-clock deltas meaningless.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


# ──────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────
class CellStatus(str, Enum):
    """Per-cell progress through the cut-over."""

    PENDING = "pending"                  # discovered, not touched
    SKIPPED = "skipped"                  # UNMAPPED band, or group not run
    ALREADY_IN_SERVICE = "already_in_service"  # was up before we started
    UNLOCK_SENT = "unlock_sent"          # ldeb issued, awaiting status
    UNLOCK_FAILED = "unlock_failed"      # ldeb output matched an error pattern
    WAITING_ENABLE = "waiting_enable"    # seen in status, still DISABLED
    BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"  # radio/parent still locked
    ENABLED = "enabled"                  # op state ENABLED (+ adm UNLOCKED)
    ENABLE_TIMEOUT = "enable_timeout"    # deadline hit, never came up
    BARRED = "barred"                    # up but barred — no UE can ever camp
    WAITING_TRAFFIC = "waiting_traffic"  # enabled, UE still below threshold
    TRAFFIC_OK = "traffic_ok"            # UE >= threshold        [terminal OK]
    TRAFFIC_TIMEOUT = "traffic_timeout"  # deadline hit, UE stayed 0
    TRAFFIC_UNKNOWN = "traffic_unknown"  # UE column unparseable -> manual gate
    RELOCKED = "relocked"                # rolled back by the operator
    CANCELLED = "cancelled"
    ERROR = "error"


#: Cells that finished the whole happy path.
TERMINAL_OK = frozenset({CellStatus.TRAFFIC_OK})

#: Cells that will not progress further without operator action.
TERMINAL_FAIL = frozenset({
    CellStatus.UNLOCK_FAILED,
    CellStatus.ENABLE_TIMEOUT,
    CellStatus.BLOCKED_BY_DEPENDENCY,
    CellStatus.BARRED,
    CellStatus.TRAFFIC_TIMEOUT,
    CellStatus.CANCELLED,
    CellStatus.ERROR,
})

#: Anything the state machine considers "done with this cell".
TERMINAL = TERMINAL_OK | TERMINAL_FAIL | frozenset({
    CellStatus.SKIPPED,
    CellStatus.ALREADY_IN_SERVICE,
    CellStatus.RELOCKED,
})

#: Maps a CellStatus onto the icon palette already used by the integration
#: page's ``_StepRow`` (pending / running / done / warn / error / skip), so the
#: GUI row widget stays a thin adaptation rather than a new invention.
STATUS_ICON_STATE = {
    CellStatus.PENDING: "pending",
    CellStatus.SKIPPED: "skip",
    CellStatus.ALREADY_IN_SERVICE: "skip",
    CellStatus.UNLOCK_SENT: "running",
    CellStatus.UNLOCK_FAILED: "error",
    CellStatus.WAITING_ENABLE: "running",
    CellStatus.BLOCKED_BY_DEPENDENCY: "error",
    CellStatus.ENABLED: "running",
    CellStatus.ENABLE_TIMEOUT: "error",
    CellStatus.BARRED: "error",
    CellStatus.WAITING_TRAFFIC: "running",
    CellStatus.TRAFFIC_OK: "done",
    CellStatus.TRAFFIC_TIMEOUT: "error",
    CellStatus.TRAFFIC_UNKNOWN: "warn",
    CellStatus.RELOCKED: "skip",
    CellStatus.CANCELLED: "skip",
    CellStatus.ERROR: "error",
}


class RunPhase(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    RECOVERING = "recovering"
    DISCOVERING = "discovering"
    READY = "ready"
    UNLOCKING = "unlocking"
    WAIT_ENABLE = "wait_enable"
    WAIT_TRAFFIC = "wait_traffic"
    REPORTING = "reporting"
    FINAL_VERIFY = "final_verify"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GroupStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DONE_WITH_FAILURES = "done_with_failures"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Group key used for cells whose band has no entry in ``band_groups``. These
#: are shown in the UI but are structurally unreachable from any Unlock button.
UNMAPPED = "UNMAPPED"

#: Group key for GSM (GeranCell) cells. GSM has its own unlock path — BSC
#: ``cmedit set … state=ACTIVE`` plus node ``ldeb GsmSector=…,Trx`` — and its
#: "enabled" is a combination of GeranCell state and GsmSector/Trx TS state, so
#: it is kept in its own group rather than mixed into the LB/MB/HB radio bands.
GSM = "GSM"


# ──────────────────────────────────────────────────────────────────
# Cells
# ──────────────────────────────────────────────────────────────────
@dataclass
class CutoverCell:
    """One cell on one node, and everything we know about it."""

    # ── identity (set at discovery, never rewritten) ──────────────
    node_name: str
    mo_type: str                 # EUtranCellFDD | EUtranCellTDD | NRCellDU
    cell_dn: str                 # RDN value, e.g. TCFGAMANKILAMTAGUMDDNF-1
    rat: str = "LTE"             # LTE | NR
    prefix_letter: str = ""      # "F" from ...DDNF-1 (cross-check only)
    cell_id: str = ""            # trailing id after the last '-'
    sector: str = ""             # sector token, e.g. "1" from cell_id "121"

    # ── band / grouping ──────────────────────────────────────────
    band_number: int = -1        # Ericsson band number (-1 = unparsed)
    band_key: str = ""           # L1800 / NR2600 / "L5?" when unmapped
    extra_band_numbers: list = field(default_factory=list)
    group: str = UNMAPPED        # LB | MB | HB | UNMAPPED
    raw_band_line: str = ""      # verbatim source line, for troubleshooting
    #: NR only — live ``nRSectorCarrierRef`` read from the NRCellDU. Kept as
    #: the complete MO reference, e.g. ``GNBDUFunction=1,NRSectorCarrier=N41_S1``.
    nr_sector_carrier_ref: str = ""

    # ── live node state (rewritten on every poll) ────────────────
    admin_state: str = ""        # UNLOCKED | LOCKED | SHUTTING_DOWN | ""
    op_state: str = ""           # ENABLED | DISABLED | ""
    avail_status: str = ""
    ue_count: Optional[int] = None   # None = not parsed yet / unparseable
    ue_peak: int = 0                 # max ever seen — latches traffic evidence
    alarm_count: Optional[int] = None
    flags: str = ""              # stzrc TABREMDF column, verbatim
    #: Per-RF-port VSWR from ``sdirc``, e.g.
    #: ``[{"port": "A", "vswr": 1.13, "rl": 24.5, "fru": "B0B28_RRU1"}, …]``.
    #: Empty until an sdirc capture attributes ports to this cell. NOT persisted
    #: — it is a live radio reading, re-derived on the next capture.
    vswr_ports: list = field(default_factory=list)
    #: GSM only — the full ``GsmSector`` RDN on the node this GeranCell maps to
    #: (e.g. ``CMPBAHIANMALAYBBUK-1``), captured at discovery. Empty for a
    #: GeranCell whose sector was not found on the node. Used to target
    #: ``ldeb GsmSector=…,Trx`` and to read ``tss`` for the combined state.
    gsm_sector_mo: str = ""
    #: GSM only — the full BSC GeranCell FDN (``SubNetwork=…,GeranCell=M8239R3``),
    #: captured at discovery. ``cmedit set`` needs the FDN, not a filter.
    gsm_fdn: str = ""
    #: GSM only — the BSC GeranCell state (``ACTIVE`` / ``HALTED`` / …).
    geran_state: str = ""

    # ── pre-state (captured before we send anything) ─────────────
    #: True if this cell was ALREADY unlocked when the run started. Such a cell
    #: is not ours: we neither unlock it nor — critically — re-lock it during a
    #: rollback, because it may be carrying live customers.
    was_unlocked_before: bool = False
    already_in_service: bool = False
    #: Set immediately before this run sends an unlock command. Persisted in
    #: the recovery checkpoint so a restarted app never blindly replays it.
    was_unlocked_by_run: bool = False

    # ── diagnosis ────────────────────────────────────────────────
    cell_barred: Optional[bool] = None   # None = unknown, never assumed False
    dependency_locked: bool = False
    traffic_samples: int = 0             # consecutive samples at/above threshold

    # ── phase status ─────────────────────────────────────────────
    status: CellStatus = CellStatus.PENDING
    status_detail: str = ""
    attempts: int = 0
    last_error: str = ""
    unlock_command: str = ""     # verbatim command sent (audit trail)
    unlock_output: str = ""      # tail of the ldeb output

    # ── timestamps (time.monotonic) ──────────────────────────────
    t_unlock_sent: float = 0.0
    t_enabled: float = 0.0
    t_traffic_ok: float = 0.0
    t_last_seen: float = 0.0     # last st-cell row that matched this cell

    # ── derived ──────────────────────────────────────────────────
    @property
    def mo_ref(self) -> str:
        """What goes into a command: ``EUtranCellFDD=SITE-1``."""
        return f"{self.mo_type}={self.cell_dn}"

    @property
    def key(self) -> str:
        """Stable dict key, unique across nodes."""
        return f"{self.node_name}|{self.mo_ref}".upper()

    @property
    def match_key(self) -> str:
        """Upper-cased DN, used for tolerant ``st cell`` row matching."""
        return self.cell_dn.upper()

    @property
    def is_unlockable(self) -> bool:
        """A cell that still needs unlocking: not UNMAPPED, not already in
        service, and not currently up. A previous attempt does not permanently
        suppress Unlock: if live polling later confirms LOCKED, the operator
        must be able to retry it."""
        return (
            self.group != UNMAPPED
            and not self.already_in_service
            and not self.is_lockable
        )

    @property
    def is_relockable(self) -> bool:
        """True only for cells **this session** actually unlocked.

        This is the whole safety property of rollback: a cell that was already
        unlocked when the run started must never be re-locked, or we would take
        a live cell carrying customers out of service.
        """
        return (
            self.was_unlocked_by_run
            and not self.was_unlocked_before
            and not self.already_in_service
            and (self.admin_state or "").upper() != "LOCKED"
            and self.status not in (CellStatus.SKIPPED, CellStatus.RELOCKED)
        )

    @property
    def vswr_worst(self) -> Optional[float]:
        """The highest (worst) VSWR across this cell's RF ports, or None when
        no numeric VSWR is available (e.g. an AAS/AIR radio reports ``-``)."""
        vals = [p.get("vswr") for p in self.vswr_ports
                if isinstance(p.get("vswr"), (int, float))]
        return max(vals) if vals else None

    @property
    def is_live_enabled(self) -> bool:
        """Whether the latest live read says this LTE/NR cell is in service."""
        if self.rat == "GSM":
            return (self.geran_state or "").upper() == "ACTIVE"
        return ((self.admin_state or "").upper() == "UNLOCKED"
                and (self.op_state or "").upper() == "ENABLED")

    @property
    def is_carrying_traffic(self) -> bool:
        """Current (not historical peak) UE evidence on an enabled LTE/NR cell."""
        return self.rat != "GSM" and self.is_live_enabled and (self.ue_count or 0) > 0

    @property
    def is_lockable(self) -> bool:
        """True when the cell is currently up (unlocked / enabled) and could be
        locked now — regardless of whether *this* run unlocked it. This drives
        the Unlock↔Lock button toggle: a group that is already fully unlocked
        offers Lock instead of a dead Unlock button. (Rollback safety —
        :attr:`is_relockable` — is separate and unchanged: it only ever covers
        cells this run unlocked.)"""
        if self.group == UNMAPPED:
            return False
        if self.rat == "GSM":
            # A live GeranCell read is authoritative.  In particular, do not
            # leave the Lock button enabled from a stale ENABLED status after a
            # later poll has confirmed HALTED.
            geran = (self.geran_state or "").upper()
            if geran:
                return geran == "ACTIVE"
            return (self.status == CellStatus.ENABLED
                    and self.status != CellStatus.RELOCKED)
        if self.status in (CellStatus.RELOCKED, CellStatus.SKIPPED):
            return False
        admin = (self.admin_state or "").upper()
        # A fresh live LOCKED read is authoritative. Ownership records that an
        # earlier write was attempted; it does not mean the cell is still up.
        if admin == "LOCKED":
            return False
        if admin == "UNLOCKED":
            return True
        # A cell this run unlocked can always be locked back, even if it never
        # reached ENABLED (unlocked but DISABLED / enable-timeout).
        if self.was_unlocked_by_run:
            return True
        return self.status in (CellStatus.ENABLED, CellStatus.WAITING_TRAFFIC,
                               CellStatus.TRAFFIC_OK)

    def short_label(self) -> str:
        return f"{self.mo_ref} [{self.band_key or '?'}]"


# ──────────────────────────────────────────────────────────────────
# Per-node SSH session
# ──────────────────────────────────────────────────────────────────
@dataclass
class NodeSession:
    """One live AMOS session, with the lock that keeps it single-writer.

    The paramiko shell channel is a single stateful PTY. Two threads writing
    to it interleaves output and corrupts *both* results, so every command
    for a node goes through :meth:`run`.
    """

    node_name: str
    ssh: object = None                     # IntegrationSSH (not imported here)
    #: Second AMOS session for fast, read-only STATUS polling (hgetc admin/op),
    #: so the status monitor never contends with unlock on the single-writer
    #: primary PTY. ``None`` → fall back to ``ssh`` under the action lock.
    read_ssh: object = None
    #: Third AMOS session for the SLOW background reads (stzrc traffic + sdirc
    #: VSWR), so they run in parallel without ever holding up the fast status
    #: poll. Opened lazily; ``None`` (or ``bg_failed``) → share ``read_ssh``
    #: under ``read_lock``.
    bg_ssh: object = None
    bg_failed: bool = False
    #: Serialises use of ``read_ssh`` between the status monitor and any
    #: background reader that had to fall back to it.
    read_lock: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_amos: bool = False
    connected: bool = False
    last_error: str = ""
    consecutive_failures: int = 0
    degraded: bool = False                 # too many failures — stop polling it
    alarm_count: Optional[int] = None      # `alt` is node-scoped, not per cell
    alarm_output: str = ""

    def run(self, command: str, timeout: int = 120) -> str:
        """Run one AMOS command. All node traffic is serialized through here."""
        if self.ssh is None:
            raise RuntimeError(f"No SSH session for {self.node_name}")
        with self.lock:
            return self.ssh.run_amos_command_safe(
                command, self.node_name, timeout=timeout
            )


# ──────────────────────────────────────────────────────────────────
# Groups and final verification
# ──────────────────────────────────────────────────────────────────
@dataclass
class GroupState:
    name: str
    status: GroupStatus = GroupStatus.PENDING
    cell_keys: list = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    enable_deadline: float = 0.0
    traffic_deadline: float = 0.0
    poll_count: int = 0
    traffic_output: str = ""
    traffic_command: str = ""
    alarm_output: str = ""
    alarm_command: str = ""
    screenshot_path: str = ""
    message: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            GroupStatus.DONE,
            GroupStatus.DONE_WITH_FAILURES,
            GroupStatus.FAILED,
            GroupStatus.CANCELLED,
        )


@dataclass
class FinalStepState:
    key: str
    label: str
    command: str
    scope: str = "per_node"      # per_node | once
    timeout_s: int = 120
    expect_regex: str = ""
    fail_regex: str = ""
    screenshot: bool = False
    status: str = "pending"      # pending | running | pass | fail | warn | skipped
    output: str = ""
    detail: str = ""
    screenshot_path: str = ""


# ──────────────────────────────────────────────────────────────────
# Whole-run state
# ──────────────────────────────────────────────────────────────────
@dataclass
class CutoverEvent:
    """Something the engine needs the GUI to do (or show)."""

    kind: str                    # handoff | confirm_traffic | diagnostic
                                 # | group_done | run_done | discovery_done
    group: str = ""
    png_path: str = ""
    caption: str = ""
    message: str = ""


@dataclass
class CutoverRun:
    """The single mutable object shared between worker threads and the GUI."""

    shortcode: str = ""
    node_names: list = field(default_factory=list)
    cfg: dict = field(default_factory=dict)

    cells: list = field(default_factory=list)          # list[CutoverCell]
    by_key: dict = field(default_factory=dict)         # key -> CutoverCell
    groups: dict = field(default_factory=dict)         # name -> GroupState
    sessions: dict = field(default_factory=dict)       # node -> NodeSession
    final_steps: list = field(default_factory=list)    # list[FinalStepState]

    phase: RunPhase = RunPhase.IDLE
    active_group: str = ""
    error: str = ""
    #: node -> raw `alt` output captured before the first unlock, so the
    #: evidence can show alarms this cut over actually caused.
    alarm_baseline: dict = field(default_factory=dict)
    #: Paths only; raw outputs stay in separate files so the structured
    #: checkpoint and final manifest remain compact.
    artifacts: dict = field(default_factory=dict)

    # ── concurrency ──────────────────────────────────────────────
    lock: threading.RLock = field(default_factory=threading.RLock)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    version: int = 0
    dirty_cells: set = field(default_factory=set)
    dirty_meta: bool = False
    on_change: Optional[Callable[[], None]] = field(
        default=None, repr=False, compare=False,
    )

    # ── mutation helpers — always call under self.lock ────────────
    def touch(self, cell: Optional[CutoverCell] = None) -> None:
        """Record that something changed, so the flush loop repaints it."""
        self.version += 1
        if cell is not None:
            self.dirty_cells.add(cell.key)
        else:
            self.dirty_meta = True
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def set_cell(self, cell: CutoverCell, status: Optional[CellStatus] = None,
                 **fields) -> None:
        """Atomically update a cell and mark its row dirty."""
        with self.lock:
            if status is not None:
                cell.status = status
            for name, value in fields.items():
                setattr(cell, name, value)
            self.touch(cell)

    def set_phase(self, phase: RunPhase, active_group: str = None) -> None:
        with self.lock:
            self.phase = phase
            if active_group is not None:
                self.active_group = active_group
            self.touch()

    def set_group(self, name: str, status: Optional[GroupStatus] = None,
                  **fields) -> None:
        with self.lock:
            grp = self.groups.get(name)
            if grp is None:
                return
            if status is not None:
                grp.status = status
            for fname, value in fields.items():
                setattr(grp, fname, value)
            self.touch()

    # ── queries ──────────────────────────────────────────────────
    def cells_of(self, group: str, sector: Optional[str] = None) -> list:
        grp = self.groups.get(group)
        if not grp:
            return []
        cells = [self.by_key[k] for k in grp.cell_keys if k in self.by_key]
        if sector:
            cells = [c for c in cells if c.sector == sector]
        return cells

    def unlockable_cells_of(self, group: str,
                            sector: Optional[str] = None) -> list:
        return [c for c in self.cells_of(group, sector) if c.is_unlockable]

    def relockable_cells_of(self, group: str,
                            sector: Optional[str] = None) -> list:
        """Only cells this session unlocked — see :attr:`CutoverCell.is_relockable`."""
        return [c for c in self.cells_of(group, sector) if c.is_relockable]

    def lockable_cells_of(self, group: str,
                          sector: Optional[str] = None) -> list:
        """Cells currently up that could be locked now — see
        :attr:`CutoverCell.is_lockable`. Drives the Unlock↔Lock button toggle."""
        return [c for c in self.cells_of(group, sector) if c.is_lockable]

    def sectors_of(self, group: str) -> list:
        """Sorted, de-duplicated sector tokens present in a group's cells —
        drives the per-(band group × sector) unlock buttons. Only sectors that
        actually have a cell appear, so the UI auto-adapts to each site."""
        seen = {c.sector for c in self.cells_of(group) if c.sector}
        return sorted(seen, key=lambda s: (len(s), s))

    def group_counts(self, group: str) -> dict:
        """Counts used by the group header: total / enabled / traffic / failed."""
        cells = self.cells_of(group)
        enabled = sum(
            1 for c in cells
            if c.status in (CellStatus.ENABLED, CellStatus.WAITING_TRAFFIC,
                            CellStatus.TRAFFIC_OK, CellStatus.TRAFFIC_UNKNOWN,
                            CellStatus.TRAFFIC_TIMEOUT)
        )
        return {
            "total": len(cells),
            "enabled": enabled,
            "traffic_ok": sum(1 for c in cells if c.is_carrying_traffic),
            "failed": sum(1 for c in cells if c.status in TERMINAL_FAIL),
        }

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()
