"""Per-node GSM TRX capacity checks; installed keys are not additive."""
import re
from collections import defaultdict

from .audit_core import AuditResult
from .power_audit import number, display


LICENSES = ("CXC4012021", "CXC4012037")


def audit_trx_license(records, nodes=None):
    grouped = defaultdict(dict)
    for dn, attrs in records.items():
        match = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if match:
            grouped[match[1]][dn[match.end():]] = attrs
    results = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        trx = [mo for mo in mos if re.search(
            r"(?:^|,)BtsFunction=[^,]+,GsmSector=[^,]+,Trx=[^,]+$", mo)]
        if not trx and not any(re.search(r"(?:^|,)GsmSector=[^,]+$", mo) for mo in mos):
            continue
        for code in LICENSES:
            states, keys = [], []
            for mo, attrs in mos.items():
                state = re.search(rf"(?:^|,)Lm=1,CapacityState={code}$", mo)
                key = re.search(rf"(?:^|,)Lm=1,CapacityKey={code}(?:_[^,]+)?$", mo)
                if state or key:
                    grant = number({k.lower(): v for k, v in attrs.items()}.get("grantedcapacitylevel"))
                    (states if state else keys).append((mo, grant))
            # CapacityState is the effective capacity, not CapacityKey + State.
            candidates = states if states else keys
            valid = len(candidates) == 1 and candidates[0][1] is not None
            granted = candidates[0][1] if valid else None
            source = candidates[0][0] if len(candidates) == 1 else f"Lm=1,CapacityState={code}"
            if not valid:
                status = "NotFound"
                remark = "Granted TRX capacity missing, invalid or ambiguous; capacity keys are not summed."
            elif granted >= len(trx):
                status = "Match"
                remark = f"TRX license is sufficient for {len(trx)} defined GSM TRX MOs."
            else:
                status = "Mismatch"
                remark = f"Insufficient TRX license; shortage {display(len(trx) - granted)} TRX."
            results.append(AuditResult(
                "trx-license", node, source, f"GSM TRX capacity ({code})",
                str(len(trx)), display(granted), status, source, node, remark=remark))
    return results
