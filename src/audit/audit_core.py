"""
audit_core.py — compare CDD expected values against parsed node dump records
and write an Excel report.

``normalize`` + the Match/Mismatch/NotFound verdict are ported from
enp-generator ``services/audit_engine.py`` / ``services/node_audit.py``.
"""
from __future__ import annotations

import os
import collections
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# ── Value normalization (from enp-generator audit_engine.normalize) ──
_ENUM_RE = re.compile(r"^\d+\s*\((.+)\)$")


def normalize(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # Dump enums render as "<code> (<LABEL>)" e.g. "0 (NORMAL_SECTOR)"; the CDD
    # carries the LABEL. Compare on the label so they match.
    m = _ENUM_RE.match(s)
    if m:
        return m.group(1).strip().lower()
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


_LATLON_B = re.compile(r"^([NSEW])\s*0*(\d+)[-\s]0*(\d+)[-\s]0*(\d+(?:\.\d+)?)",
                       re.IGNORECASE)                         # N07-40-08.15
_LATLON_A = re.compile(r"0*(\d+)\s*[°\-]\s*0*(\d+)\s*['’\-]\s*"
                       r"0*(\d+(?:\.\d+)?)\s*[\"”\-]?\s*([NSEW])",
                       re.IGNORECASE)                         # 7°40'8.15"N


def normalize_latlong(v) -> str:
    """Normalize a DMS coordinate to signed decimal degrees so the CDD form
    (e.g. 7°40'8.15\"N) and the node form (e.g. N07-40-08.15) compare equal."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() == "null":
        return ""
    m = _LATLON_B.match(s)
    if m:
        hemi, d, mn, sec = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        m = _LATLON_A.search(s)
        if not m:
            return s.lower()
        d, mn, sec, hemi = m.group(1), m.group(2), m.group(3), m.group(4)
    dec = int(d) + int(mn) / 60.0 + float(sec) / 3600.0
    if hemi.upper() in ("S", "W"):
        dec = -dec
    return f"{dec:.5f}"


def normalize_list(v) -> str:
    """Normalize a multi-value field so different renderings compare equal:
    CDD ``0 1 2 3`` or ``1&37`` vs node ``[0, 1, 2, 3]`` / ``[1, 37]``. Splits on
    brackets/comma/ampersand/space, numeric-normalizes each token, rejoins."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() == "null":
        return ""
    parts = [p for p in re.split(r"[\[\],&\s]+", s) if p != ""]
    out = []
    for p in parts:
        try:
            f = float(p)
            out.append(str(int(f)) if f.is_integer() else str(f))
        except (ValueError, TypeError):
            out.append(p.lower())
    return ",".join(out)


def normalize_geo(v) -> str:
    """Normalize LTE/NR coordinates: the dump stores integer **micro-degrees**
    (e.g. 6999000 = 6.999°), the CDD stores decimal degrees (6.999). Convert
    both to decimal degrees so they compare equal."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() == "null":
        return ""
    if re.fullmatch(r"-?\d+", s):          # integer micro-degrees (from dump)
        return f"{int(s) / 1e6:.5f}"
    try:
        return f"{float(s):.5f}"           # decimal degrees (from CDD)
    except (ValueError, TypeError):
        return s.lower()


def normalize_bbtype(v) -> str:
    """Normalize a baseband/RAN-processor type so the CDD short form matches the
    node's ``productName``: CDD ``RP6655`` / ``BB6621`` vs node ``RAN Processor
    6655`` / ``Baseband 6621`` — compare on the trailing product number."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() == "null":
        return ""
    m = re.search(r"(\d{4})", s)
    return m.group(1) if m else s.lower()


def normalize_bw(v) -> str:
    """Channel bandwidth to a common unit: the CDD is in MHz (``10``), the node's
    ``dl/ulChannelBandwidth`` is in kHz (``10000``). Reduce both to MHz so
    ``10`` and ``10000`` compare equal (``1.4``/``1400`` handled too)."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() == "null":
        return ""
    m = re.search(r"[-+]?\d*\.?\d+", s)
    if not m:
        return s.lower()
    f = float(m.group())
    if f >= 1000:            # kHz → MHz
        f = f / 1000.0
    return str(int(f)) if f == int(f) else str(f)


# name -> comparator; selected per-column via the CDD map's optional "norm".
_NORMALIZERS = {"segments": normalize_segments, "latlong": normalize_latlong,
                "list": normalize_list, "geo": normalize_geo,
                "bbtype": normalize_bbtype, "bw": normalize_bw}


# Boolean equivalence for the compare: many GSM/LTE attrs are ``1``/``0`` in the
# CDD but ``ACTIVE``/``INACTIVE`` (or ON/OFF, …) on the node — the same value.
_BOOL_ON = {"1", "on", "active", "true", "enabled", "yes", "unlocked"}
_BOOL_OFF = {"0", "off", "inactive", "false", "disabled", "no", "locked"}


def _bool_canon(x):
    s = str(x).strip().lower()
    if s in _BOOL_ON:
        return "1"
    if s in _BOOL_OFF:
        return "0"
    return None


def _bool_equal(a, b) -> bool:
    """True when both sides are boolean-like and denote the same state, so CDD
    ``1`` == node ``ACTIVE`` (and ``0`` == ``INACTIVE``/``OFF``/…)."""
    ca, cb = _bool_canon(a), _bool_canon(b)
    return ca is not None and ca == cb


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
    attr_fallback: str = ""  # try this attr if ``parameter`` is absent — e.g.
                           #   EUtranCellFDD uses earfcnDl/Ul, EUtranCellTDD uses
                           #   a single ``earfcn``
    attr_alt: str = ""     # accept EITHER attr — match if expected equals this
                           #   MO's value OR the alt's, and the alt wins for
                           #   display when the primary is empty/0 (e.g. MIMO
                           #   noOfRxAntennas=0 but noOfUsedRxAntennas=4)
    from_cmedit: bool = False  # sourced from live cmedit (BSC GeranCell) — such
                           #   params can't be set via moshell, so they're kept
                           #   out of the .mos script (cmedit/cmbulk only)


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
    norm: str = ""         # comparator hint carried from the CDD item, so a
                           #   generated set can re-format the value to the
                           #   node's convention (list/latlong/geo/…)
    from_cmedit: bool = False  # BSC/cmedit-sourced → kept out of the .mos script


@dataclass
class LldResult:
    """One LLD (physical baseband / CPRI) row — one planned link (or the
    baseband unit), with LLD-vs-Node values side by side. Kept separate from the
    logical CDD ``AuditResult`` so it gets its own sheet with paired
    (LLD | Node) columns rather than the generic MO/Parameter ones.

    A link is paired to a node RiLink by BB port first, then by radio (band) —
    so an AAS radio planned on port P but wired on port H still lines up on one
    row, exposing the port mapping instead of reporting a phantom hole."""
    node: str
    bbid: str                  # "BB1" (from the node's B0<k> suffix)
    ref_cell: str = ""         # Sector/Radio, e.g. "S2/R1"
    bb_port_lld: str = ""      # BB RiPort — planned vs actual
    bb_port_node: str = ""
    hw_type_lld: str = ""      # baseband type or radio type — planned vs actual
    hw_type_node: str = ""
    data_port_lld: str = ""    # Radio DATA port — planned vs actual
    data_port_node: str = ""
    status: str = ""           # Match | Mismatch | NotFound | Unplanned
    source: str = ""
    # per-metric verdicts (False → that LLD|Node pair differs → highlighted).
    bb_ok: bool = True
    hw_ok: bool = True
    data_ok: bool = True


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
                it.expected, "", "MO_NotFound", it.source, it.node, ref_cell,
                it.norm, it.from_cmedit))
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
                    it.expected, "", "MO_NotFound", it.source, it.node, ref_cell,
                    it.norm, it.from_cmedit))
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
                status = ("Match" if (norm(actual_str) == norm(it.expected)
                                      or _bool_equal(it.expected, actual_str))
                          else "Mismatch")
            results.append(AuditResult(
                it.category, it.key, eff_mo, it.parameter,
                it.expected, actual_str, status, it.source, it.node, ref_cell,
                it.norm, it.from_cmedit))
            continue
        # Attribute lookup is case-insensitive (dump uses canonical casing).
        actual = lookup.get(it.parameter)
        if actual is None:
            actual = low.get(it.parameter.lower())
        if actual is None and it.attr_fallback:
            actual = (lookup.get(it.attr_fallback)
                      or low.get(it.attr_fallback.lower()))
        # Alternative attribute: accept EITHER MO value. The alt is preferred for
        # display when the primary is empty/0 (e.g. noOfRxAntennas=0 while
        # noOfUsedRxAntennas=4 — the "used" one is the real answer).
        actual_alt = None
        if it.attr_alt:
            actual_alt = (lookup.get(it.attr_alt)
                          or low.get(it.attr_alt.lower()))
        if actual is None and actual_alt is None:
            # The MO EXISTS (we resolved it above) but the attribute isn't
            # present — i.e. the node's value is null/unset. The CDD expects a
            # value, so that's a Mismatch (not a NotFound, which we reserve for
            # a value we couldn't locate at all / a missing MO).
            status = "Mismatch"
            actual_str = "(not set)"
        else:
            norm = _NORMALIZERS.get(it.norm, normalize)
            prim = "" if actual is None else str(actual)
            alt = "" if actual_alt is None else str(actual_alt)
            # Display the primary unless it's empty/0 and the alt has a value.
            actual_str = (alt if prim in ("", "0") and alt not in ("", "0")
                          else prim)
            exp_n = norm(it.expected)
            match = (norm(prim) == exp_n or _bool_equal(it.expected, prim)
                     or (it.attr_alt and (norm(alt) == exp_n
                                          or _bool_equal(it.expected, alt))))
            status = "Match" if match else "Mismatch"
        results.append(AuditResult(
            it.category, it.key, eff_mo, it.parameter,
            it.expected, actual_str, status, it.source, it.node, ref_cell,
            it.norm, it.from_cmedit))
    return results


_CELL_MO_INV = re.compile(
    r"(?:^|,)(EUtranCellFDD|EUtranCellTDD|NRCellDU|NRCellCU|GeranCell|GsmSector)"
    r"=([^,]+)",
    re.IGNORECASE)


_SEC_SUFFIX_RE = re.compile(r"-([LR]?)(\d+)\s*$", re.IGNORECASE)


def _sector_parts(cellname: str):
    """('BULUANX-L1') → (layer 'BULUANX', lr 'L', sector '1'); ('BULUAN-1') →
    ('BULUAN', '', '1'). None if the name has no sector suffix."""
    m = _SEC_SUFFIX_RE.search(cellname or "")
    if not m:
        return None
    layer = _SEC_SUFFIX_RE.sub("", cellname)
    return layer, m.group(1).upper(), m.group(2)


def _parse_trx_agg(text: str) -> List[str]:
    """Split a TRX-count formula into one term per sector, respecting parens:
    ``3+3+2`` → ['3','3','2']; ``(2+2)+2+2`` → ['(2+2)','2','2']."""
    terms, depth, cur = [], 0, ""
    for ch in str(text):
        if ch == "(":
            depth += 1; cur += ch
        elif ch == ")":
            depth -= 1; cur += ch
        elif ch == "+" and depth == 0:
            terms.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        terms.append(cur)
    return [t.replace(" ", "") for t in terms if t.strip()]


def trx_count_check(trx_items: List[AuditItem],
                    records: Dict[str, Dict[str, str]]) -> List[AuditResult]:
    """Audit TRX count per sector against the CDD's aggregate formula.

    A GsmSector name ends in ``[L|R]?<sector>`` (BULUAN-1, BULUANX-L1). Cells
    sharing the stripped prefix form a *layer* (BULUAN, BULUANX) whose CDD
    ``TRX COUNT`` reads e.g. ``3+3+2`` or ``(2+2)+2+2`` — one term per sector,
    with ``(L+R)`` when the sector is split Left/Right. This counts the node's
    Trx per sector, rebuilds the same expression, and reports Match/Mismatch
    per sector so a wrong sector is named exactly."""
    import collections
    # node: layer -> sector -> {lr: trx_count}
    node = collections.defaultdict(lambda: collections.defaultdict(dict))
    trx_by_sector = collections.defaultdict(int)
    for ldn in records:
        if ldn.split(",")[-1].split("=", 1)[0] != "Trx":
            continue
        m = re.search(r"GsmSector=([^,]+)", ldn)
        if m:
            trx_by_sector[m.group(1)] += 1
    for sec_name, count in trx_by_sector.items():
        parts = _sector_parts(sec_name)
        if not parts:
            continue
        layer, lr, sector = parts
        node[layer][sector][lr or ""] = count

    # cdd: layer -> (expected formula, node label)
    cdd = {}
    for it in trx_items:
        m = re.search(r"GsmSector=([^,]+)", it.mo_local)
        name = m.group(1) if m else it.key
        parts = _sector_parts(name)
        if not parts:
            continue
        cdd.setdefault(parts[0], (it.expected, it.node or it.key))

    def _node_term(sec_map: dict) -> str:
        if "L" in sec_map or "R" in sec_map:
            return f"({sec_map.get('L', 0)}+{sec_map.get('R', 0)})"
        return str(sec_map.get("", sum(sec_map.values())))

    out: List[AuditResult] = []
    for layer, (expected, node_name) in sorted(cdd.items()):
        exp_terms = _parse_trx_agg(expected)
        sectors = sorted(node.get(layer, {}))
        n = max(len(exp_terms), len(sectors))
        for i in range(n):
            exp = exp_terms[i] if i < len(exp_terms) else "(none)"
            act = (_node_term(node[layer][sectors[i]])
                   if i < len(sectors) else "(none)")
            status = "Match" if exp.replace(" ", "") == act.replace(" ", "") \
                else "Mismatch"
            out.append(AuditResult(
                "trx-count", node_name, f"GsmSector [{layer}]",
                f"TRX Sect{i + 1}", exp, act, status,
                f"CDD TRX COUNT ({expected})", node_name))
    return out


def broker_check(broker_items: List[AuditItem],
                 records: Dict[str, Dict[str, str]],
                 bsc_broker_map: Dict[str, str]) -> List[AuditResult]:
    """Audit each GSM node's IP-broker against config.json ``bsc_broker_map``.

    The CDD names the BSC a node belongs to (``BSC`` column → ``__bscbroker__``
    items); ``bsc_broker_map`` maps that BSC to its broker IP. EVERY ``AbisIp``
    MO's ``bscBrokerIpAddress`` (from the dump) is compared to that expected IP —
    ONE ROW PER AbisIp, keyed by its ``GsmSector`` (unique) — catching any single
    sector wired to the WRONG BSC's broker (the ping still succeeds, so it is
    otherwise invisible). The actual IP is annotated with the BSC it points at."""
    # node → expected BSC name (from the CDD BSC column), first non-empty wins.
    bsc_by_node: Dict[str, str] = {}
    for it in broker_items:
        node = (it.node or it.key or "").strip()
        bsc = (it.expected or "").strip()
        if node and bsc:
            bsc_by_node.setdefault(node, bsc)

    # reverse map: broker IP → BSC name, so a wrong actual IP names its real BSC.
    ip_to_bsc = {str(v).strip(): k for k, v in (bsc_broker_map or {}).items()}

    out: List[AuditResult] = []
    for ldn, attrs in records.items():
        if ldn.split(",")[-1].split("=", 1)[0] != "AbisIp":
            continue
        m = re.search(r"ManagedElement=([^,]+)", ldn)
        node = m.group(1) if m else ""
        bsc = bsc_by_node.get(node)
        if not bsc:
            continue          # node not in the CDD BSC map — nothing to expect
        # Real MO FDN below ManagedElement (e.g. BtsFunction=1,GsmSector=X,
        # AbisIp=1) so the generated set line targets the correct instance.
        mo = re.sub(r"^.*?ManagedElement=[^,]+,", "", ldn)
        sec = re.search(r"GsmSector=([^,]+)", ldn)
        sector = sec.group(1) if sec else "?"
        sname = (attrs.get("gsmSectorName") or "").strip()
        ip = (attrs.get("bscBrokerIpAddress") or "").strip()
        expected_ip = (bsc_broker_map or {}).get(bsc)
        ref = f"GsmSector={sector}" + (f" ({sname})" if sname else "")
        if expected_ip is None:
            status = "NotFound"
            # Non-settable placeholder: expected IP unknown, so no set line is
            # generated (NotFound rows are skipped by the generators anyway).
            expected_val = f"{bsc} (not in bsc_broker_map)"
            actual_disp = ip or "(none)"
            src = "config.json bsc_broker_map"
        else:
            # The expected value is the CLEAN IP so it can be emitted as a
            # settable ``bscBrokerIpAddress <ip>`` line; the BSC name it belongs
            # to is carried in the Source column instead of inline.
            expected_val = str(expected_ip).strip()
            b = ip_to_bsc.get(ip)
            actual_disp = (f"{ip} ({b})" if b else ip) or "(none)"
            src = f"config.json bsc_broker_map ({bsc})"
            if not ip:
                status = "NotFound"
            elif ip == str(expected_ip).strip():
                status = "Match"
            else:
                status = "Mismatch"
        out.append(AuditResult(
            "ip-broker", node, mo, "bscBrokerIpAddress",
            expected_val, actual_disp, status, src, node, ref_cell=ref))
    return out


def audit_sw_level(records: Dict[str, Dict[str, str]], expected: str,
                   nodes=None, log=lambda m: None) -> List[AuditResult]:
    """Audit each node's software level against the expected UpgradePackage id
    from config.json (``uri_setting.upgrade_package_id``) — the same source of
    truth the integration "SW Level Check" step uses, but read from the DUMP
    instead of a live AMOS ``pr`` and reported as an audit row.

    The dump carries ``SystemFunctions=1,SwM=1,UpgradePackage=<id>`` MOs (the MO
    id IS the version, e.g. ``CXP2010174/2-R42J13``), each with a ``state`` such
    as ``7 (COMMIT_COMPLETED)``. A node is Match when the expected id is present
    (mirroring the integration check's ``expected in found``); the committed
    package is shown when it differs. Nodes with no dump records are skipped
    (nothing to read); a node that has records but no UpgradePackage is
    NotFound. Emits one row per node."""
    expected = (expected or "").strip()
    if not expected:
        return []
    import collections
    ups_by_node = collections.defaultdict(list)   # node → [(up_id, state)]
    node_has_records = set()
    for ldn, a in records.items():
        mn = re.search(r"ManagedElement=([^,]+)", ldn)
        if mn:
            node_has_records.add(mn.group(1))
        parts = ldn.split(",")
        leaf = parts[-1].split("=", 1)
        if leaf[0] != "UpgradePackage" or len(leaf) < 2:
            continue
        if not any(p.startswith("SwM=") for p in parts):
            continue
        if mn and leaf[1]:
            ups_by_node[mn.group(1)].append(
                (leaf[1], str(a.get("state") or "")))

    target = list(nodes) if nodes else sorted(node_has_records)
    out: List[AuditResult] = []
    for node in target:
        if node not in node_has_records:
            continue                      # no dump for this node — nothing to read
        ups = ups_by_node.get(node, [])
        found = [u for u, _ in ups]
        # The EFFECTIVE package is the COMMIT_COMPLETED one. modump reports the
        # state as "7 (COMMIT_COMPLETED)", cmdump as bare "COMMIT_COMPLETED" —
        # match the label, not the numeric code, so both are covered. When a
        # node carries several packages (e.g. a PREPARE_COMPLETED leftover next
        # to the committed one) only the committed one is compared to expected.
        committed = [u for u, s in ups if "COMMIT_COMPLETED" in s.upper()]
        if committed:
            effective = committed[0]
        elif len(found) == 1:
            effective = found[0]
        else:
            effective = ""
        if not found:
            status, actual = "NotFound", "(none)"
        elif not effective:
            status, actual = "NotFound", \
                "no COMMIT_COMPLETED among: " + ", ".join(sorted(set(found)))
        elif effective == expected:
            status, actual = "Match", effective
        else:
            status, actual = "Mismatch", effective
        out.append(AuditResult(
            "sw-level", node, "SystemFunctions=1,SwM=1,UpgradePackage",
            "UpgradePackage", expected, actual, status,
            "config.json uri_setting.upgrade_package_id", node))
    return out


def audit_gnbid_consistency(records: Dict[str, Dict[str, str]],
                            nodes=None, log=lambda m: None) -> List[AuditResult]:
    """Internal consistency: a gNodeB's identity (gNBId) is stored on three MOs —
    ``GNBDUFunction``, ``GNBCUCPFunction`` and ``GNBCUUPFunction`` — which MUST all
    carry the SAME value. A mismatch is a real misconfiguration (a CU/DU-split or
    a hand-edit gone wrong) that the per-MO CDD audit (which reads only
    GNBDUFunction) would not catch. Needs no CDD/expected and no live state, so it
    is valid even before integration. ONE ROW PER MO (GNBDUFunction /
    GNBCUCPFunction / GNBCUUPFunction), each compared to the consensus value, so
    the odd MO out is flagged on its own line.

    Note: ``ExternalGNodeBFunction`` also carries a gNB id, but under a DIFFERENT
    attribute (``gNodeBId``; its ``gNBId`` is -1) and it is a NEIGHBOUR list, not
    this node's identity — so it is deliberately excluded here (handled by the
    EN-DC self-reference check instead)."""
    import collections
    per = collections.defaultdict(dict)     # node → {MO leaf: (mo_below, gNBId)}
    for ldn, a in records.items():
        leaf = ldn.split(",")[-1].split("=", 1)[0]
        if leaf not in ("GNBDUFunction", "GNBCUCPFunction", "GNBCUUPFunction"):
            continue
        v = ("" if a.get("gNBId") is None else str(a.get("gNBId")).strip())
        if not v or v == "-1":
            continue
        mn = re.search(r"ManagedElement=([^,]+)", ldn)
        if mn:
            mo_below = re.sub(r"^.*?ManagedElement=[^,]+,", "", ldn)
            per[mn.group(1)][leaf] = (mo_below, v)

    order = ["GNBDUFunction", "GNBCUCPFunction", "GNBCUUPFunction"]
    out: List[AuditResult] = []
    target = list(nodes) if nodes else sorted(per)
    for node in target:
        d = per.get(node)
        if not d:
            continue
        # Consensus = most common value (ties resolved toward DU, the canonical
        # source); each MO is Match when it equals the consensus.
        cnt = collections.Counter(v for _, v in d.values())
        ref = None
        if "GNBDUFunction" in d:
            ref = d["GNBDUFunction"][1]
        top = cnt.most_common(1)[0]
        if top[1] > 1 or ref is None:
            ref = top[0]
        for leaf in order:
            if leaf not in d:
                continue
            mo_below, v = d[leaf]
            status = "Match" if v == ref else "Mismatch"
            out.append(AuditResult(
                "consistency", node, mo_below, "gNBId", ref, v, status,
                "internal cross-MO consistency (DU/CUCP/CUUP)", node))
    return out


def audit_endc_external(records: Dict[str, Dict[str, str]],
                        nodes=None, log=lambda m: None) -> List[AuditResult]:
    """EN-DC self-reference audit (co-sited NR under the SAME PLA/node).

    On an EN-DC node the LTE side keeps an ``ExternalGNodeBFunction`` for the
    node's OWN gNB (``gNodeBId`` == the node's ``GNBDUFunction.gNBId``), and under
    it one ``ExternalGUtranCell`` per NR cell mirroring that cell's identity. If
    the mirror is wrong or missing, EN-DC addition to the co-sited gNB fails.
    This checks, per node that has NR:

      * the self ``ExternalGNodeBFunction`` (gNodeBId == own gNBId) EXISTS;
      * each self ``ExternalGUtranCell.nRPCI`` equals the real ``NRCellDU.nRPCI``
        of the cell with the same ``localCellId``.

    Pure config + internal — no CDD/expected and no live state, valid
    pre-integration. ``ExternalGNodeBFunction`` uses attribute ``gNodeBId`` (its
    ``gNBId`` is -1)."""
    import collections

    def _n(x):
        return "" if x is None else str(x).strip()

    def _below(l):
        return re.sub(r"^.*?ManagedElement=[^,]+,", "", l)

    own = {}                                       # node → own gNBId
    nrpci = collections.defaultdict(dict)          # node → {cellLocalId: nRPCI}
    self_ext = collections.defaultdict(list)       # node → [ext gNB LDN] (self)
    ext_children = collections.defaultdict(list)   # ext gNB LDN → [(LDN, attrs)]

    for l, a in records.items():
        leaf = l.split(",")[-1].split("=", 1)[0]
        mn = re.search(r"ManagedElement=([^,]+)", l)
        node = mn.group(1) if mn else ""
        if leaf in ("GNBDUFunction", "GNBCUCPFunction"):
            v = _n(a.get("gNBId"))
            if node and v and v != "-1":
                own.setdefault(node, v)
        elif leaf == "NRCellDU":
            cid, pci = _n(a.get("cellLocalId")), _n(a.get("nRPCI"))
            if node and cid and pci:
                nrpci[node][cid] = pci
        elif leaf == "ExternalGUtranCell":
            ext_children[l.rsplit(",", 1)[0]].append((l, a))

    # Second pass for self ExternalGNodeBFunction (needs own[] populated).
    for l, a in records.items():
        if l.split(",")[-1].split("=", 1)[0] != "ExternalGNodeBFunction":
            continue
        mn = re.search(r"ManagedElement=([^,]+)", l)
        node = mn.group(1) if mn else ""
        if node and _n(a.get("gNodeBId")) == own.get(node):
            self_ext[node].append(l)

    out: List[AuditResult] = []
    target = list(nodes) if nodes else sorted(own)
    for node in target:
        g = own.get(node)
        if not g:
            continue                               # LTE-only node — nothing to mirror
        keys = self_ext.get(node)
        if not keys:
            out.append(AuditResult(
                "endc", node, "ExternalGNodeBFunction",
                "self gNB reference", g, "(missing)", "Mismatch",
                "EN-DC self-reference (same PLA)", node))
            continue
        out.append(AuditResult(
            "endc", node, _below(sorted(keys)[0]),
            "self gNB reference", g, g, "Match",
            "EN-DC self-reference (same PLA)", node))
        for sk in keys:
            for cl, ca in ext_children.get(sk, []):
                cid = _n(ca.get("localCellId"))
                ext_pci = _n(ca.get("nRPCI"))
                real = nrpci.get(node, {}).get(cid)
                if real is None:
                    st, exp, act = "NotFound", f"(no NRCellDU localCellId={cid})", \
                        (ext_pci or "(none)")
                elif ext_pci == real:
                    st, exp, act = "Match", real, ext_pci
                else:
                    st, exp, act = "Mismatch", real, ext_pci
                out.append(AuditResult(
                    "endc", node, _below(cl), "nRPCI (ext mirror vs NRCellDU)",
                    exp, act, st, "EN-DC external cell mirror", node,
                    ref_cell=f"localCellId={cid}"))
    return out


def audit_chain_trace(records: Dict[str, Dict[str, str]],
                      nodes=None, log=lambda m: None) -> List[AuditResult]:
    """End-to-end MO reference-chain traceability, per cell/sector:

        Cell / GsmSector
          → SectorCarrier / NRSectorCarrier / Trx
            → SectorEquipmentFunction
              → Radio (rfBranchRef → FieldReplaceableUnit[/Transceiver] or RfBranch)

    Each hop is a reference attribute; if any is unset or points to an MO that is
    not in the dump, the chain is broken and the cell can't carry traffic. This
    walks the chain and reports, per cell, whether it fully resolves (Match) or
    the first hop that breaks (Mismatch). Pure internal — no CDD/expected and no
    live state, valid pre-integration. On AAS radios the chain ends at the radio
    FRU/Transceiver (there is no separate RfBranch MO), which counts as resolved."""
    def _below(l):
        return re.sub(r"^.*?ManagedElement=[^,]+,", "", l)

    def _n(x):
        return "" if x is None else str(x).strip()

    # Resolver maps (built once): a ref may be a full FDN or omit ManagedElement.
    by_full = records
    by_below = {}
    for k, v in records.items():
        by_below.setdefault(_below(k), v)

    def resolve(ref):
        r = _n(ref)
        if not r:
            return None
        return by_full.get(r) or by_below.get(_below(r))

    def exists(ref):
        return resolve(ref) is not None

    # Trx per GsmSector (GSM chain starts GsmSector → Trx → SEF).
    import collections
    trx_by_sector = collections.defaultdict(list)
    for l, a in records.items():
        if l.split(",")[-1].split("=", 1)[0] == "Trx":
            m = re.search(r"((?:.*,)?GsmSector=[^,]+)", _below(l))
            if m:
                trx_by_sector[m.group(1)].append(a)

    def _radio_name(ref):
        s = _n(ref)
        m = re.search(r"FieldReplaceableUnit=([^,;]+)", s)
        if m:
            return m.group(1)
        m = re.search(r"AntennaUnitGroup=([^,;]+)", s)
        if m:
            return m.group(1)                     # classic radio (RfBranch list)
        return _below(s.split(";")[0]).split(",")[-1]

    out: List[AuditResult] = []
    target = set(nodes) if nodes else None
    for l, a in records.items():
        leaf = l.split(",")[-1].split("=", 1)[0]
        mn = re.search(r"ManagedElement=([^,]+)", l)
        node = mn.group(1) if mn else ""
        if target is not None and node not in target:
            continue
        ref_cell = l.split(",")[-1]

        # Resolve hop 1 (cell → carrier / sector → Trx) and the carrier attrs
        # that carry the SEF reference.
        if leaf in ("EUtranCellFDD", "EUtranCellTDD"):
            carrier_ref = a.get("sectorCarrierRef")
            carrier = resolve(carrier_ref)
            sef_ref = carrier.get("sectorFunctionRef") if carrier else None
            hop1_label, hop1_val = "SectorCarrier", carrier_ref
        elif leaf == "NRCellDU":
            carrier_ref = a.get("nRSectorCarrierRef")
            carrier = resolve(carrier_ref)
            sef_ref = (carrier.get("sectorEquipmentFunctionRef")
                       if carrier else None)
            hop1_label, hop1_val = "NRSectorCarrier", carrier_ref
        elif leaf == "GsmSector":
            trxs = trx_by_sector.get(_below(l), [])
            carrier = trxs[0] if trxs else None
            carrier_ref = "Trx" if trxs else ""
            sef_ref = (carrier.get("sectorEquipmentFunctionRef")
                       if carrier else None)
            hop1_label, hop1_val = "Trx", (carrier_ref or "")
        else:
            continue

        sef = resolve(sef_ref)
        radio_ref = sef.get("rfBranchRef") if sef else None
        # rfBranchRef may be a LIST on classic radios — one RfBranch per antenna
        # branch. modump separates the FDNs with ';', cmdump with whitespace; an
        # MO FDN never contains a space, so split on either. The chain resolves
        # only if every target MO is present.
        radio_targets = [r for r in re.split(r"[;\s]+", _n(radio_ref)) if r.strip()]
        radio_missing = [r for r in radio_targets if not exists(r)]

        if not carrier:
            status, actual = "Mismatch", \
                f"broken at {hop1_label}: {hop1_val or '(unset)'}"
        elif not sef_ref or not sef:
            status, actual = "Mismatch", \
                f"broken at SectorEquipmentFunction: {_n(sef_ref) or '(unset)'}"
        elif not radio_targets:
            status, actual = "Mismatch", "broken at Radio (rfBranchRef): (unset)"
        elif radio_missing:
            status, actual = "Mismatch", (
                f"broken at Radio (rfBranchRef): "
                f"{len(radio_missing)}/{len(radio_targets)} target MO(s) missing")
        else:
            status, actual = "Match", f"OK -> {_radio_name(radio_ref)}"

        out.append(AuditResult(
            "chain", node, _below(l), "ref chain (Cell->Carrier->SEF->Radio)",
            "resolves", actual, status, "MO reference traceability", node,
            ref_cell=ref_cell))
    return out


def _detect_feature_conditions(node, records, cdd_tx_by_node=None):
    """Return the set of config conditions present on ``node`` — the vocabulary
    used by ``feature_rules`` detect keys:
      lte, nr, ess, 8t8r, 4t4r, aas_b41_lte, aas_b41_nr, aas_b1b3.

    8T8R/4T4R detection priority (per spec): CDD MIMO Tx count first (reliable
    even pre-integration), then node ``noOfTxAntennas`` on (NR)SectorCarrier
    (skipping 0/-1 which mean "not set"), then the number of ``RfBranch`` MOs
    under a radio (8 → 8x8, 4 → 4x4)."""
    conds = set()
    nl = node
    fru_ids = []
    tx_counts = set()
    rfbranch_by_radio = collections.defaultdict(set)
    for ldn, a in records.items():
        m = re.search(r"ManagedElement=([^,]+)", ldn)
        if not m or m.group(1) != nl:
            continue
        leaf = ldn.split(",")[-1].split("=", 1)[0]
        if leaf in ("EUtranCellFDD", "EUtranCellTDD"):
            conds.add("lte")
        elif leaf == "NRCellDU":
            conds.add("nr")
        elif leaf == "FieldReplaceableUnit":
            fid = ldn.split(",")[-1].split("=", 1)[1]
            fru_ids.append(fid)
        elif leaf in ("SectorCarrier", "NRSectorCarrier"):
            v = a.get("noOfTxAntennas")
            try:
                iv = int(str(v).strip())
                if iv > 0:
                    tx_counts.add(iv)
            except (ValueError, TypeError):
                pass
        elif leaf == "RfBranch":
            mg = re.search(r"(AntennaUnitGroup=[^,]+)", ldn)
            if mg:
                rfbranch_by_radio[mg.group(1)].add(ldn.split(",")[-1])
        if "SpectrumSharingFunction=" in ldn:
            conds.add("ess")

    # 8T8R / 4T4R — CDD first, then node noOf*, then RfBranch count.
    tx = set(cdd_tx_by_node.get(node, set())) if cdd_tx_by_node else set()
    tx |= tx_counts
    tx |= {len(v) for v in rfbranch_by_radio.values()}
    if 8 in tx:
        conds.add("8t8r")
    if 4 in tx:
        conds.add("4t4r")

    # AAS/AIR radios by band (FieldReplaceableUnit id, e.g. AAS_B41_RRU1, AIR…B1B3)
    up = [f.upper() for f in fru_ids]
    aas_b41 = any(("AAS" in f or "AIR" in f) and "B41" in f for f in up)
    aas_b1b3 = any(("AAS" in f or "AIR" in f) and "B1B3" in f for f in up)
    if aas_b41 and "lte" in conds:
        conds.add("aas_b41_lte")
    if aas_b41 and "nr" in conds:
        conds.add("aas_b41_nr")
    if aas_b1b3:
        conds.add("aas_b1b3")
    return conds


def _feature_state(node, feat, records):
    """(featureState, licenseState, description) for ``FeatureState=<feat>`` on
    ``node``, or (None, None, "") if the MO is absent."""
    for ldn, a in records.items():
        if ldn.split(",")[-1] != f"FeatureState={feat}":
            continue
        m = re.search(r"ManagedElement=([^,]+)", ldn)
        if m and m.group(1) != node:
            continue
        return (a.get("featureState"), a.get("licenseState"),
                (a.get("description") or "").strip())
    return (None, None, "")


def audit_features(records: Dict[str, Dict[str, str]], feature_rules: dict,
                   cdd_tx_by_node=None, nodes=None,
                   log=lambda m: None) -> List[AuditResult]:
    """Conditional feature-compliance audit.

    ``feature_rules`` (from audit_map.json) maps a rule key → {detect, features}.
    A feature is expected ACTIVATED when ANY of its governing conditions is
    present on the node, else DEACTIVATED. When active it must also have
    ``licenseState = ENABLED``. Emits one row per (node, feature):
      * active & OK   → featureState ACTIVATED and licenseState ENABLED
      * active & bad  → Mismatch, remark "Feature Deactivated" / "License Missing"
      * inactive & bad→ Mismatch, remark "Should be Deactivated"
    ``ExternalGNodeBFunction``-style specifics already override baseline lists in
    the config, so each feature's condition set is exactly what should gate it."""
    if not feature_rules:
        return []
    # feature → set(conditions) that would activate it.
    feat_conds = collections.defaultdict(set)
    baseline_feats = set()          # from broad LTE/NR/ESS baseline lists
    for r in feature_rules.values():
        cond = r.get("detect")
        for f in r.get("features", []):
            feat_conds[f].add(cond)
            if r.get("baseline"):
                baseline_feats.add(f)

    def _is1(v):
        s = str(v or "").strip().upper()
        return s.startswith("1") or "ACTIVATED" in s or "ENABLED" in s

    # Friendly label per detect condition — so a row explains WHICH config it
    # belongs to (e.g. "AAS FDD", "AAS TDD", "EN-DC/NR").
    _LABEL = {"8t8r": "8T8R", "4t4r": "4T4R", "nr": "EN-DC/NR",
              "aas_b41_lte": "AAS TDD", "aas_b41_nr": "AAS TDD",
              "aas_b1b3": "AAS FDD", "lte": "LTE", "ess": "ESS"}

    def _labels(conds_set):
        seen, out_l = set(), []
        for c in sorted(conds_set):
            lab = _LABEL.get(c, c)
            if lab not in seen:
                seen.add(lab)
                out_l.append(lab)
        return "/".join(out_l)

    # nodes that actually appear in the dump
    node_set = set()
    for ldn in records:
        m = re.search(r"ManagedElement=([^,]+)", ldn)
        if m:
            node_set.add(m.group(1))
    target = [n for n in (nodes or sorted(node_set)) if n in node_set]

    out: List[AuditResult] = []
    for node in target:
        conds = _detect_feature_conditions(node, records, cdd_tx_by_node)
        log(f"[audit/feature] {node}: conditions {sorted(conds) or '(none)'}")
        for feat in sorted(feat_conds):
            gov = feat_conds[feat]
            active_conds = sorted(gov & conds)
            expect_active = bool(active_conds)
            fstate, lstate, fdesc = _feature_state(node, feat, records)
            mo = f"SystemFunctions=1,Lm=1,FeatureState={feat}"
            src = fdesc or "feature compliance"       # feature name (description)
            gov_label = _labels(gov)                  # config this feature needs
            ref = _labels(active_conds) if active_conds else gov_label
            if fstate is None:
                # MO absent: an issue only when the feature was expected active
                # (missing/unlicensed); when it should be off, absent == off = OK.
                # Baseline (broad LTE/NR/ESS) features whose MO isn't on the node
                # are NOT flagged — a missing broad feature is not actionable the
                # way a config-specific one is.
                if expect_active and feat not in baseline_feats:
                    out.append(AuditResult(
                        "feature", node, mo, "featureState",
                        f"ACTIVATED ({ref})",
                        "(FeatureState MO not found)", "NotFound",
                        src, node, ref_cell=ref))
                continue
            f_on, l_on = _is1(fstate), _is1(lstate)
            if expect_active:
                exp = f"ACTIVATED ({ref})"
                if f_on and l_on:
                    continue                      # OK → hidden (only issues shown)
                remarks = []
                if not f_on:
                    remarks.append("Feature Deactivated")
                if not l_on:
                    remarks.append("License Missing")
                act = (f"{'ACTIVATED' if f_on else 'DEACTIVATED'} / "
                       f"{'ENABLED' if l_on else 'DISABLED'} "
                       f"({'; '.join(remarks)})")
                status = "Mismatch"
            else:
                if not f_on:
                    continue                      # correctly deactivated → hidden
                # Remark names the config this feature belongs to, prefixed
                # "Non " because the node does NOT have it (e.g. "Non AAS TDD").
                reason = f"Non {gov_label}"
                exp = f"DEACTIVATED ({reason})"
                act = f"ACTIVATED (Should be Deactivated: {reason})"
                status = "Mismatch"
                ref = reason
            out.append(AuditResult(
                "feature", node, mo, "featureState", exp, act, status,
                src, node, ref_cell=ref))
    return out


