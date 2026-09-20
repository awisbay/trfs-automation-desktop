"""Power license demand per physical radio, including its initial free 20 W."""
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

    Each configuredMaxTxPower (mW) counts once per owning MO. Resolve LTE/NR
    carriers and GSM TRX to physical FRUs, sum per radio, then subtract one
    20-W capacity unit per radio (minimum zero). Never subtract per carrier.
    Ambiguous topology/allocation is unresolved, not estimated by MO naming.
    """
    grouped = defaultdict(dict)
    for dn, attrs in records.items():
        match = re.search(r"(?:^|,)ManagedElement=([^,]+)(?:,|$)", dn)
        if match:
            grouped[match[1]][dn[match.end():]] = attrs
    indexed = {node.casefold(): {mo.casefold(): (mo, attrs) for mo, attrs in mos.items()}
               for node, mos in grouped.items()}

    def attr(attrs, key):
        return next((v for k, v in attrs.items() if k.casefold() == key.casefold()), '')

    def refs(value):
        value = re.sub(r'^(?:i)?\[\d+\]\s*=?\s*', '', str(value or '').strip())
        return [ref for ref in re.split(r'[;\s]+', value) if ref]

    def resolve(ref, node):
        owner = re.search(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)', ref)
        target = owner[1] if owner else node
        local = re.sub(r'^.*?ManagedElement=[^,]+,?', '', ref).strip()
        candidates = indexed.get(target.casefold(), {})
        hit = candidates.get(local.casefold())
        if hit is None:
            hits = [record for dn, record in candidates.items()
                    if dn.endswith(',' + local.casefold()) and local]
            hit = hits[0] if len(hits) == 1 else None
        if hit is None:
            raise ValueError(f'reference missing or ambiguous: {ref or "(empty)"}')
        return target, hit[0], hit[1]

    def radio_from_ref(ref, node):
        if not re.search(r'(?:^|,)FieldReplaceableUnit=', ref, re.I):
            target, branch_mo, branch = resolve(ref, node)
            port_refs = refs(attr(branch, 'rfPortRef'))
            if len(port_refs) != 1:
                raise ValueError(f'{branch_mo}: rfPortRef missing or ambiguous')
            ref, node = port_refs[0], target
        match = re.search(r'^(.*?(?:^|,)FieldReplaceableUnit=[^,;\s]+)', ref, re.I)
        if not match:
            raise ValueError(f'physical radio FRU cannot be identified: {ref}')
        target, fru_mo, fru = resolve(match[1], node)
        serial = attr(fru, 'productData.serialNumber') or attr(fru, 'serialNumber')
        if not serial:
            serial_match = re.search(r'serialNumber\s*=\s*([^,}]+)', str(attr(fru, 'productData')), re.I)
            serial = serial_match[1].strip() if serial_match else ''
        serial = str(serial).strip()
        full_dn = f'ManagedElement={target},{fru_mo}'
        # Serial identity also deduplicates aliases of a shared physical radio.
        key = ('serial:' + serial.upper() if serial.casefold() not in
               {'', 'null', 'none', 'n/a', 'unknown', '0', '-1'} else full_dn.casefold())
        return key, full_dn

    def radios_for_mo(mo, attrs, node):
        sef_refs = refs(attr(attrs, 'sectorFunctionRef') or attr(attrs, 'sectorEquipmentFunctionRef'))
        if not sef_refs:
            raise ValueError('SectorEquipmentFunction reference missing')
        radios = {}
        for sef_ref in sef_refs:
            target, sef_mo, sef = resolve(sef_ref, node)
            branch_refs = refs(attr(sef, 'rfBranchRef'))
            if not branch_refs:
                raise ValueError(f'{sef_mo}: rfBranchRef missing')
            for branch_ref in branch_refs:
                key, full_dn = radio_from_ref(branch_ref, target)
                radios[key] = full_dn
        if not radios:
            raise ValueError('MO has no resolved physical radio')
        return list(radios.items())

    evidence, calculations = [], {}
    # A shared radio receives its initial package independently on each BB.
    for node in sorted(set(grouped) | set(nodes or [])):
        mos = grouped.get(node, {})
        radios, grants, problems = {}, [], []
        power_mos = 0
        for mo, attrs in mos.items():
            attrs = {k.lower(): v for k, v in attrs.items()}
            if "configuredmaxtxpower" in attrs:
                power_mos += 1
                raw = attrs["configuredmaxtxpower"]
                value = number(raw)
                evidence.append((node, mo, str(raw), value))
                if value is None:
                    problems.append(f"invalid configuredMaxTxPower: {mo}")
                    continue
                if mo.split(',')[-1].split('=')[0] not in ('SectorCarrier', 'NRSectorCarrier', 'Trx'):
                    problems.append(f'{mo}: unsupported power MO; physical radio allocation unavailable')
                    continue
                try:
                    mapped_radios = radios_for_mo(mo, attrs, node)
                except ValueError as exc:
                    problems.append(f'{mo}: {exc}')
                    continue
                # configuredMaxTxPower belongs to the carrier/TRX. When its RF
                # branches terminate on multiple physical FRUs, that total is
                # distributed evenly over those radios. Counting the full
                # carrier power on every FRU would duplicate demand; rejecting
                # it made valid multi-radio sectors unverifiable.
                share = value / Decimal(len(mapped_radios))
                for key, radio_dn in mapped_radios:
                    radio = radios.setdefault(
                        key, {'dn': radio_dn, 'power': Decimal(0)})
                    radio['power'] += share
            elif mo.split(",")[-1].split("=")[0] in ("SectorCarrier", "NRSectorCarrier", "Trx"):
                problems.append(f"configuredMaxTxPower missing: {mo}")
            if re.search(r"(?:^|,)Lm=1,CapacityState=CXC4012338$", mo):
                grants.append(number(attrs.get("grantedcapacitylevel")))
        if not power_mos:
            problems.append("no configuredMaxTxPower evidence")
        if len(grants) != 1 or grants[0] is None:
            problems.append("CXC4012338 grantedCapacityLevel missing/invalid/ambiguous")
        calculations[node] = (radios, grants, problems)

    results = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        radios, grants, problems = calculations[node]
        demands = [max(Decimal(0), radio['power'] / Decimal(20000) - 1) for radio in radios.values()]
        required = sum(demands, Decimal(0)) if radios and not problems else None
        granted = grants[0] if len(grants) == 1 else None
        delta = granted - required if granted is not None and required is not None else None
        status = "NotFound" if problems else ("Match" if delta >= 0 else "Mismatch")
        source = "CapacityState=CXC4012338; per-radio power / 20 W minus initial package"
        remark = ("; ".join(problems) if problems else
                  f"Insufficient power license; shortage {display(-delta)} capacity units."
                  if delta < 0 else "Power license capacity is sufficient.")
        if radios:
            breakdown = [f"{radio['dn']}: {display(radio['power'] / 1000)} W -> "
                         f"{display(max(Decimal(0), radio['power'] / Decimal(20000) - 1))} units"
                         for radio in radios.values()]
            remark += '\nRadio calculation (one free 20 W package per radio per BB):\n' + '\n'.join(breakdown)
        results.append(AuditResult(
            "power-license", node, "SystemFunctions=1,Lm=1,CapacityState=CXC4012338",
            "Power license capacity (required vs granted)", display(required),
            display(granted), status, source, node, remark=remark))
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
    detail.append(["Node", "MO", "configuredMaxTxPower (mW)", "Gross power / 20 W (before radio credit)", "Data status"])
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
