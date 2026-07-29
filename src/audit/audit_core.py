"""
audit_core.py — compare CDD expected values against parsed node dump records
and write an Excel report.

``normalize`` + the Match/Mismatch/NotFound verdict are ported from
enp-generator ``services/audit_engine.py`` / ``services/node_audit.py``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# ── Value normalization (from enp-generator audit_engine.normalize) ──
def normalize(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(f)
    except (ValueError, TypeError):
        return s.lower()


def normalize_segments(v) -> str:
    """Normalize a hyphen-separated value (e.g. CGI ``MCC-MNC-LAC-CI``) by
    stripping leading zeros from each numeric segment, so ``515-02-00087-60031``
    equals ``515-02-87-60031``. Non-numeric segments fall back to ``normalize``.
    """
    if v is None:
        return ""
    parts = str(v).strip().split("-")
    out = []
    for p in parts:
        p = p.strip()
        out.append(str(int(p)) if p.isdigit() else normalize(p))
    return "-".join(out)


# name -> comparator; selected per-column via the CDD map's optional "norm".
_NORMALIZERS = {"segments": normalize_segments}


@dataclass
class AuditItem:
    """One expected (MO, parameter) coming from the CDD."""
    category: str          # node | cell | relation
    tech: str              # lte_nr | gsm
    mo_local: str          # FDN below ManagedElement, e.g.
                           #   ENodeBFunction=1,EUtranCellFDD=BOALANG-L1
    parameter: str
    expected: str
    key: str               # human key (cell / node name) for the report
    source: str            # "<sheet>!<column>" for traceability
    norm: str = ""         # optional comparator hint (e.g. "segments" for CGI)
    via_ref: str = ""      # follow this ref attr on the MO to the target MO
                           #   (e.g. "sectorCarrierRef") before reading parameter
    attr_format: str = ""  # build actual from >1 attrs, e.g.
                           #   "{noOfTxAntennas}T{noOfRxAntennas}R" → "4T4R"
    node: str = ""         # node this row belongs to (eNodeBName/gNBName…)


@dataclass
class AuditResult:
    category: str
    key: str
    mo: str
    parameter: str
    expected: str
    actual: str
    status: str            # Match | Mismatch | NotFound | MO_NotFound
    source: str
    node: str = ""
    ref_cell: str = ""     # the cell a via_ref param belongs to (MO points to
                           #   the real MO, e.g. SectorCarrier)


def _index_records(records: Dict[str, Dict[str, str]]):
    """Build matching indexes for CDD ``mo_local`` → dump/cmedit records.

    Returns ``(g_exact, g_leaf, by_node)`` where:
      * g_exact — global: FDN below ManagedElement → attrs.
      * g_leaf  — global: last MO segment ``Class=id`` → attrs (BSC GSM,
                  node-name-independent).
      * by_node — per-node {node: {"exact": {...}, "leaf": {...}}}, so that
                  when several nodes' dumps are merged, a node MO like
                  ``ENodeBFunction=1`` doesn't collide across nodes.
    All keys lower-cased."""
    g_exact: Dict[str, Dict[str, str]] = {}
    g_leaf: Dict[str, Dict[str, str]] = {}
    by_node: Dict[str, Dict[str, Dict[str, Dict[str, str]]]] = {}
    for ldn, attrs in records.items():
        local = re.sub(r"^.*?ManagedElement=[^,]+,?", "", ldn).lower()
        last = ldn.split(",")[-1].strip().lower()
        g_exact[local] = attrs
        if "=" in last:
            g_leaf[last] = attrs
        m = re.search(r"ManagedElement=([^,]+)", ldn)
        if m:
            nd = by_node.setdefault(m.group(1).lower(),
                                    {"exact": {}, "leaf": {}})
            nd["exact"][local] = attrs
            if "=" in last:
                nd["leaf"][last] = attrs
    return g_exact, g_leaf, by_node


def _find_mo(mo: str, node: str, g_exact, g_leaf, by_node):
    """Look up an MO's attrs, preferring the owning node's index (avoids
    cross-node collisions) and falling back to the global index (GSM/BSC)."""
    mo_l = mo.lower()
    single = "," not in mo
    nd = by_node.get((node or "").lower())
    if nd is not None:
        hit = nd["exact"].get(mo_l)
        if hit is None and single:
            hit = nd["leaf"].get(mo_l)
        if hit is not None:
            return hit
    hit = g_exact.get(mo_l)
    if hit is None and single:
        hit = g_leaf.get(mo_l)
    return hit


_CELL_RE = re.compile(
    r"(?:EUtranCellFDD|EUtranCellTDD|NRCellDU|NRCellCU|GeranCell)=([^,]+)")


def _cell_of(mo_local: str) -> str:
    """Extract the cell identity from an MO FDN, for the Reference Cell column."""
    m = _CELL_RE.search(mo_local)
    return m.group(1) if m else ""


def _resolve_ref(attrs: Dict[str, str], ref_attr: str, g_exact, g_leaf, by_node):
    """Follow a reference attribute (e.g. ``sectorCarrierRef`` on EUtranCellFDD,
    whose value is a full FDN to a SectorCarrier MO) to the referenced MO. The
    ref FDN carries its own ManagedElement, so we resolve within that node to
    stay collision-free. Returns ``(below_ME_fdn, target_attrs)`` — the FDN so
    the report/script can point at the REAL MO, not the referencing cell."""
    low = {k.lower(): v for k, v in attrs.items()}
    ref = attrs.get(ref_attr) or low.get(ref_attr.lower())
    if not ref:
        return "", None
    ref = str(ref)
    node_m = re.search(r"ManagedElement=([^,]+)", ref)
    below = re.sub(r"^.*?ManagedElement=[^,]+,", "", ref).strip()
    node = node_m.group(1) if node_m else ""
    return below, _find_mo(below.lower(), node, g_exact, g_leaf, by_node)


def compare(items: List[AuditItem],
            records: Dict[str, Dict[str, str]]) -> List[AuditResult]:
    """Compare each CDD AuditItem against the node dump / cmedit records."""
    g_exact, g_leaf, by_node = _index_records(records)
    results: List[AuditResult] = []
    for it in items:
        ref_cell = _cell_of(it.mo_local)
        eff_mo = it.mo_local          # the MO the report/script should target
        attrs = _find_mo(it.mo_local, it.node, g_exact, g_leaf, by_node)
        if attrs is None:
            results.append(AuditResult(
                it.category, it.key, it.mo_local, it.parameter,
                it.expected, "", "MO_NotFound", it.source, it.node, ref_cell))
            continue
        # Follow a reference (e.g. sectorCarrierRef → SectorCarrier MO) when the
        # audited attribute lives on the referenced MO. The MO reported becomes
        # the REAL MO (SectorCarrier), and the cell moves to the ref_cell column.
        lookup = attrs
        if it.via_ref:
            rmo, target = _resolve_ref(attrs, it.via_ref, g_exact, g_leaf, by_node)
            if target is None:
                results.append(AuditResult(
                    it.category, it.key, it.mo_local, it.parameter,
                    it.expected, "", "MO_NotFound", it.source, it.node, ref_cell))
                continue
            lookup = target
            eff_mo = rmo or it.mo_local
        low = {k.lower(): v for k, v in lookup.items()}
        # Composite value (display-only): rebuild the actual into the CDD's own
        # format, e.g. "{noOfTxAntennas}T{noOfRxAntennas}R" → "4T4R". Prefer the
        # ``split`` mechanism (real component attrs) for anything you also want
        # to emit as a set line.
        if it.attr_format:
            missing = []

            def _repl(m, _low=low):
                name = m.group(1)
                v = _low.get(name.lower())
                if v is None or str(v) == "":
                    missing.append(name)
                    return ""
                return str(v)

            actual_str = re.sub(r"\{([^}]+)\}", _repl, it.attr_format)
            if missing:
                status, actual_str = "NotFound", ""
            else:
                norm = _NORMALIZERS.get(it.norm, normalize)
                status = ("Match" if norm(actual_str) == norm(it.expected)
                          else "Mismatch")
            results.append(AuditResult(
                it.category, it.key, eff_mo, it.parameter,
                it.expected, actual_str, status, it.source, it.node, ref_cell))
            continue
        # Attribute lookup is case-insensitive (dump uses canonical casing).
        actual = lookup.get(it.parameter)
        if actual is None:
            actual = low.get(it.parameter.lower())
        if actual is None:
            status = "NotFound"
            actual_str = ""
        else:
            actual_str = str(actual)
            norm = _NORMALIZERS.get(it.norm, normalize)
            status = ("Match" if norm(actual_str) == norm(it.expected)
                      else "Mismatch")
        results.append(AuditResult(
            it.category, it.key, eff_mo, it.parameter,
            it.expected, actual_str, status, it.source, it.node, ref_cell))
    return results


# ── Excel report ────────────────────────────────────────────────────
_FILL_MISMATCH = PatternFill("solid", fgColor="FFC7CE")   # red
_FILL_NOTFOUND = PatternFill("solid", fgColor="FFEB9C")   # yellow
_FILL_MATCH = PatternFill("solid", fgColor="C6EFCE")      # green
_FILL_HEADER = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER = Alignment(horizontal="center", vertical="center")
_LEFT = Alignment(horizontal="left", vertical="center")


def _write_summary(summ, results, meta):
    counts = {"Match": 0, "Mismatch": 0, "NotFound": 0, "MO_NotFound": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results)

    # ── Title banner ─────────────────────────────────────────────
    summ.merge_cells("A1:C1")
    t = summ["A1"]
    t.value = "CDD Audit Report"
    t.font = Font(bold=True, size=18, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor="2F5597")
    t.alignment = _CENTER
    summ.row_dimensions[1].height = 32

    # ── Meta block ───────────────────────────────────────────────
    r = 3
    for k, v in meta.items():
        lc = summ.cell(r, 1, k)
        summ.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        vc = summ.cell(r, 2, v)
        lc.font = Font(bold=True, color="1F3864")
        lc.fill = PatternFill("solid", fgColor="E7EFF9")
        for c in (lc, vc):
            c.border = _BORDER
            c.alignment = _LEFT
        r += 1

    # ── Results section ──────────────────────────────────────────
    r += 1
    summ.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    h = summ.cell(r, 1, "Results")
    h.font = Font(bold=True, size=13, color="FFFFFF")
    h.fill = PatternFill("solid", fgColor="4472C4")
    h.alignment = _CENTER
    r += 1
    for c, txt in enumerate(("Metric", "Count", "Share"), 1):
        cc = summ.cell(r, c, txt)
        cc.font = Font(bold=True, color="FFFFFF")
        cc.fill = PatternFill("solid", fgColor="8EA9DB")
        cc.alignment = _CENTER
        cc.border = _BORDER
    r += 1

    def kpi(label, count, fill, font_color, pct=True):
        nonlocal r
        share = f"{count / total * 100:.1f}%" if (total and pct) else ""
        a = summ.cell(r, 1, label)
        b = summ.cell(r, 2, count)
        c = summ.cell(r, 3, share)
        for cell in (a, b, c):
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.font = Font(bold=True, color=font_color)
            cell.border = _BORDER
            cell.alignment = _CENTER
        a.alignment = _LEFT
        r += 1

    kpi("Total checks", total, "D9E1F2", "1F3864", pct=False)
    kpi("✔  Match", counts["Match"], "C6EFCE", "006100")
    kpi("✖  Mismatch", counts["Mismatch"], "FFC7CE", "9C0006")
    kpi("⚠  Parameter not found", counts["NotFound"], "FFEB9C", "9C6500")
    kpi("⚠  MO not found", counts["MO_NotFound"], "F8CBAD", "833C00")

    # ── Compliance headline ──────────────────────────────────────
    r += 1
    comp = counts["Match"] / total * 100 if total else 0.0
    lc = summ.cell(r, 1, "Compliance")
    lc.font = Font(bold=True, size=12, color="1F3864")
    lc.alignment = _LEFT
    summ.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
    vc = summ.cell(r, 2, f"{comp:.1f}%")
    good, warn = comp >= 95, comp >= 80
    fill = "C6EFCE" if good else ("FFEB9C" if warn else "FFC7CE")
    fcol = "006100" if good else ("9C6500" if warn else "9C0006")
    vc.fill = PatternFill("solid", fgColor=fill)
    vc.font = Font(bold=True, size=12, color=fcol)
    vc.alignment = _CENTER
    for cell in (lc, vc):
        cell.border = _BORDER

    # ── Per-node breakdown (batch/cluster audits) ────────────────
    nodes = {}
    for res in results:
        n = res.node or res.key or "-"
        nd = nodes.setdefault(n, {"Match": 0, "Mismatch": 0,
                                  "NotFound": 0, "MO_NotFound": 0})
        nd[res.status] = nd.get(res.status, 0) + 1
    if len(nodes) > 1:
        r += 2
        summ.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        h = summ.cell(r, 1, "Per-node breakdown")
        h.font = Font(bold=True, size=13, color="FFFFFF")
        h.fill = PatternFill("solid", fgColor="4472C4")
        h.alignment = _CENTER
        r += 1
        for c, txt in enumerate(("Node", "Match", "Mismatch",
                                 "Not found", "MO not found", "Compliance"), 1):
            cc = summ.cell(r, c, txt)
            cc.font = Font(bold=True, color="FFFFFF")
            cc.fill = PatternFill("solid", fgColor="8EA9DB")
            cc.alignment = _CENTER
            cc.border = _BORDER
        r += 1
        for n in sorted(nodes):
            nd = nodes[n]
            tot = sum(nd.values())
            pc = nd["Match"] / tot * 100 if tot else 0.0
            g, w = pc >= 95, pc >= 80
            pfill = "C6EFCE" if g else ("FFEB9C" if w else "FFC7CE")
            pfcol = "006100" if g else ("9C6500" if w else "9C0006")
            vals = [n, nd["Match"], nd["Mismatch"], nd["NotFound"],
                    nd["MO_NotFound"], f"{pc:.1f}%"]
            for c, v in enumerate(vals, 1):
                cc = summ.cell(r, c, v)
                cc.border = _BORDER
                cc.alignment = _LEFT if c == 1 else _CENTER
            summ.cell(r, 2).fill = PatternFill("solid", fgColor="C6EFCE")
            summ.cell(r, 3).fill = PatternFill("solid", fgColor="FFC7CE")
            pcell = summ.cell(r, 6)
            pcell.fill = PatternFill("solid", fgColor=pfill)
            pcell.font = Font(bold=True, color=pfcol)
            r += 1
        for col, wdt in (("D", 14), ("E", 16), ("F", 14)):
            summ.column_dimensions[col].width = wdt

    summ.column_dimensions["A"].width = 40
    summ.column_dimensions["B"].width = 24
    summ.column_dimensions["C"].width = 22


def write_excel(results: List[AuditResult], out_path: str, meta: dict) -> str:
    wb = Workbook()

    # Summary sheet
    summ = wb.active
    summ.title = "Summary"
    _write_summary(summ, results, meta)

    # Detail sheet
    ws = wb.create_sheet("Detail")
    headers = ["Category", "Node", "Reference Cell",
               "MO (below ManagedElement)", "Parameter",
               "CDD (expected)", "Node (actual)", "Status", "Source"]
    ws.append(headers)
    for c in ws[1]:
        c.fill = _FILL_HEADER
        c.font = _HEADER_FONT
    status_col = 8   # 1-based index of the Status column
    for r in results:
        ws.append([r.category, (r.node or r.key), r.ref_cell, r.mo, r.parameter,
                   r.expected, r.actual, r.status, r.source])
        row = ws.max_row
        fill = {"Mismatch": _FILL_MISMATCH, "NotFound": _FILL_NOTFOUND,
                "MO_NotFound": _FILL_NOTFOUND, "Match": _FILL_MATCH}.get(r.status)
        if fill:
            ws.cell(row=row, column=status_col).fill = fill

    ws.freeze_panes = "A2"
    # Auto-size the Detail sheet only; Summary has hand-tuned widths + merges.
    for col in ws.columns:
        width = 0
        letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value is not None:
                width = max(width, min(len(str(cell.value)), 60))
        ws.column_dimensions[letter].width = max(width + 2, 10)

    wb.save(out_path)
    return out_path


# ── moshell SetParameter script generation ──────────────────────────
# Top-level MO areas in the order a moshell alignment script conventionally
# lists them; anything else is appended after, alphabetically.
_MO_GROUP_ORDER = [
    "Transport", "SystemFunctions", "NodeSupport", "Equipment",
    "ManagedElement", "ENodeBFunction", "GNBDUFunction", "GNBCUCPFunction",
    "GNBCUUPFunction", "NRNetwork", "GeranCell",
]


def _mo_group(mo: str) -> str:
    """Top-level MO class of an FDN, e.g. 'ENodeBFunction=1,EUtranCellFDD=X'
    → 'ENodeBFunction'."""
    first = mo.split(",", 1)[0]
    return first.split("=", 1)[0].strip() or "Other"


def _banner(name: str) -> str:
    bar = "-" * (len(name) + 2)
    return f"# {bar}\n# {name}\n# {bar}"


def generate_moshell_scripts(results: List[AuditResult], out_dir: str,
                             site: str, audit_xlsx: str,
                             generated_by: str = "",
                             statuses=("Mismatch",)) -> List[str]:
    """Write one moshell ``set`` script per node from the audit results whose
    status is in ``statuses`` (default: Mismatch only). Each ``set`` line uses
    the CDD (expected) value. Returns the list of files written."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = _stamp()

    # node → group → list of (mo, parameter, expected); de-dup identical
    # (node, mo, parameter) so a param audited from repeated CDD rows yields
    # a single set line.
    by_node: Dict[str, Dict[str, list]] = {}
    seen = set()
    for r in results:
        if r.status not in statuses or r.expected == "":
            continue
        node = r.node or r.key or site
        sig = (node, r.mo, r.parameter)
        if sig in seen:
            continue
        seen.add(sig)
        by_node.setdefault(node, {}).setdefault(_mo_group(r.mo), []).append(
            (r.mo, r.parameter, r.expected))

    written: List[str] = []
    for node, groups in by_node.items():
        lines = [
            "# " + "-" * 60,
            f"# Generate by: {generated_by or 'NodeCraft'}",
            f"# Datetime: {stamp}",
            f"# Audit File: {os.path.basename(audit_xlsx)}",
            "# Sheet Name: Detail",
            "# " + "-" * 60,
            "",
            f"l mkdir ~/LOGS/{site}",
            '$timeCheck = `date "+%y%m%d_%H%M%S"`',
            f"l+ ~/LOGS/{site}/{node}_SetParameter_$timeCheck.log",
            "",
            "lt all",
            "gs+",
            "alt",
            "",
        ]
        ordered = [g for g in _MO_GROUP_ORDER if g in groups]
        ordered += sorted(g for g in groups if g not in _MO_GROUP_ORDER)
        for g in ordered:
            lines.append("")
            lines.append(_banner(g))
            # Sort by parameter (then MO) so all lines of one parameter are
            # contiguous — easy to review or delete a whole parameter at once.
            for mo, param, val in sorted(groups[g], key=lambda x: (x[1], x[0])):
                lines.append(f"set {mo}$ {param} {val}")
        # Close the log opened with l+ at the top.
        lines.append("")
        lines.append("l-")
        path = os.path.join(out_dir, f"{node}_SetParameter_{stamp}.mos")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(path)
    return written


def _stamp() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