def aggregate_trx(records: Dict[str, Dict[str, str]]) -> None:
    """Fold each ``GsmSector``'s ``Trx`` children (from a RadioNode dump) up
    onto the ``GsmSector`` record so the audit can read them without a live
    cmedit call: ``arfcnMin`` = the lowest Trx arfcnMin, ``arfcnMax`` = the
    highest, and ``__count__`` = the number of Trx (= the sector's TRX count).
    Mutates ``records`` in place; a no-op when the dump carries no Trx."""
    import collections
    trx_by_sector: dict = collections.defaultdict(list)
    for ldn, a in records.items():
        if ldn.split(",")[-1].split("=", 1)[0] != "Trx":
            continue
        m = re.search(r"GsmSector=([^,]+)", ldn)
        if m:
            trx_by_sector[m.group(1)].append(a)
    if not trx_by_sector:
        return

    def _ints(vals):
        out = []
        for v in vals:
            s = str(v).strip()
            if re.fullmatch(r"-?\d+", s):
                out.append(int(s))
        return out

    for ldn, a in records.items():
        leaf = ldn.split(",")[-1]
        if leaf.split("=", 1)[0] != "GsmSector":
            continue
        trx = trx_by_sector.get(leaf.split("=", 1)[-1])
        if not trx:
            continue
        mins = _ints(t.get("arfcnMin") for t in trx)
        maxs = _ints(t.get("arfcnMax") for t in trx)
        if mins:
            a["arfcnMin"] = str(min(mins))
        if maxs:
            a["arfcnMax"] = str(max(maxs))
        a["__count__"] = str(len(trx))


