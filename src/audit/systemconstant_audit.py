"""Audit SystemConstant 4631 in a selected modump's rnclog.txt."""
from __future__ import annotations

import os
import re
import zipfile

from .audit_core import AuditResult


_COMMAND = re.compile(r"/cm/sysconread\s+all\b", re.I)
_VALUE = re.compile(r"^\s*(?:\d+:\s*)?\[default\]\s+4631:(\d+)\s*$", re.I)
_TECH = re.compile(r"^\s*(?:\d+:\s*)?\[(LTE|NR)\]\s*$", re.I)
_OWNER = re.compile(r"ManagedElement=([^,\]\s]+)", re.I)


def _read_rnclog(path):
    if not path or not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist()
                   if os.path.basename(name).casefold() == "rnclog.txt"]
        if not members:
            return None
        return archive.read(sorted(members)[0]).decode("utf-8", "replace")


def audit_systemconstant(records, nodes, dump_evidence):
    """Return one node-level result; GSM-only nodes are explicitly N/A."""
    paths = {row["node"].casefold(): row["path"] for row in dump_evidence}
    results = []
    for node in dict.fromkeys(nodes):
        node_records = [dn for dn in records
                        if re.search(r"(?:^|,)ManagedElement=" + re.escape(node) + r"(?:,|$)", dn, re.I)]
        has_lte = any(re.search(r"(?:^|,)EUtranCell(?:FDD|TDD)=", dn, re.I)
                      for dn in node_records)
        has_nr = any(re.search(r"(?:^|,)NRCell(?:DU|CU)=", dn, re.I)
                     for dn in node_records)
        if not (has_lte or has_nr):
            results.append(AuditResult(
                category="systemconstant", key=node, mo="[GSM]",
                parameter="SystemConstant 4631", expected="N/A", actual="N/A",
                status="Match", source="Technology applicability",
                node=node, remark="Not Required - GSM-only node",
            ))
            continue
        path = paths.get(node.casefold(), "")
        try:
            log_text = _read_rnclog(path)
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            log_text = None
        found = []
        if log_text is not None:
            active = False
            value = None
            tech = None
            owner = None

            def commit():
                if (tech in ("LTE", "NR") and value is not None
                        and (owner is None or owner.casefold() == node.casefold())):
                    found.append((tech, value))

            for line in log_text.splitlines():
                if _COMMAND.search(line):
                    commit()
                    active, value, tech, owner = True, None, None, None
                    continue
                if not active:
                    continue
                if re.match(r"^={10,}\s*$", line):
                    commit()
                    active, value, tech, owner = False, None, None, None
                    continue
                if _VALUE.match(line):
                    value = _VALUE.match(line).group(1)
                tech_match = _TECH.match(line)
                if tech_match:
                    tech = tech_match.group(1).upper()
                owner_match = _OWNER.search(line)
                if owner_match:
                    owner = owner_match.group(1)
            commit()
        cmdump = bool(path) and "cmdump" in os.path.basename(path).casefold()
        values = [value for _, value in found]
        actual = (", ".join(f"{tech} 4631:{value}" for tech, value in found)
                  if found else "N/A" if cmdump else "(not found)")
        results.append(AuditResult(
            category="systemconstant", key=node, mo="[LTE/NR]",
            parameter="SystemConstant 4631", expected="N/A" if cmdump else "4631:1",
            actual=actual, status="Match" if cmdump or (values and all(v == "1" for v in values)) else "Mismatch",
            source=f"{os.path.basename(path) or 'dump'} / rnclog.txt",
            node=node,
            remark=("N/A use cmdump" if cmdump else
                    "/cm/sysconread all; correction: scw 4631:1"),
        ))
    return results
