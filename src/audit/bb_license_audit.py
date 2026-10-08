"""
bb_license_audit.py — baseband type on the LKF vs the baseband actually fitted.

The HWAC licence keys under ``NodeSupport=1,CapacityUsage=1`` carry the hardware
the licence was issued for. Each ``hupInfo`` entry has a ``totInstalled`` string
built of ``;``-separated parts::

    totInstalled="IP:BB6621:1:1;"              -> hardware id BB6621
    totInstalled="IP:RANP6672:1:4;EP::2:6;"    -> hardware id RANP6672
    totInstalled="0"                           -> nothing installed

Only ``IP:`` parts name hardware; ``EP::`` parts are pool entries with no id. So
the licence says ``BB6621`` / ``RANP6672`` while the fitted unit reports its
``productName`` in ``Equipment=1,FieldReplaceableUnit=BB-1`` ("Baseband 6621",
"RAN Processor 6672"). They are compared on the 4-digit product number via
``normalize_bbtype`` - the same comparator the LLD audit uses.

Checked across the 67 node dumps in this repo: 48 matched, 2 differed
(MIN570_…B02 and MIN823_…B03: licence BB6621, fitted Baseband 6631) and 17 had
no hardware id at all because every ``totInstalled`` was ``0`` - those are
reported as unresolved ("LKF not installed or not detected"), never as a
mismatch.

Dump note: ``hupInfo`` is a read-only attribute that the **cmdump does not
export** (0 occurrences in the XML), so the licence side needs a **modump**. The
fitted ``productName`` is in both.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List

from .audit_core import AuditResult, normalize_bbtype

CATEGORY = "bb-license"
PARAMETER = "BB type (HWAC LKF vs installed)"
NOT_INSTALLED = ("possible LKF not installed or not detect. please verify")


def licence_hw_ids(hup_info: str) -> List[str]:
    """Hardware ids named by the HWAC licences, in order, without duplicates."""
    out: List[str] = []
    for installed in re.findall(r"totInstalled\s*=\s*([^,}]*)", str(hup_info or "")):
        for part in installed.split(";"):
            fields = part.split(":")
            if len(fields) >= 2 and fields[0].strip().upper() == "IP":
                hw = fields[1].strip()
                if hw and hw not in out:
                    out.append(hw)
    return out


def _product_name(attrs: Dict[str, str]) -> str:
    """Fitted unit name from a flattened ``productData.productName`` (cmdump) or
    out of the raw ``productData`` struct string (modump)."""
    name = attrs.get("productData.productName") or attrs.get("productName")
    if name:
        return str(name).strip()
    match = re.search(r"productName\s*=\s*([^,}]+)", str(attrs.get("productData") or ""))
    return match.group(1).strip() if match else ""


def _by_node(records: Dict[str, Dict[str, str]]):
    grouped: Dict[str, Dict[str, Dict[str, str]]] = defaultdict(dict)
    for dn, attrs in records.items():
        match = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if match:
            grouped[match[1]][dn[match.end():].lstrip(",")] = attrs
    return grouped


def audit_bb_license(records, nodes=None) -> List[AuditResult]:
    """One row per node: expected = the baseband actually fitted (FRU BB-1),
    actual = what the LKF was issued for (CapacityUsage)."""
    grouped = _by_node(records)
    results: List[AuditResult] = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        cap_mo = next((mo for mo in mos if re.search(r"(?:^|,)CapacityUsage=[^,]+$", mo)), "")
        fru_mo = next((mo for mo in mos if mo.endswith("FieldReplaceableUnit=BB-1")), "")
        if not cap_mo and not fru_mo:
            continue                      # not a baseband node in this dump

        installed = _product_name(mos.get(fru_mo, {})) if fru_mo else ""
        hup_info = mos.get(cap_mo, {}).get("hupInfo", "") if cap_mo else ""
        hw_ids = licence_hw_ids(hup_info)
        licence = " | ".join(hw_ids)
        source = cap_mo or "NodeSupport=1,CapacityUsage=1"

        if not installed:
            status = "MO_NotFound"
            remark = ("FieldReplaceableUnit=BB-1 productName not found in the dump, "
                      "so the installed baseband is unknown.")
        elif not hup_info:
            # Either the MO is absent, or it is a cmdump - which exports the MO
            # but none of its read-only licence attributes. Saying "LKF not
            # installed" here would be wrong: nothing was read at all.
            status = "NotFound"
            remark = (f"No licence data in this dump - hupInfo is read-only and a "
                      f"cmdump does not export it, so run this check on the modump. "
                      f"Actual BB installed is {installed}.")
        elif not hw_ids:
            status = "NotFound"
            remark = (f"{NOT_INSTALLED}. Actual BB installed is {installed}.")
        else:
            same = {normalize_bbtype(hw) for hw in hw_ids} == {normalize_bbtype(installed)}
            status = "Match" if same else "Mismatch"
            remark = (f"LKF value is {licence}, actual BB installed is {installed}.")

        results.append(AuditResult(
            CATEGORY, node, cap_mo or fru_mo, PARAMETER,
            installed, licence, status, source, node, remark=remark))
    return results