@dataclass
class CellInvRow:
    """One cell in the inventory comparison — CDD list vs the node's live MOs."""
    mo_class: str            # EUtranCellFDD | EUtranCellTDD | NRCellDU | GeranCell
    cell: str                # the cell name
    in_cdd: str              # "Yes" | "No"
    on_node: str             # "Yes" | "No"
    status: str              # Match | Missing (CDD-only) | Unplanned (node-only)


_CANON_CELL = {"eutrancellfdd": "EUtranCellFDD", "eutrancelltdd": "EUtranCellTDD",
               "nrcelldu": "NRCellDU", "nrcellcu": "NRCellCU",
               "gerancell": "GeranCell", "gsmsector": "GsmSector"}


def cell_inventory_rows(items: List[AuditItem],
                        records: Dict[str, Dict[str, str]]) -> List[CellInvRow]:
    """Per-cell inventory: for every cell MO class the CDD defines, list each
    cell name and whether it is in the CDD, on the node, or both — so the
    operator sees exactly which cells are missing or extra, not just a count."""
    import collections
    cdd = collections.defaultdict(set)      # class_lower -> {id_lower}
    actual = collections.defaultdict(set)
    disp: Dict[tuple, str] = {}             # (cls, id_lower) -> original name

    for it in items:
        if it.category != "cell":
            continue
        m = _CELL_MO_INV.search("," + it.mo_local)
        if not m:
            continue
        cls, cid = m.group(1).lower(), m.group(2)
        cdd[cls].add(cid.lower())
        disp[(cls, cid.lower())] = cid
    for ldn in records:
        m = _CELL_MO_INV.search(ldn)
        if not m:
            continue
        cls, cid = m.group(1).lower(), m.group(2)
        actual[cls].add(cid.lower())
        disp.setdefault((cls, cid.lower()), cid)

    rows: List[CellInvRow] = []
    for cls in sorted(cdd):                  # only classes the CDD defines
        name = _CANON_CELL.get(cls, cls)
        for cid in sorted(cdd[cls] | actual.get(cls, set())):
            in_cdd = cid in cdd[cls]
            on_node = cid in actual.get(cls, set())
            status = ("Match" if in_cdd and on_node
                      else "Missing" if in_cdd else "Unplanned")
            rows.append(CellInvRow(
                name, disp.get((cls, cid), cid),
                "Yes" if in_cdd else "No",
                "Yes" if on_node else "No", status))
    return rows


