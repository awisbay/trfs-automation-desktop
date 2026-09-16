"""Read-only, co-located LTE anchor -> NR X2 address audit.

Uses the shared cmdump/modump records, never a global local-DN lookup.
Neighbouring sites' TermPoints must not inherit this site's NR address.
"""
import ipaddress
import re
from collections import defaultdict

from .audit_core import AuditResult


def audit_termpoint_to_gnb(records, nodes=None):
    by_node = defaultdict(dict)
    for dn, attrs in records.items():
        match = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if match:
            by_node[match[1]][dn[match.end():]] = attrs

    def attr(attrs, key):
        return next((str(v).strip() for k, v in attrs.items()
                     if k.lower() == key.lower() and v is not None), "")

    def identity(value):
        return str(int(value)) if value.isdecimal() else value

    def ipv4(value):
        try:
            # Preserve host bits (including a valid .0 host on a /31).
            parsed = ipaddress.IPv4Interface(value).ip
            return "" if parsed.is_unspecified else str(parsed)
        except ValueError:
            return ""

    targets = defaultdict(list)
    for node, mos in by_node.items():
        if not any(re.search(r"(?:^|,)(?:GNBDUFunction|NRCellDU|NRCellCU)=", mo)
                   for mo in mos):
            continue
        ids = {identity(attr(a, "gNBId")) for mo, a in mos.items()
               if mo.split(",")[-1].split("=")[0] in
               ("GNBDUFunction", "GNBCUCPFunction")
               and attr(a, "gNBId") not in ("", "-1")}
        ips = {ipv4(attr(a, "address")) for mo, a in mos.items()
               if re.search(r"(?:^|,)Router=NR,InterfaceIPv4=X2_CP_UP,AddressIPv4=1$", mo)}
        ips.discard("")
        targets[node.split("_", 1)[0].upper()].append((node, ids, ips))

    results = []
    for anchor in sorted(by_node):
        if nodes is not None and anchor not in nodes:
            continue
        mos = by_node[anchor]
        if not any(re.search(r"(?:^|,)(?:ENodeBFunction|EUtranCellFDD|EUtranCellTDD|TermPointToGNB)=", mo)
                   for mo in mos):
            continue
        peers = targets.get(anchor.split("_", 1)[0].upper(), [])
        assigned = defaultdict(list)
        for mo, attrs in mos.items():
            if not mo.split(",")[-1].startswith("TermPointToGNB="):
                continue
            parent = mo.rsplit(",", 1)[0]
            parent_id = parent.split(",")[-1].split("=", 1)[-1]
            term_id = mo.split(",")[-1].split("=", 1)[-1]
            gnb_id = identity(attr(mos.get(parent, {}), "gNodeBId"))
            candidates = [p for p in peers if
                          gnb_id and gnb_id in p[1]]
            if not candidates:
                candidates = [p for p in peers if p[0].lower() in
                              (parent_id.lower(), term_id.lower())]
            for peer in candidates:
                assigned[peer[0]].append((mo, attrs, len(candidates) > 1))

        for target, ids, ips in peers:
            expected = next(iter(ips)) if len(ips) == 1 else ""
            source = (f"{target}: Router=NR,InterfaceIPv4=X2_CP_UP,AddressIPv4=1.address "
                      "(subnet removed)")
            links = assigned[target]
            if not links:
                results.append(AuditResult(
                    "termpoint-gnb", anchor, "TermPointToGNB", "ipAddress",
                    expected or "(NR X2 address unavailable)",
                    "No identifiable co-located target TermPoint in dump",
                    "MO_NotFound", source, anchor, ref_cell=target))
            for mo, attrs, ambiguous in links:
                actual = attr(attrs, "ipAddress")
                if ambiguous or not expected:
                    status = "NotFound"
                    source += ("; ambiguous target identity" if ambiguous else
                               "; missing/invalid/ambiguous NR X2 address")
                else:
                    status = "Match" if ipv4(actual) == expected else "Mismatch"
                results.append(AuditResult(
                    "termpoint-gnb", anchor, mo, "ipAddress",
                    expected or "(NR X2 address unavailable)", actual or "(empty)",
                    status, source, anchor, ref_cell=target))
    return results
