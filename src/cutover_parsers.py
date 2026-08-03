"""
Cut Over — output parsers.

Pure text in, structured data out. No SSH, no threads, no GUI, so every
function here is testable against captured node output.

The guiding rule throughout: **fail loudly rather than guess**. On a cut-over
a false green (a cell reported as carrying traffic when it isn't) is far worse
than a stall, so every parser that cannot find what it expects says so instead
of falling back to a plausible-looking number.

Band-number mappings are reused from :mod:`band_detector` — this module is a
row-producing sibling of ``detect_bands_from_hgetc``, which returns only the
*set* of bands present and is left untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from band_detector import (
    CELL_PREFIX_TO_BAND,
    LTE_FREQ_BAND_MAP,
    NR_BAND_LIST_MAP,
    _CELL_PREFIX_RE,
)
from cutover_model import CutoverCell, UNMAPPED

# ──────────────────────────────────────────────────────────────────
# Shared line handling
# ──────────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]|\x1b\(B")

#: ``EUtranCellFDD=SITE-1`` anywhere in a line. Also matches inside a comma
#: joined DN (``ManagedElement=1,ENodeBFunction=1,EUtranCellFDD=SITE-1``).
#:
#: The short forms matter: ``stzrc`` prints ``FDD=CCL09149_2A_1`` and
#: ``DU=CCSN003383_N002A_1``, not the full MO class names. Accepting only the
#: long forms meant the traffic parser matched nothing at all on real output.
#: Longest alternative first so ``EUtranCellFDD`` is not truncated to ``FDD``.
_MO_ASSIGN_RE = re.compile(
    r"\b(EUtranCellFDD|EUtranCellTDD|NRCellDU|NRCellCU|FDD|TDD|DU|CU)"
    r"\s*=\s*([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)

_MOS_FOUND_RE = re.compile(r"^\s*\d+\s+MOs?\s+(found|match)", re.IGNORECASE)
_TRAILING_INT_RE = re.compile(r"(-?\d+)\s*$")
_ARRAY_VALUE_RE = re.compile(r"=\s*(\d+)")

#: Canonical MO-type spelling, so ``eutrancellfdd`` from a lowercase command
#: echo still produces ``EUtranCellFDD`` in the commands we send back.
_MO_CANONICAL = {
    "eutrancellfdd": "EUtranCellFDD",
    "eutrancelltdd": "EUtranCellTDD",
    "nrcelldu": "NRCellDU",
    "nrcellcu": "NRCellCU",
    # stzrc abbreviations
    "fdd": "EUtranCellFDD",
    "tdd": "EUtranCellTDD",
    "du": "NRCellDU",
    "cu": "NRCellCU",
}


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _is_noise(line: str) -> bool:
    """True for headers, separators, totals, command echoes and prompts."""
    s = strip_ansi(line).strip()
    if not s:
        return True
    if len(s) >= 6 and set(s) <= set("=-_ "):
        return True
    if _MOS_FOUND_RE.match(s):
        return True
    if s.startswith("MO") or s.startswith("Proxy"):
        return True
    if "hgetc" in s or "hget " in s:
        return True
    if s.endswith(">"):                      # AMOS prompt line
        return True
    return False


def _canon_mo(raw: str) -> str:
    return _MO_CANONICAL.get(raw.lower(), raw)


def _split_prefix_and_id(cell_dn: str) -> tuple:
    """Return ``(prefix_letter, cell_id)`` from a DN like ``SITE...NF-1``."""
    prefix = ""
    m = _CELL_PREFIX_RE.search(cell_dn)
    if m and m.group(1).upper() in CELL_PREFIX_TO_BAND:
        prefix = m.group(1).upper()
    cell_id = cell_dn.rsplit("-", 1)[-1] if "-" in cell_dn else ""
    return prefix, cell_id


_SECTOR_LAST_DIGIT_RE = re.compile(r"(\d)\s*$")


def sector_of(cell_id: str, cell_dn: str = "", regex: str = "") -> str:
    """Sector token for a cell. Default rule (confirmed for this environment):
    the LAST digit of the trailing cell id — ``121`` → ``1``, ``232`` → ``2``,
    ``3`` → ``3``. A config ``discovery.sector_regex`` (named group ``sector``
    or group 1) applied to the full ``cell_dn`` overrides it, so a different
    naming convention needs no code change."""
    if regex:
        try:
            m = re.search(regex, cell_dn or "")
        except re.error:
            m = None
        if m:
            try:
                val = m.groupdict().get("sector")
            except Exception:
                val = None
            if val is None:
                val = m.group(1) if m.groups() else m.group(0)
            return (val or "").strip()
    m = _SECTOR_LAST_DIGIT_RE.search(cell_id or "")
    return m.group(1) if m else ""


def group_for_band(band_key: str, band_groups: dict) -> str:
    """Map a band key (``L1800``) onto a group (``MB``), else ``UNMAPPED``."""
    if not band_key:
        return UNMAPPED
    for group, keys in (band_groups or {}).items():
        if band_key in keys:
            return group
    return UNMAPPED


# ──────────────────────────────────────────────────────────────────
# 1. Cell discovery — hgetc band output
# ──────────────────────────────────────────────────────────────────
def parse_cells_from_hgetc(
    lte_output: str,
    nr_output: str,
    node_name: str,
    band_groups: Optional[dict] = None,
    mo_types: Optional[tuple] = None,
    nr_multiband_policy: str = "first",
    include_unmapped: bool = True,
    sector_regex: str = "",
) -> list:
    """Build one :class:`CutoverCell` per cell from the two ``hgetc`` outputs.

    ``lte_output`` comes from ``hgetc ^eutrancell[FT]DD= freqBand$`` and looks
    like::

        EUtranCellFDD=TCFGAMANKILAMTAGUMDDNF-1   ;3

    ``nr_output`` comes from ``hgetc nrcelldu bandListManual``. ``bandListManual``
    is an *array* attribute, so a dual-band cell spans several lines::

        NRCellDU=TCFGAMANKILAMTAGUMDDNN-401 ;i[1] = 41
                                            ;i[2] = 78

    The continuation line carries no MO name — it must be attributed to the
    cell above it, which is why the NR pass is stateful.

    A cell always belongs to exactly one group, because ``ldeb`` is issued once
    per MO; ``nr_multiband_policy`` (``first`` | ``lowest`` | ``highest``)
    decides which band wins, and the rest land in ``extra_band_numbers``.
    """
    band_groups = band_groups or {}
    allowed = tuple(m.lower() for m in (mo_types or (
        "EUtranCellFDD", "EUtranCellTDD", "NRCellDU")))
    cells: list = []
    seen: set = set()

    def _append(cell: CutoverCell) -> None:
        if cell.mo_type.lower() not in allowed:
            return
        if not include_unmapped and cell.group == UNMAPPED:
            return
        if cell.key in seen:
            return
        seen.add(cell.key)
        cells.append(cell)

    # ── LTE pass — one line per cell ─────────────────────────────
    for raw_line in (lte_output or "").splitlines():
        if _is_noise(raw_line):
            continue
        line = strip_ansi(raw_line).rstrip()
        m = _MO_ASSIGN_RE.search(line)
        if not m:
            continue

        mo_type = _canon_mo(m.group(1))
        cell_dn = m.group(2)

        # Value is after the ';' in the documented form. Some builds pad with
        # spaces instead, so fall back to the last whitespace-separated field.
        if ";" in line:
            value_part = line.split(";", 1)[1]
        else:
            parts = re.split(r"\s{2,}", line.strip())
            value_part = parts[-1] if len(parts) > 1 else ""

        # A trailing-int search beats int(value) — it survives ";3 (BAND3)".
        vm = _TRAILING_INT_RE.search(value_part.strip()) or \
            re.search(r"(\d+)", value_part)
        if not vm:
            continue
        band_number = int(vm.group(1))
        band_key = LTE_FREQ_BAND_MAP.get(band_number, f"L{band_number}?")

        prefix, cell_id = _split_prefix_and_id(cell_dn)
        # The MO attribute is authoritative; the cell-name prefix is only a
        # cross-check, recorded in raw_band_line when the two disagree.
        note = line.strip()
        if prefix and CELL_PREFIX_TO_BAND.get(prefix) not in (None, band_key):
            note += (f"   [prefix {prefix} suggests "
                     f"{CELL_PREFIX_TO_BAND[prefix]}, freqBand says {band_key}]")

        _append(CutoverCell(
            node_name=node_name,
            mo_type=mo_type,
            cell_dn=cell_dn,
            rat="LTE",
            prefix_letter=prefix,
            cell_id=cell_id,
            sector=sector_of(cell_id, cell_dn, sector_regex),
            band_number=band_number,
            band_key=band_key,
            group=group_for_band(band_key, band_groups),
            raw_band_line=note,
        ))

    # ── NR pass — stateful, array attribute spans lines ──────────
    current: Optional[dict] = None
    pending: list = []

    def _flush() -> None:
        nonlocal current, pending
        if current is None:
            return
        bands = [b for b in pending if b is not None]
        if bands:
            if nr_multiband_policy == "lowest":
                primary = min(bands)
            elif nr_multiband_policy == "highest":
                primary = max(bands)
            else:
                primary = bands[0]
            extra = [b for b in bands if b != primary]
        else:
            primary, extra = -1, []

        band_key = NR_BAND_LIST_MAP.get(primary, f"NR{primary}?" if primary >= 0 else "")
        prefix, cell_id = _split_prefix_and_id(current["cell_dn"])
        _append(CutoverCell(
            node_name=node_name,
            mo_type=current["mo_type"],
            cell_dn=current["cell_dn"],
            rat="NR",
            prefix_letter=prefix,
            cell_id=cell_id,
            sector=sector_of(cell_id, current["cell_dn"], sector_regex),
            band_number=primary,
            band_key=band_key,
            extra_band_numbers=extra,
            group=group_for_band(band_key, band_groups),
            raw_band_line=current["raw"],
        ))
        current, pending = None, []

    for raw_line in (nr_output or "").splitlines():
        if _is_noise(raw_line):
            continue
        line = strip_ansi(raw_line).rstrip()
        m = _MO_ASSIGN_RE.search(line)

        if m and m.group(1).upper().startswith("NRCELL"):
            _flush()
            current = {
                "mo_type": _canon_mo(m.group(1)),
                "cell_dn": m.group(2),
                "raw": line.strip(),
            }
            value_part = line.split(";", 1)[1] if ";" in line else ""
        else:
            if current is None:
                continue                      # orphan continuation — drop it
            value_part = line.split(";", 1)[1] if ";" in line else line
            current["raw"] += " | " + line.strip()

        found = [int(x) for x in _ARRAY_VALUE_RE.findall(value_part)]
        if not found:
            stripped = value_part.strip()
            if stripped.isdigit():
                found = [int(stripped)]
        pending.extend(found)

    _flush()
    return cells


# ──────────────────────────────────────────────────────────────────
# 2. `st cell` rows
# ──────────────────────────────────────────────────────────────────
@dataclass
class StCellRow:
    mo_type: str
    cell_dn: str
    admin_state: str
    op_state: str
    avail_status: str
    raw: str

    @property
    def mo_ref(self) -> str:
        return f"{self.mo_type}={self.cell_dn}"


# Both known layouts must parse. Real node output puts the MO *last* with
# parenthesized, numerically-prefixed states:
#     2966  1 (UNLOCKED)  1 (ENABLED)   ...,EUtranCellFDD=SITE-1
# The demo/sample format puts the MO *first* with bare states:
#     EUtranCellFDD=SITE-1    UNLOCKED ENABLED  null
# Longest alternative first; \b already stops LOCKED matching inside UNLOCKED.
_ADM_RE = re.compile(
    r"\b\d+\s*\(\s*(UNLOCKED|SHUTTING_DOWN|LOCKED)\s*\)|\b(UNLOCKED|SHUTTING_DOWN|LOCKED)\b"
)
_OP_RE = re.compile(
    r"\b\d+\s*\(\s*(ENABLED|DISABLED)\s*\)|\b(ENABLED|DISABLED)\b"
)
_AVAIL_RE = re.compile(
    r"\b(DEPENDENCY_LOCKED|NOT_INSTALLED|POWER_OFF|OFF_LINE|DEGRADED|FAILED|LOG_FULL|NO_STATUS|null)\b",
    re.IGNORECASE,
)

_ST_HEADER_TOKENS = ("admstate", "adm state", "opstate", "op. state",
                     "op state", "availstatus", "avail status")


def _first_group(match) -> str:
    if not match:
        return ""
    for g in match.groups():
        if g:
            return g.upper()
    return ""


def parse_st_cell_rows(
    output: str,
    mo_types: Optional[tuple] = None,
    row_regex: str = "",
) -> list:
    """Parse ``st cell`` (or ``st nrcelldu``) output into rows.

    Order-agnostic: the MO may appear first or last on the line, and the
    states may be bare (``ENABLED``) or parenthesized (``1 (ENABLED)``).

    ``row_regex`` overrides the built-in heuristic. It needs named groups
    ``mo``, ``adm`` and ``op`` (``avail`` optional). A regex that fails to
    match simply falls through to the heuristic rather than hard-failing —
    an operator typo should not blind the poller.
    """
    allowed = tuple(m.lower() for m in (mo_types or (
        "EUtranCellFDD", "EUtranCellTDD", "NRCellDU", "NRCellCU")))
    compiled = None
    if row_regex:
        try:
            compiled = re.compile(row_regex)
        except re.error:
            compiled = None

    rows: list = []
    for raw_line in (output or "").splitlines():
        line = strip_ansi(raw_line).rstrip()
        s = line.strip()
        if not s:
            continue
        if len(s) >= 6 and set(s) <= set("=- _"):
            continue
        if _MOS_FOUND_RE.match(s):
            continue
        low = s.lower()
        if any(tok in low for tok in _ST_HEADER_TOKENS):
            continue
        if s.endswith(">"):
            continue

        # Operator override first.
        if compiled is not None:
            m = compiled.search(line)
            if m:
                gd = m.groupdict()
                mo_raw = (gd.get("mo") or "").strip()
                mm = _MO_ASSIGN_RE.search(mo_raw) or _MO_ASSIGN_RE.search(line)
                if mm:
                    rows.append(StCellRow(
                        mo_type=_canon_mo(mm.group(1)),
                        cell_dn=mm.group(2),
                        admin_state=(gd.get("adm") or "").upper(),
                        op_state=(gd.get("op") or "").upper(),
                        avail_status=(gd.get("avail") or ""),
                        raw=s,
                    ))
                    continue

        # Heuristic: take the LAST MO assignment whose class we care about.
        # "Last" is what makes this work for comma-joined DNs and for the
        # MO-last layout at the same time.
        chosen = None
        for m in _MO_ASSIGN_RE.finditer(line):
            if m.group(1).lower() in allowed:
                chosen = m
        if chosen is None:
            continue

        rows.append(StCellRow(
            mo_type=_canon_mo(chosen.group(1)),
            cell_dn=chosen.group(2),
            admin_state=_first_group(_ADM_RE.search(line)),
            op_state=_first_group(_OP_RE.search(line)),
            avail_status=_first_group(_AVAIL_RE.search(line)),
            raw=s,
        ))

    return rows


def ue_for_cell(counts: dict, cell, mode: str = "suffix") -> Optional[int]:
    """Look up a cell's UE count tolerantly, mirroring :func:`match_row`.

    An exact ``mo_ref`` lookup is not enough. The traffic command and the
    discovery command do not have to print the DN in the same form — that is
    exactly why status matching has a suffix fallback — and a silent miss here
    would look identical to "this cell has no traffic", stalling the run for
    the whole timeout on a cell that is actually fine.

    As everywhere else, ambiguity resolves to ``None`` rather than a guess.
    """
    exact = counts.get(cell.mo_ref.upper())
    if exact is not None or mode == "exact":
        return exact

    def _dn(key: str) -> str:
        return key.split("=", 1)[-1]

    dn = cell.cell_dn.upper()
    hits = [v for k, v in counts.items() if _dn(k) == dn]
    if len(hits) == 1:
        return hits[0]
    if hits or mode == "dn":
        return None

    hits = [v for k, v in counts.items()
            if _dn(k).endswith(dn) or dn.endswith(_dn(k))]
    return hits[0] if len(hits) == 1 else None


def match_row(cells: list, node_name: str, row: StCellRow,
              mode: str = "suffix") -> Optional[CutoverCell]:
    """Find the cell a ``st cell`` row refers to, or ``None``.

    Ambiguity never resolves to a guess — on a cut-over, guessing means
    marking the wrong cell green.

    ``mode``:
      * ``exact``  — node + MO type + DN must all match.
      * ``dn``     — same node and DN, any MO class (FDD/TDD confusion).
      * ``suffix`` — additionally allow one DN to be a suffix of the other,
        for builds that print a shortened or fully-qualified variant.
    """
    want_key = f"{node_name}|{row.mo_ref}".upper()
    for c in cells:
        if c.key == want_key:
            return c
    if mode == "exact":
        return None

    dn = row.cell_dn.upper()
    hits = [c for c in cells if c.node_name == node_name and c.match_key == dn]
    if len(hits) == 1:
        return hits[0]
    if hits or mode == "dn":
        return None                     # ambiguous, or exhausted this mode

    hits = [
        c for c in cells
        if c.node_name == node_name
        and (c.match_key.endswith(dn) or dn.endswith(c.match_key))
    ]
    return hits[0] if len(hits) == 1 else None


# ──────────────────────────────────────────────────────────────────
# 3. `stzrc` — the composite status/traffic command
# ──────────────────────────────────────────────────────────────────
@dataclass
class StzrcRow:
    """One cell row from an ``stzrc`` LTECell / NRCell table."""

    table: str                       # "LTE" | "NR"
    mo_type: str
    cell_dn: str
    state: str = ""                  # raw S column: "1" = up, "L" = locked
    flags: str = ""                  # TABREMDF column, kept verbatim
    alm: str = ""                    # Alm column
    ue_count: Optional[int] = None
    band: str = ""                   # may be "5,26" for multi-band NR
    raw: str = ""

    @property
    def mo_ref(self) -> str:
        return f"{self.mo_type}={self.cell_dn}"

    @property
    def is_up(self) -> bool:
        return self.state.strip() == "1"

    @property
    def is_locked(self) -> bool:
        return self.state.strip().upper() == "L"


@dataclass
class StzrcResult:
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)   # "LTE"/"NR" -> (total, up)
    warning: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rows)


#: Header cell naming the table and the column that holds the cell name.
_STZRC_TABLES = {"LTECell": "LTE", "NRCell": "NR"}
_STZRC_TOTAL_RE = re.compile(r"Total:\s*(\d+)\s*Cells?\s*\((\d+)\s*up\)", re.IGNORECASE)


def _split_semis(line: str) -> list:
    return [p.strip() for p in strip_ansi(line).split(";")]


def parse_stzrc(output: str) -> StzrcResult:
    """Parse the LTECell / NRCell tables out of ``stzrc`` output.

    ``stzrc`` is a composite command: it loads the MO tree, collects alarms and
    prints several ``;``-delimited tables. Only the two cell tables matter here.
    They look like::

        Id ;LTECell            ;S ;TABREMDF ;Alm ; UEs ;cId ; ... ;Band ; ...
         1 ;FDD=CCL09149_2A_1  ;1 ;-------- ;  - ;  59 ; 22 ; ... ;   4 ; ...
        ------------------------------------------------------------------
        Total: 11 Cells (11 up)

    Columns are located **by header name**, not by position, since the LTE and
    NR tables have different column sets (NR adds ``subCarrS`` and an
    ``NRCellCU (State:UEs)`` tail).

    One trap worth naming: the NR table's ``subCarrS`` column reads ``15 (LB)``
    / ``30 (MB)``. That is subcarrier spacing, not this app's LB/MB band
    grouping — the two must never be conflated, which is why the band comes
    only from the column literally named ``Band``.
    """
    result = StzrcResult()
    lines = (output or "").splitlines()

    table = None          # "LTE" | "NR" while inside a cell table
    idx: dict = {}

    for raw_line in lines:
        line = strip_ansi(raw_line).rstrip()
        s = line.strip()
        if not s:
            continue

        # A header row names the table and gives us the column layout.
        if ";" in s:
            parts = _split_semis(line)
            found = None
            for i, name in enumerate(parts):
                if name in _STZRC_TABLES:
                    found = (i, _STZRC_TABLES[name])
                    break
            if found is not None:
                cell_col, table = found
                idx = {"cell": cell_col}
                for i, name in enumerate(parts):
                    key = name.strip().lower()
                    if key == "s" and "state" not in idx:
                        idx["state"] = i
                    elif key == "tabremdf":
                        idx["flags"] = i
                    elif key == "alm":
                        idx["alm"] = i
                    elif key in ("ues", "ue"):
                        idx["ue"] = i
                    elif key == "band":
                        idx["band"] = i
                continue

        if table is None:
            continue

        # End of the current table.
        m = _STZRC_TOTAL_RE.search(s)
        if m:
            result.totals[table] = (int(m.group(1)), int(m.group(2)))
            table = None
            idx = {}
            continue
        if set(s) <= set("=- ") and len(s) >= 6:
            continue

        parts = _split_semis(line)
        cell_col = idx.get("cell", 1)
        if cell_col >= len(parts):
            continue
        mm = _MO_ASSIGN_RE.search(parts[cell_col])
        if not mm:
            continue

        def _at(key: str) -> str:
            i = idx.get(key, -1)
            return parts[i] if 0 <= i < len(parts) else ""

        ue_raw = _at("ue")
        ue = int(ue_raw) if ue_raw.isdigit() else None

        result.rows.append(StzrcRow(
            table=table,
            mo_type=_canon_mo(mm.group(1)),
            cell_dn=mm.group(2),
            state=_at("state"),
            flags=_at("flags"),
            alm=_at("alm"),
            ue_count=ue,
            band=_at("band"),
            raw=s,
        ))

    if not result.rows:
        result.warning = ("No LTECell/NRCell table found in the output — this "
                          "does not look like stzrc output.")
    return result


def st_rows_from_stzrc(result: StzrcResult) -> list:
    """Adapt ``stzrc`` rows to :class:`StCellRow` so the enable poll can reuse
    the same folding logic regardless of which command produced the state.

    ``S`` is a compact state: ``1`` means unlocked and enabled, ``L`` means
    locked. Anything else is reported as unknown rather than guessed.
    """
    rows = []
    for r in result.rows:
        if r.is_up:
            adm, op = "UNLOCKED", "ENABLED"
        elif r.is_locked:
            adm, op = "LOCKED", "DISABLED"
        else:
            adm, op = "", ""
        rows.append(StCellRow(
            mo_type=r.mo_type, cell_dn=r.cell_dn,
            admin_state=adm, op_state=op, avail_status="", raw=r.raw,
        ))
    return rows


# ──────────────────────────────────────────────────────────────────
# 4. UE / traffic counts
# ──────────────────────────────────────────────────────────────────
@dataclass
class UeParseResult:
    counts: dict = field(default_factory=dict)   # mo_ref.upper() -> int
    strategy: str = "none"                       # stzrc|regex|column_span|token_index|none
    header_line: str = ""
    warning: str = ""
    #: Populated when the output was recognised as ``stzrc``, so the caller can
    #: reuse the same poll for cell state instead of running a second command.
    stzrc: Optional[StzrcResult] = None

    @property
    def ok(self) -> bool:
        return self.strategy != "none"


def _first_int(text: str) -> Optional[int]:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def _find_ue_header(lines: list, names: tuple):
    """Return ``(index, (start, end))`` of the UE column header, or ``None``.

    A header is the first non-data line containing one of *names* as a whole
    word. Data lines are excluded by requiring no MO assignment on the line.
    """
    for i, line in enumerate(lines):
        s = strip_ansi(line)
        if not s.strip():
            continue
        if _MO_ASSIGN_RE.search(s):
            continue                     # that's a data row, not a header
        for name in names:
            m = re.search(rf"\b{re.escape(name)}\b", s, re.IGNORECASE)
            if m:
                return i, m.span()
    return None


def parse_ue_counts(
    output: str,
    mo_types: Optional[tuple] = None,
    ue_column_names: tuple = ("UE", "UEs", "NoOfUsers", "nrOfRrcConnected",
                              "connectedUsers", "RrcConnected"),
    ue_regex: str = "",
) -> UeParseResult:
    """Extract a per-cell UE count from traffic-command output.

    Deliberately conservative. If the UE column cannot be located, this
    returns ``strategy="none"`` and an empty mapping rather than reaching for
    "the biggest integer on the line" — the caller then escalates to the
    operator instead of inventing traffic that may not exist.
    """
    allowed = tuple(m.lower() for m in (mo_types or (
        "EUtranCellFDD", "EUtranCellTDD", "NRCellDU", "NRCellCU")))
    lines = (output or "").splitlines()
    counts: dict = {}

    def _mo_ref_of(line: str) -> Optional[str]:
        chosen = None
        for m in _MO_ASSIGN_RE.finditer(line):
            if m.group(1).lower() in allowed:
                chosen = m
        if chosen is None:
            return None
        return f"{_canon_mo(chosen.group(1))}={chosen.group(2)}".upper()

    # ── Strategy 0: stzrc's own cell tables ──────────────────────
    # Tried first because it is the known-good real-node format, and it also
    # hands back cell state so the caller can skip a separate `st cell`.
    if not ue_regex:
        stz = parse_stzrc(output)
        if stz.ok:
            counts = {r.mo_ref.upper(): r.ue_count
                      for r in stz.rows if r.ue_count is not None}
            return UeParseResult(counts=counts, strategy="stzrc", stzrc=stz)

    # ── Strategy 1: operator-supplied regex ──────────────────────
    if ue_regex:
        try:
            rx = re.compile(ue_regex)
        except re.error as exc:
            return UeParseResult(strategy="none",
                                 warning=f"ue_regex is not valid: {exc}")
        for raw in lines:
            line = strip_ansi(raw)
            m = rx.search(line)
            if not m:
                continue
            gd = m.groupdict()
            ue_raw = gd.get("ue")
            if ue_raw is None or not str(ue_raw).strip().isdigit():
                continue
            ref = None
            if gd.get("mo"):
                mm = _MO_ASSIGN_RE.search(gd["mo"])
                if mm:
                    ref = f"{_canon_mo(mm.group(1))}={mm.group(2)}".upper()
            if ref is None:
                ref = _mo_ref_of(line)
            if ref:
                counts[ref] = int(ue_raw)
        if counts:
            return UeParseResult(counts=counts, strategy="regex")
        return UeParseResult(strategy="none",
                             warning="ue_regex matched no rows with a UE value.")

    # ── Strategy 2: column-span slicing ──────────────────────────
    found = _find_ue_header(lines, tuple(ue_column_names))
    if found is None:
        return UeParseResult(
            strategy="none",
            warning=("No UE column header found (looked for: "
                     f"{', '.join(ue_column_names)}). Set cutover.traffic."
                     "ue_column_names or ue_regex in config.json."),
        )

    hdr_i, (col_lo, col_hi) = found
    header_line = strip_ansi(lines[hdr_i]).rstrip()
    # Columns are right-aligned and values can overflow leftwards, so widen.
    lo, hi = max(0, col_lo - 4), col_hi + 8

    data_rows = 0
    for raw in lines[hdr_i + 1:]:
        line = strip_ansi(raw)
        ref = _mo_ref_of(line)
        if ref is None:
            continue
        data_rows += 1
        value = _first_int(line[lo:hi])
        if value is not None:
            counts[ref] = value

    if data_rows and len(counts) >= max(1, data_rows // 2):
        return UeParseResult(counts=counts, strategy="column_span",
                             header_line=header_line)

    # ── Strategy 3: token index ──────────────────────────────────
    header_tokens = re.split(r"\s+", header_line.strip())
    idx = -1
    for i, tok in enumerate(header_tokens):
        if any(tok.lower() == n.lower() for n in ue_column_names):
            idx = i
            break
    if idx >= 0:
        counts = {}
        for raw in lines[hdr_i + 1:]:
            line = strip_ansi(raw)
            ref = _mo_ref_of(line)
            if ref is None:
                continue
            tokens = re.split(r"\s+", line.strip())
            if idx < len(tokens) and tokens[idx].isdigit():
                counts[ref] = int(tokens[idx])
        if counts:
            return UeParseResult(counts=counts, strategy="token_index",
                                 header_line=header_line)

    return UeParseResult(
        strategy="none",
        header_line=header_line,
        warning=("Found a UE column header but could not read a number from "
                 "any data row. Set cutover.traffic.ue_regex in config.json."),
    )


# ──────────────────────────────────────────────────────────────────
# 4. Alarms
# ──────────────────────────────────────────────────────────────────
_ALARM_SEVERITIES = ("CRITICAL", "MAJOR", "MINOR", "WARNING", "INDETERMINATE")


def parse_alarm_summary(output: str,
                        no_alarm_patterns: tuple = ("No Active alarms",)) -> tuple:
    """Return ``(total, by_severity, has_no_alarms)`` from ``alt`` output."""
    text = strip_ansi(output or "")
    for pat in no_alarm_patterns:
        if pat.lower() in text.lower():
            return 0, {}, True

    by_severity: dict = {}
    for sev in _ALARM_SEVERITIES:
        n = len(re.findall(rf"\b{sev}\b", text, re.IGNORECASE))
        if n:
            by_severity[sev] = n

    m = re.search(r"(\d+)\s+alarms?\b", text, re.IGNORECASE)
    total = int(m.group(1)) if m else sum(by_severity.values())
    return total, by_severity, False


def diff_alarms(before: str, after: str) -> list:
    """Return alarm lines present in *after* but not in *before*.

    `alt` is captured before the first unlock so the evidence can distinguish
    alarms this cut over caused from ones the site already had. Comparison is
    on the alarm text with leading timestamps/ids stripped, since those differ
    between captures of the same underlying alarm.
    """
    def _keys(text: str) -> dict:
        out = {}
        for raw in strip_ansi(text or "").splitlines():
            s = raw.strip()
            if not s or set(s) <= set("=- "):
                continue
            if not re.search(r"\b(CRITICAL|MAJOR|MINOR|WARNING|INDETERMINATE)\b",
                             s, re.IGNORECASE):
                continue
            key = re.sub(r"^\s*\d+\s*;?\s*", "", s)
            key = re.sub(r"\b\d{6}-\d{2}:\d{2}:\d{2}\S*", "", key)
            out[re.sub(r"\s+", " ", key).strip()] = s
        return out

    before_keys = _keys(before)
    return [line for key, line in _keys(after).items() if key not in before_keys]


_BARRED_RE = re.compile(
    r"\b(cellBarred|cellReservedForOperatorUse)\b[^\S\n]*[=:;]?[^\S\n]*"
    r"([A-Za-z_0-9]+)", re.IGNORECASE)

#: Values that mean "a UE may camp here".
_NOT_BARRED_VALUES = {"NOT_BARRED", "NOTBARRED", "0", "FALSE", "NOT_RESERVED"}
_BARRED_VALUES = {"BARRED", "1", "TRUE", "RESERVED"}


def parse_barred_state(output: str) -> Optional[bool]:
    """Return True if barred, False if not barred, ``None`` if unknown.

    ``None`` matters: an absent attribute must not be read as "not barred",
    or we would wait out the whole traffic timeout on a cell that can never
    attract a UE and report nothing useful about why.
    """
    result = None
    for m in _BARRED_RE.finditer(strip_ansi(output or "")):
        value = m.group(2).strip().upper()
        if value in _BARRED_VALUES:
            return True
        if value in _NOT_BARRED_VALUES:
            result = False
    return result


def parse_radio_status(output: str) -> dict:
    """Summarise ``st B<band>`` (radio / Carrier) output.

    Returns ``{"total": n, "locked": n, "disabled": n, "rows": [...]}``. Used to
    explain a cell stuck at DEPENDENCY_LOCKED: if the band's radio is itself
    locked, unlocking the cell alone will never bring it up.
    """
    rows, locked, disabled = [], 0, 0
    for raw in strip_ansi(output or "").splitlines():
        s = raw.strip()
        if not s or set(s) <= set("=- "):
            continue
        low = s.lower()
        if "adm state" in low or "admstate" in low or s.startswith("Proxy"):
            continue
        adm = _first_group(_ADM_RE.search(s))
        op = _first_group(_OP_RE.search(s))
        if not adm and not op:
            continue
        rows.append({"raw": s, "admin_state": adm, "op_state": op})
        if adm == "LOCKED":
            locked += 1
        if op == "DISABLED":
            disabled += 1
    return {"total": len(rows), "locked": locked, "disabled": disabled, "rows": rows}


def band_prefix_for(band_key: str) -> str:
    """Invert ``CELL_PREFIX_TO_BAND`` — ``L1800`` -> ``F``.

    Supports the prefix-filtered status form the team already uses
    (``st cellfdd=.*F-``). Returns "" when the band has no known prefix.
    """
    for prefix, key in CELL_PREFIX_TO_BAND.items():
        if key == band_key:
            return prefix
    return ""


def looks_like_unknown_command(output: str, patterns: tuple) -> Optional[str]:
    """Return the matched pattern if the node rejected the command.

    Guards against an unconfirmed command spelling turning into ten minutes
    of polling something that does not exist.
    """
    text = strip_ansi(output or "")
    for pat in patterns or ():
        try:
            if re.search(pat, text, re.IGNORECASE):
                return pat
        except re.error:
            if pat.lower() in text.lower():
                return pat
    return None