def cell_inventory_check(items: List[AuditItem],
                         records: Dict[str, Dict[str, str]]) -> List[AuditResult]:
    """Compare the SET of cells the CDD defines against the cells actually on
    the node(s), per cell MO class: the count and the exact names. Produces a
    'Cell count' row plus, when they differ, a 'Missing on node' / 'Extra on
    node' row so the operator sees which cell names don't line up."""
    import collections
    cdd = collections.defaultdict(set)      # class_lower -> {id_lower}
    actual = collections.defaultdict(set)
    disp: Dict[str, str] = {}               # id_lower -> original casing

    for it in items:
        if it.category != "cell":
            continue
        m = _CELL_MO_INV.search("," + it.mo_local)
        if not m:
            continue
        cls, cid = m.group(1).lower(), m.group(2)
        cdd[cls].add(cid.lower())
        disp[cid.lower()] = cid
    for ldn in records:
        m = _CELL_MO_INV.search(ldn)
        if not m:
            continue
        cls, cid = m.group(1).lower(), m.group(2)
        actual[cls].add(cid.lower())
        disp.setdefault(cid.lower(), cid)

    _CANON = {"eutrancellfdd": "EUtranCellFDD", "eutrancelltdd": "EUtranCellTDD",
              "nrcelldu": "NRCellDU", "nrcellcu": "NRCellCU",
              "gerancell": "GeranCell"}
    out: List[AuditResult] = []
    for cls in sorted(cdd):                  # only classes the CDD defines
        exp, act = cdd[cls], actual.get(cls, set())
        name = _CANON.get(cls, cls)
        missing = sorted(disp.get(x, x) for x in (exp - act))
        extra = sorted(disp.get(x, x) for x in (act - exp))
        ok = (len(exp) == len(act) and not missing and not extra)
        out.append(AuditResult(
            "cell-count", name, name, "cell count",
            str(len(exp)), str(len(act)),
            "Match" if ok else "Mismatch", "CDD vs node cells"))
        if missing:
            out.append(AuditResult(
                "cell-count", name, name, "missing on node",
                ", ".join(missing), "(not found)", "Mismatch",
                "in CDD, absent on node"))
        if extra:
            out.append(AuditResult(
                "cell-count", name, name, "extra on node",
                "(not in CDD)", ", ".join(extra), "Mismatch",
                "on node, absent from CDD"))
    return out


