"""Node power capacity audit using the supplied licensepower.pl formula."""
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .audit_core import AuditResult


def number(value):
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def display(value):
    return format(value, "f") if value is not None else "(unavailable)"


def audit_power_license(records, nodes=None):
    """Return report rows and per-MO evidence; no license set is generated.

    All configuredMaxTxPower attributes count once per owning full DN,
    including GSM Trx, LTE SectorCarrier and NR NRSectorCarrier.
    """
    grouped = defaultdict(dict)
    for dn, attrs in records.items():
        match = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if match:
            grouped[match[1]][dn[match.end():]] = attrs
    results, evidence = [], []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        values, grants, problems = [], [], []
        for mo, attrs in mos.items():
            attrs = {k.lower(): v for k, v in attrs.items()}
            if "configuredmaxtxpower" in attrs:
                raw = attrs["configuredmaxtxpower"]
                value = number(raw)
                values.append(value)
                evidence.append((node, mo, str(raw), value))
                if value is None:
                    problems.append(f"invalid configuredMaxTxPower: {mo}")
            elif mo.split(",")[-1].split("=")[0] in ("SectorCarrier", "NRSectorCarrier", "Trx"):
                problems.append(f"configuredMaxTxPower missing: {mo}")
            if re.search(r"(?:^|,)Lm=1,CapacityState=CXC4012338$", mo):
                grants.append(number(attrs.get("grantedcapacitylevel")))
        if not values:
            problems.append("no configuredMaxTxPower evidence")
        if len(grants) != 1 or grants[0] is None:
            problems.append("CXC4012338 grantedCapacityLevel missing/invalid/ambiguous")
        total = sum((v for v in values if v is not None), Decimal(0))
        required = total / Decimal(20000) if values and not problems else None
        granted = grants[0] if len(grants) == 1 else None
        delta = granted - required if granted is not None and required is not None else None
        source = (f"licensepower.pl: sum(configuredMaxTxPower)/20000; "
                  f"{len(values)} power MOs; total={display(total)}; "
                  f"required={display(required)}; granted={display(granted)}; "
                  f"delta={display(delta)}")
        if problems:
            source += "; " + "; ".join(problems)
        status = "NotFound" if problems else ("Match" if delta >= 0 else "Mismatch")
        results.append(AuditResult(
            "power-license", node, "SystemFunctions=1,Lm=1,CapacityState=CXC4012338",
            "Power license capacity (required vs granted)", display(required),
            display(granted), status, source, node))
    return results, evidence


def write_power_sheets(wb, results, evidence):
    summary = wb.create_sheet("Power License")
    summary.append(["Node", "Required capacity", "Granted capacity", "Delta", "Status", "Calculation / evidence"])
    for row in results:
        required, granted = number(row.expected), number(row.actual)
        delta = granted - required if required is not None and granted is not None else None
        summary.append([row.node, float(required) if required is not None else None,
                        float(granted) if granted is not None else None,
                        float(delta) if delta is not None else None, row.status, row.source])
    detail = wb.create_sheet("Power MO Detail")
    detail.append(["Node", "MO", "configuredMaxTxPower (raw)", "Power / 20000", "Data status"])
    for node, mo, raw, value in evidence:
        detail.append([node, mo, raw, float(value / 20000) if value is not None else None,
                       "Valid" if value is not None else "Invalid"])
    from openpyxl.styles import Font, PatternFill
    for sheet in (summary, detail):
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(
                80, max(12, max(len(str(c.value or "")) for c in column) + 2))
        if sheet is summary:
            for cells in sheet.iter_rows(min_row=2):
                cells[4].fill = PatternFill("solid", fgColor={
                    "Match": "C6EFCE", "Mismatch": "FFC7CE", "NotFound": "FFEB9C"}[cells[4].value])
