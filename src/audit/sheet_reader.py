"""
sheet_reader.py - resilient, self-reporting CDD/LLD sheet access.

Every audit reader has to turn a human-maintained Excel sheet into
(column → index) + data rows. Those sheets drift between revisions in a
handful of predictable ways, and each one used to silently drop a whole
category (0 rows, no error) or crash on a ``None`` index. This module
centralizes the defenses so every reader inherits them:

  Layer 1 - Sheet lookup is tolerant: exact name first, then a
            whitespace/case-insensitive match ("ESS " == "ess").
  Layer 2 - Header row is DETECTED, not assumed: a leading blank/banner
            row (e.g. the "COREDEF" row) shifts the real header down a
            line; we scan a window of rows and pick the one that actually
            contains the expected columns, rather than trusting a
            hardcoded row number.
  Layer 3 - Column names are matched after NORMALIZATION (nbsp → space,
            collapsed whitespace, trailing dots/colons stripped, lower-
            cased) and via ALIASES, so "NRSectorCarrier.essScLocalId" and
            "SectorCarrier.essScLocalId" resolve to the same field.
            Duplicate headers (CellName appears twice) are addressable by
            position.
  Layer 4 - Cell access is GUARDED: a missing column or a short row never
            indexes with ``None`` / out of range - it yields "".
  Layer 5 - DIAGNOSTICS: the chosen header row and any missing required
            columns are logged, so a silent miss becomes a visible line
            ("[audit/ess] missing columns: gnb id") instead of an empty
            result nobody notices.

Readers that already embed this behaviour (``cdd_reader._get_sheet``) need
not change; new/fragile readers should go through :func:`open_sheet`.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple, Union

# A "required column" is an alias group: any one of the spellings satisfies it.
AliasGroup = Union[str, Sequence[str]]


def hnorm(s) -> str:
    """Canonical header key: nbsp→space, collapse whitespace, strip trailing
    separators, lowercase. ``"NRSectorCarrier.essScLocalId\\xa0"`` → the same
    key as ``"nrsectorcarrier.esssclocalid"``."""
    if s is None:
        return ""
    t = str(s).replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(" .:;·-").strip()
    return t.lower()


def _aliases(group: AliasGroup) -> List[str]:
    if isinstance(group, str):
        group = [group]
    return [hnorm(a) for a in group if hnorm(a)]


def find_sheet(wb, name: str) -> Optional[str]:
    """Actual sheet name matching ``name`` - exact, else case/space-insensitive."""
    if name in wb.sheetnames:
        return name
    want = hnorm(name)
    for sn in wb.sheetnames:
        if hnorm(sn) == want:
            return sn
    return None


class SheetView:
    """A resolved sheet: chosen header row, normalized column index, data rows."""

    def __init__(self, sheet: str, header_row: int,
                 header_cells: Sequence, data_rows: List[tuple]):
        self.sheet = sheet
        self.header_row = header_row
        self.rows = data_rows
        # name(normalized) → first index; plus every index per name for dups.
        self._idx: Dict[str, int] = {}
        self._all: Dict[str, List[int]] = {}
        for i, h in enumerate(header_cells):
            key = hnorm(h)
            if not key:
                continue
            self._all.setdefault(key, []).append(i)
            if key not in self._idx:
                self._idx[key] = i

    def col(self, *aliases: str) -> Optional[int]:
        """First column index matching any alias (normalized), else None."""
        for a in aliases:
            i = self._idx.get(hnorm(a))
            if i is not None:
                return i
        return None

    def cols(self, *aliases: str) -> List[int]:
        """All indices matching any alias, in column order (for duplicates like
        the LTE/NR ``CellName`` pair)."""
        out: List[int] = []
        for a in aliases:
            out += self._all.get(hnorm(a), [])
        return sorted(set(out))

    def get(self, row: tuple, alias_or_idx, default: str = "") -> str:
        """Guarded cell read: accepts a column index or an alias name; a missing
        column or short row yields ``default`` (never an exception)."""
        i = alias_or_idx if isinstance(alias_or_idx, int) else self.col(alias_or_idx)
        if i is None or i < 0 or i >= len(row):
            return default
        v = row[i]
        return "" if v is None else str(v).strip()

    def missing(self, required: Sequence[AliasGroup]) -> List[str]:
        """Alias groups with no matching column, labelled by their first name."""
        miss = []
        for g in required:
            al = _aliases(g)
            if not any(a in self._idx for a in al):
                miss.append(al[0] if al else "?")
        return miss


def _score(header_cells: Sequence, required: Sequence[AliasGroup]) -> int:
    keys = {hnorm(h) for h in header_cells if hnorm(h)}
    return sum(1 for g in required if any(a in keys for a in _aliases(g)))


def open_sheet(wb, sheet: str, required: Sequence[AliasGroup],
               header_hint: int = 1, window: int = 25,
               log=lambda m: None, tag: str = "sheet") -> Optional[SheetView]:
    """Open ``sheet`` and return a :class:`SheetView`, or ``None`` (logged) if the
    sheet is absent or no plausible header row can be found.

    ``required`` drives BOTH header detection (the row satisfying the most
    groups wins) and the missing-column diagnostic. ``header_hint`` only breaks
    ties, so a wrong hint self-corrects instead of silently failing."""
    actual = find_sheet(wb, sheet)
    if actual is None:
        log(f"[audit/{tag}] sheet '{sheet}' not found - skipped.")
        return None
    ws = wb[actual]
    scan = list(ws.iter_rows(min_row=1, max_row=window, values_only=True))
    if not scan:
        log(f"[audit/{tag}] sheet '{sheet}' is empty - skipped.")
        return None
    # Pick the header row: highest number of required groups present; ties go to
    # the configured hint, then to the earliest row.
    best_i, best_score = 0, -1
    for i, cells in enumerate(scan):
        sc = _score(cells, required)
        hint_bonus = 0.5 if (i + 1) == header_hint else 0.0
        if sc + hint_bonus > best_score:
            best_score, best_i = sc + hint_bonus, i
    if best_score <= 0:
        log(f"[audit/{tag}] sheet '{sheet}': no header row with the expected "
            f"columns in the first {window} rows - skipped.")
        return None
    header_row = best_i + 1
    header_cells = scan[best_i]
    data = list(ws.iter_rows(min_row=header_row + 1, values_only=True))
    view = SheetView(actual, header_row, header_cells, data)
    miss = view.missing(required)
    if miss:
        log(f"[audit/{tag}] sheet '{sheet}': header at row {header_row}, "
            f"but missing columns: {', '.join(miss)} - those checks are skipped.")
    else:
        log(f"[audit/{tag}] sheet '{sheet}': header at row {header_row}, "
            f"{len(data)} data rows.")
    return view