def _col_attrs(col: dict) -> List[str]:
    out = []
    if col.get("attr"):
        out.append(col["attr"].lower())
    for sp in col.get("split") or []:
        if sp.get("attr"):
            out.append(sp["attr"].lower())
    return out


def audited_attr_sets(audit_map: dict):
    """Derive the audited attribute universe for the *reverse* pass (node params
    with no CDD row). Returns ``(gsm_attrs, lte_class_attrs, ref_attrs)``:

      * gsm_attrs      — every GSM attribute (child-MO attrs are merged onto the
                         GeranCell record, so match by name alone).
      * lte_class_attrs— {(leaf MO class, attr)} for LTE/NR, so a short attr name
                         only matches on the right MO class.
      * ref_attrs      — via_ref/split attrs (on a referenced MO, e.g.
                         SectorCarrier); matched by name (they're distinctive).
    """
    gsm_attrs = set()
    lte_class_attrs = set()
    ref_attrs = set()
    for p in audit_map.get("profiles", []):
        cols = p.get("columns", [])
        if p.get("tech") == "gsm":
            for c in cols:
                gsm_attrs.update(_col_attrs(c))
            continue
        classes = set()
        leaf = p.get("mo_fdn", "").split(",")[-1].split("=")[0].strip()
        if leaf and "{" not in leaf:
            classes.add(leaf.lower())
        cmm = p.get("cell_mo_map") or {}
        for v in (cmm.get("map") or {}).values():
            classes.add(str(v).lower())
        if cmm.get("default"):
            classes.add(str(cmm["default"]).lower())
        for c in cols:
            cls = classes
            per_mo = c.get("mo")
            if per_mo:
                l = per_mo.split(",")[-1].split("=")[0].strip()
                if l and "{" not in l:
                    cls = {l.lower()}
            for a in _col_attrs(c):
                if c.get("via_ref"):
                    ref_attrs.add(a)
                else:
                    for cc in cls:
                        lte_class_attrs.add((cc, a))
    return gsm_attrs, lte_class_attrs, ref_attrs


def build_reverse_rows(node: str, records: Dict[str, Dict[str, str]],
                       gsm_attrs, lte_class_attrs, ref_attrs,
                       cell_rx=None) -> List[AuditResult]:
    """For a node with NO CDD rows, surface its ACTUAL audited parameters —
    CDD (expected) left blank, status ``CDD_missing``. LTE/NR MOs are matched by
    ``ManagedElement=<node>``; GSM GeranCells (BSC-level, no node in the FDN) by
    the site's cell-id regex."""
    out: List[AuditResult] = []
    nl = node.lower()
    for ldn, attrs in records.items():
        me = re.search(r"ManagedElement=([^,]+)", ldn)
        leaf_cls = ldn.split(",")[-1].split("=")[0].strip().lower()
        if me:
            if me.group(1).lower() != nl:
                continue
            local = re.sub(r"^.*?ManagedElement=[^,]+,?", "", ldn)
            is_gsm = False
        else:
            m = re.search(r"GeranCell=([^,]+)", ldn)
            if not (cell_rx is not None and m and cell_rx.match(m.group(1))):
                continue
            local = ldn
            is_gsm = True
        cell = _cell_of(local)
        for attr, val in attrs.items():
            if attr.startswith("__") or val is None or str(val).strip() == "":
                continue
            al = attr.lower()
            # ref_attrs (MIMO/tilt/power on a referenced MO) only count on a
            # *SectorCarrier — not on lookalike attrs of other MOs (e.g. an
            # ExternalEUtranCellFDD that also carries noOfTxAntennas).
            hit = (al in gsm_attrs) if is_gsm else (
                (leaf_cls, al) in lte_class_attrs
                or (al in ref_attrs and leaf_cls.endswith("sectorcarrier")))
            if hit:
                out.append(AuditResult(
                    "gsm" if is_gsm else "cell", cell or node, local, attr,
                    "", str(val), "CDD_missing", "node (no CDD)", node, cell))
    return out


# ── Excel report ────────────────────────────────────────────────────
_FILL_MISMATCH = PatternFill("solid", fgColor="FFC7CE")   # red
_FILL_NOTFOUND = PatternFill("solid", fgColor="FFEB9C")   # yellow
_FILL_MATCH = PatternFill("solid", fgColor="C6EFCE")      # green
_FILL_EXTRA = PatternFill("solid", fgColor="F8CBAD")      # orange (unplanned)
_FILL_CDDMISSING = PatternFill("solid", fgColor="E2CFF3")  # purple (node-only)
_FILL_HEADER = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

_STATUS_FILL = {"Mismatch": _FILL_MISMATCH, "NotFound": _FILL_NOTFOUND,
                "MO_NotFound": _FILL_NOTFOUND, "Match": _FILL_MATCH,
                "Unplanned": _FILL_EXTRA, "CDD_missing": _FILL_CDDMISSING}


_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER = Alignment(horizontal="center", vertical="center")
_LEFT = Alignment(horizontal="left", vertical="center")


