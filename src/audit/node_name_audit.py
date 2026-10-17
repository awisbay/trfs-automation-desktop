"""
node_name_audit.py — one node name everywhere.

A node's ``ManagedElement.networkManagedElementId`` (dump) must equal the node
name each CDD gives it:

    LTE CDD  eNodeBName   (row linked to the node by ENodeBID == eNBId)
    NR  CDD  gNodeBName   (row linked to the node by gNodeBID == gNBId)
    GSM CDD  gsmNodeName  (rows of the site, when the node carries GSM)

Only the RATs the node actually carries are checked, so a mixed GSM+LTE node
needs networkManagedElementId == eNodeBName == gsmNodeName. Rows are linked by
their IDs, not by name, so a CDD that carries a *different* name for the node
is still found — and reported as the mismatch.

Everything comes from what the audit already read: the dump records and the
CDD items (the LTE/NR node profiles carry ENodeBID/gNodeBID with the row's node
name, every GSM item carries its gsmNodeName).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set

from .audit_core import AuditResult

CATEGORY = "node-name"
PARAMETER = "networkManagedElementId"


def _by_node(records: Dict[str, Dict[str, str]]):
    grouped: Dict[str, Dict[str, Dict[str, str]]] = defaultdict(dict)
    for dn, attrs in records.items():
        m = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if m:
            grouped[m.group(1)][dn[m.end():].lstrip(",")] = attrs
    return grouped


def _first(mos: Dict[str, Dict[str, str]], pattern: str, attr: str) -> str:
    for mo, attrs in mos.items():
        if re.search(pattern, mo) and str(attrs.get(attr) or "").strip():
            return str(attrs[attr]).strip()
    return ""


def _has(mos, pattern: str) -> bool:
    return any(re.search(pattern, mo) for mo in mos)


def _site(node: str) -> str:
    return node.split("_", 1)[0]


def _names(items, tech: str, param: str, id_value: str) -> Set[str]:
    return {str(it.node).strip() for it in items
            if it.tech == tech and it.parameter == param
            and str(it.expected).strip() == id_value and str(it.node).strip()}


def audit_node_name(records: Dict[str, Dict[str, str]], items: Iterable,
                    nodes: Optional[Iterable[str]] = None) -> List[AuditResult]:
    items = list(items)
    grouped = _by_node(records)
    gsm_by_site: Dict[str, Set[str]] = defaultdict(set)
    for it in items:
        if it.tech == "gsm" and str(it.node or "").strip():
            name = str(it.node).strip()
            gsm_by_site[_site(name).upper()].add(name)

    out: List[AuditResult] = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        if not mos:
            continue
        me = mos.get("", {})
        nme = str(me.get("networkManagedElementId") or "").strip()

        sources = []        # (label, name or "")
        notes = []
        has_lte = _has(mos, r"(?:^|,)EUtranCell(?:FDD|TDD)=")
        has_nr = _has(mos, r"(?:^|,)NRCell(?:DU|CU)=")
        has_gsm = _has(mos, r"(?:^|,)(?:GsmSector|BtsFunction)=")

        if has_lte:
            enb = _first(mos, r"(?:^|,)ENodeBFunction=[^,]+$", "eNBId")
            names = _names(items, "lte_nr", "eNBId", enb) if enb else set()
            if not names:
                names = {str(it.node).strip() for it in items
                         if it.tech == "lte_nr" and str(it.node).strip().upper() == node.upper()}
            if names:
                for n in sorted(names):
                    sources.append(("eNodeBName (LTE CDD%s)" % (
                        ", ENodeBID %s" % enb if enb else ""), n))
            else:
                notes.append("no LTE CDD row for ENodeBID %s" % (enb or "?"))
        if has_nr:
            gnb = _first(mos, r"(?:^|,)GNBDUFunction=[^,]+$", "gNBId")
            names = _names(items, "nr", "gNBId", gnb) if gnb else set()
            if not names:
                names = {str(it.node).strip() for it in items
                         if it.tech == "nr" and str(it.node).strip().upper() == node.upper()}
            if names:
                for n in sorted(names):
                    sources.append(("gNodeBName (NR CDD%s)" % (
                        ", gNodeBID %s" % gnb if gnb else ""), n))
            else:
                notes.append("no NR CDD row for gNodeBID %s" % (gnb or "?"))
        if has_gsm:
            names = gsm_by_site.get(_site(node).upper(), set())
            # several GSM nodes at the site: the one named like this node wins
            if node in names:
                names = {node}
            if names:
                for n in sorted(names):
                    sources.append(("gsmNodeName (GSM CDD)", n))
            else:
                notes.append("no GSM CDD row for site %s" % _site(node))

        if not (has_lte or has_nr or has_gsm):
            continue
        # CDD (expected) column = the names the CDDs give; node (actual) =
        # the dump's networkManagedElementId.
        cdd_names = " | ".join("%s=%s" % (lbl.split(" (")[0], n) for lbl, n in sources)
        if not nme:
            status = "NotFound"
            remark = ("networkManagedElementId not found in the dump; CDD: "
                      + ("; ".join("%s = %s" % s for s in sources) or "-"))
        elif not sources:
            status = "NotFound"
            remark = "; ".join(notes)
        else:
            diff = [(lbl, n) for lbl, n in sources if n != nme]
            status = "Mismatch" if diff else "Match"
            if diff:
                remark = ("networkManagedElementId = %s; differs: %s"
                          % (nme, "; ".join("%s = %s" % d for d in diff)))
            else:
                remark = "All equal: networkManagedElementId, " + ", ".join(
                    lbl.split(" (")[0] for lbl, _n in sources)
            if notes:
                remark += "; " + "; ".join(notes)
        out.append(AuditResult(
            CATEGORY, node, "ManagedElement=%s" % node, PARAMETER,
            cdd_names, nme, status, "dump + LTE/NR/GSM CDD", node, remark=remark))
    return out
