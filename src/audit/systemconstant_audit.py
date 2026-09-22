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
    """Return one result for each LTE/NR technology actually present per node."""
    paths = {row["node"].casefold(): row["path"] for row in dump_evidence}
    results = []
    for node in dict.fromkeys(nodes):
        node_records = [dn for dn in records
                        if re.search(r"(?:^|,)ManagedElement=" + re.escape(node) + r"(?:,|$)", dn, re.I)]
        techs = []
        if any(re.search(r"(?:^|,)(?:ENodeBFunction|EUtranCell(?:FDD|TDD))=", dn, re.I)
               for dn in node_records):
            techs.append("LTE")
        if any(re.search(r"(?:^|,)(?:GNBDUFunction|GNBCUCPFunction|GNBCUUPFunction|NRCellDU)=", dn, re.I)
               for dn in node_records):
            techs.append("NR")
        if not techs:
            continue
        path = paths.get(node.casefold(), "")
        try:
            log_text = _read_rnclog(path)
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            log_text = None
        found = {tech: [] for tech in techs}
        if log_text is not None:
            active = False
            value = None
            tech = None
            owner = None

            def commit():
                if tech in found and value is not None and (owner is None or owner.casefold() == node.casefold()):
                    found[tech].append(value)

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
        for tech in techs:
            values = found[tech]
            cmdump = bool(path) and "cmdump" in os.path.basename(path).casefold()
            actual = (", ".join(f"4631:{v}" for v in values) if values else
                      "N/A" if cmdump else "(not found)")
            results.append(AuditResult(
                category="systemconstant", key=node, mo=f"[{tech}]",
                parameter="SystemConstant 4631", expected="4631:1", actual=actual,
                status="Match" if cmdump or "1" in values else "Mismatch",
                source=f"{os.path.basename(path) or 'dump'} / rnclog.txt",
                node=node,
                remark=("N/A use cmdump" if cmdump else
                        "/cm/sysconread all; correction: scw 4631:1"),
            ))
    return results