def _write_summary(summ, results, meta, lld_results=None,
                   ess_rows=None, cell_rows=None):
    counts = {"Match": 0, "Mismatch": 0, "NotFound": 0, "MO_NotFound": 0,
              "CDD_missing": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results)
    # CDD-missing rows are node-only (no CDD to grade against) — kept out of the
    # graded total / compliance, shown as a separate informational KPI.
    graded = total - counts["CDD_missing"]

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
        share = f"{count / graded * 100:.1f}%" if (graded and pct) else ""
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

    kpi("Total checks", graded, "D9E1F2", "1F3864", pct=False)
    kpi("✔  Match", counts["Match"], "C6EFCE", "006100")
    kpi("✖  Mismatch", counts["Mismatch"], "FFC7CE", "9C0006")
    kpi("⚠  Parameter not found", counts["NotFound"], "FFEB9C", "9C6500")
    kpi("⚠  MO not found", counts["MO_NotFound"], "F8CBAD", "833C00")
    if counts["CDD_missing"]:
        kpi("● CDD missing (node-only)", counts["CDD_missing"],
            "E2CFF3", "5B2A86", pct=False)

    # ── Compliance headline ──────────────────────────────────────
    r += 1
    comp = counts["Match"] / graded * 100 if graded else 0.0
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

    # ── Coverage by section ──────────────────────────────────────
    # One row per output section so a section that produced NOTHING (e.g. NR
    # cells dropped, or the ESS sheet skipped) is visible at a glance instead of
    # silently absent. Zero-row sections are flagged amber.
    cat = {}
    for res in results:
        cat[res.category or "?"] = cat.get(res.category or "?", 0) + 1
    sections = [(k, cat[k]) for k in sorted(cat)]
    sections += [
        ("ESS pairs", len(ess_rows or [])),
        ("LLD checks", len(lld_results or [])),
        ("Cell inventory", len(cell_rows or [])),
    ]
    r += 2
    summ.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    h = summ.cell(r, 1, "Coverage by section")
    h.font = Font(bold=True, size=13, color="FFFFFF")
    h.fill = PatternFill("solid", fgColor="4472C4")
    h.alignment = _CENTER
    r += 1
    for c, txt in enumerate(("Section", "Rows", ""), 1):
        cc = summ.cell(r, c, txt)
        cc.font = Font(bold=True, color="FFFFFF")
        cc.fill = PatternFill("solid", fgColor="8EA9DB")
        cc.alignment = _CENTER
        cc.border = _BORDER
    r += 1
    for label, n in sections:
        a = summ.cell(r, 1, label)
        b = summ.cell(r, 2, n)
        note = summ.cell(r, 3, "empty" if n == 0 else "")
        for cell in (a, b, note):
            cell.border = _BORDER
            cell.alignment = _LEFT if cell is a else _CENTER
        if n == 0:
            for cell in (a, b, note):
                cell.fill = PatternFill("solid", fgColor="FFEB9C")
                cell.font = Font(bold=True, color="9C6500")
        r += 1

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

    # ── LLD (physical baseband / CPRI) block ─────────────────────
    if lld_results:
        lc = {"Match": 0, "Mismatch": 0, "NotFound": 0, "Unplanned": 0}
        for x in lld_results:
            lc[x.status] = lc.get(x.status, 0) + 1
        graded = lc["Match"] + lc["Mismatch"] + lc["NotFound"]
        r += 2
        summ.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        h = summ.cell(r, 1, "LLD — baseband & CPRI (see 'LLD' sheet)")
        h.font = Font(bold=True, size=13, color="FFFFFF")
        h.fill = PatternFill("solid", fgColor="4472C4")
        h.alignment = _CENTER
        r += 1
        rows = [
            ("Checks (graded)", graded, "D9E1F2", "1F3864", False),
            ("✔  Match", lc["Match"], "C6EFCE", "006100", True),
            ("✖  Mismatch", lc["Mismatch"], "FFC7CE", "9C0006", True),
            ("⚠  Not found", lc["NotFound"], "FFEB9C", "9C6500", True),
            ("➕ Unplanned (node-only)", lc["Unplanned"], "F8CBAD", "C55A11", False),
        ]
        for label, count, fill, fcol, pct in rows:
            share = f"{count / graded * 100:.1f}%" if (graded and pct) else ""
            a = summ.cell(r, 1, label)
            b = summ.cell(r, 2, count)
            c = summ.cell(r, 3, share)
            for cell in (a, b, c):
                cell.fill = PatternFill("solid", fgColor=fill)
                cell.font = Font(bold=True, color=fcol)
                cell.border = _BORDER
                cell.alignment = _CENTER
            a.alignment = _LEFT
            r += 1
        comp = lc["Match"] / graded * 100 if graded else 0.0
        g, w = comp >= 95, comp >= 80
        lc2 = summ.cell(r, 1, "LLD compliance")
        lc2.font = Font(bold=True, size=12, color="1F3864")
        lc2.alignment = _LEFT
        summ.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        vc = summ.cell(r, 2, f"{comp:.1f}%")
        vc.fill = PatternFill("solid", fgColor=(
            "C6EFCE" if g else ("FFEB9C" if w else "FFC7CE")))
        vc.font = Font(bold=True, size=12, color=(
            "006100" if g else ("9C6500" if w else "9C0006")))
        vc.alignment = _CENTER
        for cell in (lc2, vc):
            cell.border = _BORDER

    summ.column_dimensions["A"].width = 40
    summ.column_dimensions["B"].width = 24
    summ.column_dimensions["C"].width = 22


def _write_lld_sheet(ws, lld_results: List["LldResult"]) -> None:
    """Physical baseband / CPRI checks on their own sheet, with a two-row header
    grouping each metric into paired LLD | Node columns:

        Node │ BBID │ Sector/Radio │ BB RI Port │  HW Type  │ Radio DATA Port │ Status
                                     LLD │ Node   LLD │ Node    LLD │ Node

    One row per planned link (or the baseband unit). Status colour matches the
    Detail sheet (green/red/yellow) plus orange for unplanned (node-only) links.
    """
    # ── header (rows 1-2) ────────────────────────────────────────
    #  single (2-row-merged) columns, then 3 paired groups, then Status.
    ws.merge_cells("A1:A2"); ws["A1"] = "Node"
    ws.merge_cells("B1:B2"); ws["B1"] = "BBID"
    ws.merge_cells("C1:C2"); ws["C1"] = "Sector/Radio"
    ws.merge_cells("D1:E1"); ws["D1"] = "BB RI Port"
    ws.merge_cells("F1:G1"); ws["F1"] = "HW Type"
    ws.merge_cells("H1:I1"); ws["H1"] = "Radio DATA Port"
    ws.merge_cells("J1:J2"); ws["J1"] = "Status"
    subs = {4: "LLD", 5: "Node", 6: "LLD", 7: "Node", 8: "LLD", 9: "Node"}
    for c, txt in subs.items():
        ws.cell(2, c, txt)
    for row in (1, 2):
        for c in range(1, 11):
            cell = ws.cell(row, c)
            cell.fill = _FILL_HEADER
            cell.font = _HEADER_FONT
            cell.alignment = _CENTER
            cell.border = _BORDER

    status_col = 10

    def _sort_key(r):
        # baseband row (no port) first per node, then by planned/actual port
        port = r.bb_port_lld or r.bb_port_node
        return (r.node, r.bbid, port == "", port)

    for r in sorted(lld_results, key=_sort_key):
        ws.append([r.node, r.bbid, r.ref_cell,
                   r.bb_port_lld, r.bb_port_node,
                   r.hw_type_lld, r.hw_type_node,
                   r.data_port_lld, r.data_port_node, r.status])
        row = ws.max_row
        fill = _STATUS_FILL.get(r.status)
        if fill:
            ws.cell(row=row, column=status_col).fill = fill
        # Highlight the specific LLD|Node pair(s) that differ (yellow).
        for ok, cols in ((r.bb_ok, (4, 5)), (r.hw_ok, (6, 7)),
                         (r.data_ok, (8, 9))):
            if not ok:
                for c in cols:
                    ws.cell(row, c).fill = _FILL_NOTFOUND
        for c in range(1, 11):
            ws.cell(row, c).border = _BORDER

    ws.freeze_panes = "A3"
    widths = {"A": 34, "B": 6, "C": 12, "D": 8, "E": 8, "F": 20,
              "G": 22, "H": 10, "I": 10, "J": 10}
    for col, wdt in widths.items():
        ws.column_dimensions[col].width = wdt


def _write_ess_sheet(ws, ess_rows: list) -> None:
    """ESS (LTE/NR spectrum sharing) pairing on its own sheet — one row per
    CDD ESS pair, showing existence, essScLocalId/essScPairId on the LTE
    SectorCarrier and NR NRSectorCarrier (vs the CDD), and essEnabled on both
    relations. Green/red status like Detail; the specific failing cell is
    yellow-highlighted."""
    headers = ["Node", "LTE Cell", "NR Cell", "LTE Exists", "NR Exists",
               "LTE cellId (CDD)", "LTE cellId (node)", "essScLocalId (SC)",
               "NR cellLocalId (CDD)", "NR cellLocalId (node)",
               "essScLocalId (NRSC)",
               "essScPairId (CDD)", "essScPairId (SC)", "essScPairId (NRSC)",
               "essEnabled LTE", "essEnabled NR", "Status"]
    ws.append(headers)
    for c in ws[1]:
        c.fill = _FILL_HEADER
        c.font = _HEADER_FONT
        c.alignment = _CENTER
        c.border = _BORDER
    status_col = len(headers)
    for r in ess_rows:
        ws.append([
            r.node, r.lte_cell, r.nr_cell,
            "Yes" if r.lte_exists else "No",
            "Yes" if r.nr_exists else "No",
            r.lte_local_exp, r.lte_cellid, r.sc_local,
            r.nr_local_exp, r.nr_localid, r.nrsc_local,
            r.ess_pair, r.sc_pair, r.nrsc_pair,
            r.ess_lte, r.ess_nr, r.status])
        row = ws.max_row
        fill = _STATUS_FILL.get(r.status)
        if fill:
            ws.cell(row=row, column=status_col).fill = fill
        # Yellow-flag the specific failing check(s). The local-id check is
        # three-way: CDD == node cellId == node essScLocalId.
        if not r.lte_exists:
            ws.cell(row, 4).fill = _FILL_NOTFOUND
        if not r.nr_exists:
            ws.cell(row, 5).fill = _FILL_NOTFOUND
        if not (r.lte_local_exp == r.lte_cellid == r.sc_local):
            for c in (7, 8):
                ws.cell(row, c).fill = _FILL_NOTFOUND
        if not (r.nr_local_exp == r.nr_localid == r.nrsc_local):
            for c in (10, 11):
                ws.cell(row, c).fill = _FILL_NOTFOUND
        if not (r.sc_pair == r.ess_pair == r.nrsc_pair):
            for c in (13, 14):
                ws.cell(row, c).fill = _FILL_NOTFOUND
        if r.ess_lte != "true":
            ws.cell(row, 15).fill = _FILL_NOTFOUND
        if r.ess_nr != "true":
            ws.cell(row, 16).fill = _FILL_NOTFOUND
        for c in range(1, len(headers) + 1):
            ws.cell(row, c).border = _BORDER
    ws.freeze_panes = "A2"
    widths = {"A": 30, "B": 16, "C": 16, "D": 9, "E": 9, "F": 16, "G": 16,
              "H": 16, "I": 18, "J": 18, "K": 18, "L": 16, "M": 16, "N": 16,
              "O": 12, "P": 12, "Q": 10}
    for col, wdt in widths.items():
        ws.column_dimensions[col].width = wdt


_FILL_MISSING = PatternFill("solid", fgColor="FFC7CE")     # red-ish (missing)
_FILL_EXTRA2 = PatternFill("solid", fgColor="F8CBAD")      # orange (unplanned)


def _write_cell_inventory_sheet(ws, rows: List["CellInvRow"]) -> None:
    """A dedicated sheet listing every cell — CDD vs node — so it's obvious
    which cells are missing (in CDD, not on node) or extra (on node, not in
    CDD), per MO class."""
    headers = ["MO Class", "Cell", "In CDD", "On Node", "Status"]
    ws.append(headers)
    for c in ws[1]:
        c.fill = _FILL_HEADER
        c.font = _HEADER_FONT
        c.alignment = _CENTER
        c.border = _BORDER
    fill = {"Match": _FILL_MATCH, "Missing": _FILL_MISSING,
            "Unplanned": _FILL_EXTRA2}
    for r in sorted(rows, key=lambda x: (x.mo_class, x.status != "Match", x.cell)):
        ws.append([r.mo_class, r.cell, r.in_cdd, r.on_node, r.status])
        row = ws.max_row
        f = fill.get(r.status)
        if f:
            ws.cell(row, 5).fill = f
        for c in range(1, 6):
            ws.cell(row, c).border = _BORDER
    ws.freeze_panes = "A2"
    for col, w in (("A", 20), ("B", 34), ("C", 9), ("D", 9), ("E", 12)):
        ws.column_dimensions[col].width = w


def write_excel(results: List[AuditResult], out_path: str, meta: dict,
                lld_results: Optional[List["LldResult"]] = None,
                cell_rows: Optional[List["CellInvRow"]] = None,
                ess_rows: Optional[list] = None) -> str:
    wb = Workbook()

    # Summary sheet
    summ = wb.active
    summ.title = "Summary"
    _write_summary(summ, results, meta, lld_results, ess_rows, cell_rows)

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
        # Synthetic attrs (e.g. ``__count__`` for the TRX count) are cryptic in
        # the Parameter column — show the friendly CDD column from the source
        # ("GSM!No of UL TRX" → "No of UL TRX") instead.
        param = r.parameter
        if param.startswith("__") and "!" in (r.source or ""):
            param = r.source.split("!", 1)[1]
        ws.append([r.category, (r.node or r.key), r.ref_cell, r.mo, param,
                   r.expected, r.actual, r.status, r.source])
        row = ws.max_row
        fill = _STATUS_FILL.get(r.status)
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

    # LLD sheet (physical baseband / CPRI) — only when there are LLD checks.
    if lld_results:
        _write_lld_sheet(wb.create_sheet("LLD"), lld_results)

    # Cell inventory sheet — CDD cell list vs the node's live cells.
    if cell_rows:
        _write_cell_inventory_sheet(wb.create_sheet("Cell Inventory"), cell_rows)

    # ESS sheet — LTE/NR spectrum-sharing pairing.
    if ess_rows:
        _write_ess_sheet(wb.create_sheet("ESS"), ess_rows)

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


# Synthetic audit categories that compare an aggregate/derived value, not a
# single settable MO attribute — they have no valid ``set`` target, so every
# script generator skips them (a "GsmSector [BULUAN]" MO or an ESS pairing has
# no single attribute to set). ``ip-broker`` is NOT here: bscBrokerIpAddress is
# a real settable attribute on the node's AbisIp MO, so its Mismatch rows carry
# the full FDN + clean IP and ARE generated (see broker_check).
_NON_SETTABLE_CATEGORIES = {"trx-count", "ess", "etilt", "sw-level",
                            "consistency", "endc", "chain", "feature"}

# Categories excluded from cmedit/cmbulk but STILL settable via moshell (.mos) —
# e.g. antenna tilt is a RET operation done on the node, not an ENM cmedit set.
_MOSHELL_SETTABLE = {"etilt"}


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
        if (r.category in _NON_SETTABLE_CATEGORIES
                and r.category not in _MOSHELL_SETTABLE):
            continue
        if r.parameter.startswith("__"):
            continue          # synthetic (e.g. __count__) — not a settable attr
        # cmedit-sourced params (BSC GeranCell) can't be set via moshell — they
        # only go into the cmedit/cmbulk scripts, never the runnable .mos.
        if r.from_cmedit:
            continue
        node = r.node or r.key or site
        sig = (node, r.mo, r.parameter)
        if sig in seen:
            continue
        seen.add(sig)
        by_node.setdefault(node, {}).setdefault(_mo_group(r.mo), []).append(
            (r.mo, r.parameter, r.expected, r.norm, r.actual))

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
            for mo, param, val, norm, actual in sorted(
                    groups[g], key=lambda x: (x[1], x[0])):
                lines.append(
                    f"set {mo}$ {param} {_format_set_value(val, norm, actual)}")
        # Close the log opened with l+ at the top.
        lines.append("")
        lines.append("l-")
        path = os.path.join(out_dir, f"{node}_SetParameter_{stamp}.mos")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(path)
    return written


# ── ENM cmedit / cmbulk SetParameter script generation ──────────────
# Ported from C:\dev\enp-generator (services/mo_script_generator.py) — the
# CMEdit CLI and CM Bulk CLI ``set`` formats. These are written to SEPARATE
# files from the moshell/.mos script (different command syntax) and are NOT
# meant to be run by the in-app Run Scripts button — only the .mos is.


def _collect_set_rows(results: List[AuditResult], statuses, site: str):
    """node → ordered list of (mo, parameter, value, norm, actual) for the rows
    to align, de-duplicated on (node, mo, parameter). Shared by every generator
    so the three formats stay in lock-step. ``norm``/``actual`` let the value be
    re-formatted to the node's convention when a set line is emitted."""
    by_node: Dict[str, list] = {}
    seen = set()
    for r in results:
        if r.status not in statuses or r.expected == "":
            continue
        if r.category in _NON_SETTABLE_CATEGORIES:
            continue
        if r.parameter.startswith("__"):
            continue          # synthetic (e.g. __count__) — not a settable attr
        node = r.node or r.key or site
        sig = (node, r.mo, r.parameter)
        if sig in seen:
            continue
        seen.add(sig)
        by_node.setdefault(node, []).append(
            (r.mo, r.parameter, r.expected, r.norm, r.actual))
    return by_node


# Boolean/enum domains: a CDD ``1``/``0`` maps to the ON/OFF member of the same
# domain the node reports for that attribute (detected from the actual value).
_BOOL_DOMAINS = [("ON", "OFF"), ("ACTIVE", "INACTIVE"), ("ENABLED", "DISABLED"),
                 ("TRUE", "FALSE"), ("YES", "NO"), ("UNLOCKED", "LOCKED")]


def _dms_cdd_to_node(v: str) -> str:
    """CDD DMS coordinate (``8°3'35.1\"N``) → node form (``N08-03-35.1``):
    hemisphere first, degrees/minutes/seconds hyphen-joined and zero-padded."""
    m = _LATLON_A.search(str(v))
    if not m:
        return str(v)
    d, mn, sec, hemi = m.group(1), m.group(2), m.group(3), m.group(4).upper()
    if "." in sec:
        ip, fp = sec.split(".", 1)
        secf = f"{int(ip):02d}.{fp}"
    else:
        secf = f"{int(sec):02d}"
    return f"{hemi}{int(d):02d}-{int(mn):02d}-{secf}"


def _format_set_value(value, norm: str = "", actual: str = "") -> str:
    """Format a CDD value into the node's own convention for a ``set`` — value
    from the CDD, *format* from the node:

      * ``list``    ``0 1 2 3`` / ``4&12&17``  → ``[0, 1, 2, 3]`` / ``[4, 12, 17]``
      * ``latlong`` ``8°3'35.1\"N``            → ``N08-03-35.1``
      * ``geo``     ``6.999`` (decimal deg)    → ``6999000`` (µdeg integer)
      * boolean     ``1`` / ``0``              → ``ACTIVE`` / ``INACTIVE`` … per
                                                 the domain the node reports.
    """
    s = "" if value is None else str(value).strip()
    if s == "":
        return s
    if norm == "list":
        parts = [p for p in re.split(r"[\[\],&\s]+", s) if p]
        # Only bracketise a genuine multi-value list; a lone token (e.g. a
        # ChannelGroup dchNo of "OFF", or a single value) is left as-is.
        return "[" + ", ".join(parts) + "]" if len(parts) > 1 else s
    if norm == "latlong":
        return _dms_cdd_to_node(s)
    if norm == "geo":
        try:
            return str(int(round(float(s) * 1e6)))
        except (ValueError, TypeError):
            return s
    if norm == "tilt10":                       # CDD degrees → node 0.1° units
        try:
            return str(int(round(float(s) * 10)))
        except (ValueError, TypeError):
            return s
    if s in ("0", "1"):
        a = str(actual or "").strip().upper()
        for on, off in _BOOL_DOMAINS:
            if a in (on, off):
                return on if s == "1" else off
    if s.lower() in ("true", "false"):
        return s.lower()
    return s


def _enm_prefix(fdn_prefix: str = "") -> str:
    """Normalize the ENM SubNetwork prefix.

    The main form stores the short subnetwork value (for example ``T7``), while
    ENM set commands need the full ONRM-rooted FDN prefix. Already-expanded
    prefixes are preserved for callers/tests that pass a full FDN prefix.
    """
    prefix = (fdn_prefix or "").strip().strip(",")
    if not prefix:
        return ""
    if "=" in prefix:
        return prefix.rstrip(",")
    return f"SubNetwork=ONRM_ROOT_MO_R,SubNetwork={prefix}"


def _mo_below_managed_element(mo: str) -> str:
    """Return the FDN path below ManagedElement, even if ``mo`` is rooted."""
    parts = [p.strip() for p in (mo or "").strip().strip(",").split(",")
             if p.strip()]
    for i in range(len(parts) - 1, -1, -1):
        name = parts[i].split("=", 1)[0].strip().lower()
        if name == "managedelement":
            return ",".join(parts[i + 1:])
    return ",".join(parts)


def _enm_fdn(node: str, mo: str, fdn_prefix: str = "") -> str:
    """Full ENM FDN for a ``cmedit`` target:
    ``[<prefix>,]MeContext=<node>,ManagedElement=<node>[,<mo>]``. ``mo`` is
    normally below ManagedElement, but rooted values from parsed references are
    tolerated and normalized before the final FDN is assembled."""
    rel_mo = _mo_below_managed_element(mo)
    base = f"MeContext={node},ManagedElement={node}"
    if rel_mo:
        base = f"{base},{rel_mo}"
    prefix = _enm_prefix(fdn_prefix)
    return f"{prefix},{base}" if prefix else base


# GSM profiles whose ``cmedit_mo`` is NOT a child MO under GeranCell: the base
# cell (attrs live directly on GeranCell) and Trx (on the RadioNode, not the BSC).
_GSM_NON_CHILD = {"gerancell", "trx"}


def build_gsm_child_map(audit_map: dict) -> Dict[str, str]:
    """parameter (lower) → GSM child-MO class, from the audit map's GSM
    profiles. GSM child-MO attributes are merged onto the GeranCell record for
    the compare, which loses the child-MO identity — this recovers it so the
    cmedit/cmbulk FDN can append the child segment (``…,IdleModeAndPaging=1``)."""
    m: Dict[str, str] = {}
    for p in audit_map.get("profiles", []):
        if p.get("tech") != "gsm":
            continue
        child = (p.get("cmedit_mo") or "").strip()
        if not child or child.lower() in _GSM_NON_CHILD:
            continue
        for c in p.get("columns", []):
            a = (c.get("attr") or "").strip()
            if a:
                m.setdefault(a.lower(), child)
    return m


def build_bsc_by_cell(records: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """geranCellId → BSC name, harvested from the live cmedit source records
    (each ``GeranCell=<id>`` record carries the ``__bsc__`` = NodeId column).
    Lets generated cmedit/cmbulk FDNs use the REAL BSC the cell lives on rather
    than a placeholder — the primary BSC source, ahead of the CDD/form."""
    out: Dict[str, str] = {}
    for ldn, attrs in records.items():
        m = re.match(r"GeranCell=([^,]+)", ldn)
        if not m:
            continue
        bsc = (attrs.get("__bsc__") or "").strip()
        if bsc:
            out.setdefault(m.group(1), bsc)
    return out


def _build_fdn(node: str, mo: str, param: str = "", fdn_prefix: str = "",
               gsm_fdn_prefix: str = "",
               gsm_child_map: Optional[Dict[str, str]] = None,
               bsc_by_cell: Optional[Dict[str, str]] = None,
               default_bsc: str = "") -> str:
    """Pick the right FDN shape for a target MO. GSM ``GeranCell`` MOs live on
    the BSC (not the audited RadioNode) with a fixed BscFunction/GeranCellM
    lineage, so they use the configurable ``gsm_fdn_prefix`` template — the
    ``{bsc}`` placeholder is filled per cell, preferring the real BSC from the
    cmedit source (``bsc_by_cell``), then ``default_bsc`` (CDD/form). When the
    parameter belongs to a child MO, the child segment is appended
    (``…,GeranCell=<id>,<Child>=1``). Every other MO uses the node-rooted
    ``MeContext=<node>,ManagedElement=<node>`` FDN built from the dump path."""
    top = mo.split("=", 1)[0].strip()
    if gsm_fdn_prefix and top == "GeranCell":
        # Append the child segment only for a bare ``GeranCell=<id>``; when the
        # mo already carries a child (e.g. ChannelGroup=0 from a per-index CDD
        # column) trust that real instance instead of forcing ``=1``.
        leaf = mo
        cid_m = re.match(r"GeranCell=([^,]+)", mo)
        cid = cid_m.group(1) if cid_m else ""
        if "," not in mo:
            child = (gsm_child_map or {}).get((param or "").lower())
            if child:
                leaf = f"{mo},{child}=1"
        bsc = ((bsc_by_cell or {}).get(cid) or default_bsc or "<BSC>")
        prefix = gsm_fdn_prefix.replace("{bsc}", bsc)
        return f"{prefix.rstrip(',')},{leaf}"
    return _enm_fdn(node, mo, fdn_prefix)


def _enm_value(v) -> str:
    """Format a value for ENM set: booleans lower-cased, everything else passed
    through (structs already rendered as ``{a=..,b=..}`` are kept verbatim)."""
    s = "" if v is None else str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower()
    return s


def _enm_header(fmt: str, generated_by: str, stamp: str, audit_xlsx: str):
    return [
        "# " + "-" * 60,
        f"# Generate by: {generated_by or 'NodeCraft'}",
        f"# Format: {fmt}",
        f"# Datetime: {stamp}",
        f"# Audit File: {os.path.basename(audit_xlsx)}",
        "# Sheet Name: Detail",
        "# NOTE: review before applying - not run by the in-app Run button.",
        "# NOTE: singleton GSM child MOs use index =1 (ChannelGroup uses its"
        " real index).",
        "# " + "-" * 60,
        "",
    ]


def generate_cmedit_scripts(results: List[AuditResult], out_dir: str,
                            site: str, audit_xlsx: str, generated_by: str = "",
                            statuses=("Mismatch",),
                            fdn_prefix: str = "",
                            gsm_fdn_prefix: str = "",
                            gsm_child_map: Optional[Dict[str, str]] = None,
                            bsc_by_cell: Optional[Dict[str, str]] = None,
                            default_bsc: str = ""
                            ) -> List[str]:
    """One CMEdit CLI file per node: ``cmedit set <FDN> <param>=<value>``
    (one command per parameter). Returns the files written."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = _stamp()
    written: List[str] = []
    for node, rows in _collect_set_rows(results, statuses, site).items():
        # No comment header — the ENM CMEdit CLI errors on ``#`` lines, and the
        # file is run per-line via cli.py, so keep it commands-only.
        lines: List[str] = []
        for mo, param, val, norm, actual in sorted(rows, key=lambda x: (x[1], x[0])):
            fdn = _build_fdn(node, mo, param, fdn_prefix, gsm_fdn_prefix,
                             gsm_child_map, bsc_by_cell, default_bsc)
            lines.append(
                f"cmedit set {fdn} {param}={_format_set_value(val, norm, actual)}")
        path = os.path.join(out_dir, f"{node}_SetParameter_{stamp}_cmedit.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(path)
    return written


def build_cmedit_commands(results: List[AuditResult], site: str,
                          statuses=("Mismatch",), fdn_prefix: str = "",
                          gsm_fdn_prefix: str = "",
                          gsm_child_map: Optional[Dict[str, str]] = None,
                          bsc_by_cell: Optional[Dict[str, str]] = None,
                          default_bsc: str = ""
                          ) -> List[tuple]:
    """The exact ``cmedit set <FDN> <param>=<value>`` commands for the rows to
    align — same FDN/value logic as the CMEdit file export, but returned as
    ``(node, command)`` tuples so the app can apply them live over SSH."""
    cmds: List[tuple] = []
    for node, rows in _collect_set_rows(results, statuses, site).items():
        for mo, param, val, norm, actual in sorted(rows, key=lambda x: (x[1], x[0])):
            fdn = _build_fdn(node, mo, param, fdn_prefix, gsm_fdn_prefix,
                             gsm_child_map, bsc_by_cell, default_bsc)
            cmds.append(
                (node, f"cmedit set {fdn} {param}={_format_set_value(val, norm, actual)}"))
    return cmds


def generate_cmbulk_scripts(results: List[AuditResult], out_dir: str,
                            site: str, audit_xlsx: str, generated_by: str = "",
                            statuses=("Mismatch",),
                            fdn_prefix: str = "",
                            gsm_fdn_prefix: str = "",
                            gsm_child_map: Optional[Dict[str, str]] = None,
                            bsc_by_cell: Optional[Dict[str, str]] = None,
                            default_bsc: str = ""
                            ) -> List[str]:
    """One CM Bulk CLI file per node, parameters grouped per FDN into one block::

        set
        FDN : <FDN>
        param1 : value1
        param2 : value2

    Grouping is by the full FDN, so GSM child MOs (each with its own
    ``…,<Child>=1`` FDN) get their own block rather than being folded under the
    GeranCell."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = _stamp()
    written: List[str] = []
    for node, rows in _collect_set_rows(results, statuses, site).items():
        # group by the computed FDN, preserving first-seen order
        fdn_params: Dict[str, list] = {}
        for mo, param, val, norm, actual in rows:
            fdn = _build_fdn(node, mo, param, fdn_prefix, gsm_fdn_prefix,
                             gsm_child_map, bsc_by_cell, default_bsc)
            fdn_params.setdefault(fdn, []).append(
                (param, _format_set_value(val, norm, actual)))
        # No comment header — the CM Bulk importer errors on ``#`` lines.
        lines: List[str] = []
        for fdn, params in fdn_params.items():
            lines.append("set")
            lines.append(f"FDN : {fdn}")
            for param, v in sorted(params, key=lambda x: x[0]):
                if v.startswith("{") and v.endswith("}"):
                    lines.append(f"{param}={v}")
                else:
                    lines.append(f"{param} : {v}")
            lines.append("")
        path = os.path.join(out_dir, f"{node}_SetParameter_{stamp}_cmbulk.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(path)
    return written


def _stamp() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
