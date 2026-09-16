"""Bandwidth capacity estimates with explicit topology and missing-data checks."""
import re
from collections import defaultdict
from decimal import Decimal

from .audit_core import AuditResult
from .power_audit import number, display

RULES = {
    '1622': 'LTE 5MHz Sector Carriers',
    '2367': 'LTE non-AAS Channel Bandwidth 5MHz',
    '2203': 'LTE AAS TDD Channel Bandwidth 10MHz',
    '2217': 'LTE AAS FDD Channel Bandwidth 10MHz',
    '2283': 'NR AAS TDD Channel Bandwidth 10MHz',
    '2284': 'NR AAS FDD Channel Bandwidth 10MHz',
    '2290': 'NR 5+5MHz Sector Carrier',
    '2321': 'NR non-AAS FDD Channel Bandwidth 5MHz',
    '2322': 'NR non-AAS TDD Channel Bandwidth 5MHz',
    '2411': 'LTE-NR FDD Spectrum Sharing Enabler',
}


def audit_bandwidth_license(records, nodes=None):
    grouped = defaultdict(dict)
    for dn, attrs in records.items():
        m = re.search(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)', dn)
        if m:
            grouped[m[1]][dn[m.end():]] = attrs
    results = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        if not any(mo.split(',')[-1].split('=')[0] in
                   ('EUtranCellFDD', 'EUtranCellTDD', 'NRSectorCarrier') for mo in mos):
            continue
        totals = {k: Decimal(0) for k in RULES}
        evidence = defaultdict(list)
        errors = defaultdict(list)

        def attr(a, key):
            return next((v for k, v in a.items() if k.lower() == key.lower()), '')

        def resolve(ref):
            ref = re.sub(r'^\[\d+\]\s*=\s*', '', str(ref)).strip()
            owner = re.search(r'ManagedElement=([^,]+)', ref)
            target = grouped.get(owner[1], {}) if owner else mos
            local = re.sub(r'^.*?ManagedElement=[^,]+,', '', ref)
            if local in target:
                return target[local]
            hits = [a for mo, a in target.items() if mo == local or mo.endswith(',' + local)]
            return hits[0] if len(hits) == 1 else None

        def aas(carrier, nr=False):
            sef = resolve(attr(carrier, 'sectorEquipmentFunctionRef' if nr else 'sectorFunctionRef'))
            if sef is None:
                return None
            refs = str(attr(sef, 'rfBranchRef')).split(';')
            kinds = set()
            for ref in refs:
                if 'FieldReplaceableUnit=' in ref:
                    radio_ref = ref
                else:
                    branch = resolve(ref)
                    if branch is None:
                        return None
                    radio_ref = attr(branch, 'rfPortRef') or ref
                fru_ref = re.sub(r',(?:RfPort|Transceiver)=.*$', '', str(radio_ref))
                fru = resolve(fru_ref)
                if fru is None:
                    return None
                product = str(attr(fru, 'productData.productName') or attr(fru, 'productName'))
                if not product:
                    match = re.search(r'productName=([^,}]+)', str(attr(fru, 'productData')))
                    product = match[1] if match else ''
                if product.startswith('AIR '):
                    kinds.add(True)
                elif product.startswith('Radio '):
                    kinds.add(False)
                else:
                    return None
            return kinds.pop() if len(kinds) == 1 else None

        def add(key, mo, bw, divisor):
            totals[key] += bw / Decimal(divisor)
            evidence[key].append(f'{mo}: {display(bw)}MHz/{divisor}')

        for mo, a in mos.items():
            cls = mo.split(',')[-1].split('=')[0]
            if cls not in ('EUtranCellFDD', 'EUtranCellTDD', 'NRSectorCarrier'):
                continue
            nr = cls == 'NRSectorCarrier'
            if nr:
                bw = number(attr(a, 'bSChannelBwDL'))
                ul = number(attr(a, 'bSChannelBwUL'))
                carrier = a
                cells = [ca for cm, ca in mos.items() if cm.split(',')[-1].startswith('NRCellDU=')
                         and any(resolve(ref) is a for ref in str(attr(ca, 'nRSectorCarrierRef')).split(';'))]
                bands = {int(b) for ca in cells for b in re.findall(r'\d+', re.sub(r'^i\[\d+\]\s*=\s*', '', str(attr(ca, 'bandList'))))}
                # Supported duplex classifications; unsupported bands remain unverified.
                tdd_bands = {38, 40, 41, 48, 77, 78, 79, 90}
                fdd_bands = {1, 2, 3, 5, 7, 8, 12, 13, 14, 18, 20, 25, 26, 28, 66, 68, 71}
                tdd = True if bands and bands <= tdd_bands else False if bands and bands <= fdd_bands else None
                affected = ['2290', '2283', '2284', '2321', '2322']
            else:
                tdd = cls == 'EUtranCellTDD'
                bw = number(attr(a, 'channelBandwidth' if tdd else 'dlChannelBandwidth'))
                ul = bw if tdd else number(attr(a, 'ulChannelBandwidth'))
                bw = bw / 1000 if bw is not None else None
                ul = ul / 1000 if ul is not None else None
                carrier = resolve(attr(a, 'sectorCarrierRef'))
                affected = ['1622', '2367', '2203' if tdd else '2217', '2411']
            if bw is None or bw <= 0 or tdd is None or ul != bw:
                for k in affected:
                    errors[k].append(f'{mo}: missing/invalid bandwidth, unknown duplex, or asymmetric DL/UL')
                continue
            add('2290' if nr else '1622', mo, bw, 10 if tdd else 5)
            is_aas = aas(carrier, nr) if carrier is not None else None
            if is_aas is None:
                for k in (['2283', '2284', '2321', '2322'] if nr else ['2367', '2203' if tdd else '2217']):
                    errors[k].append(f'{mo}: radio product/topology unavailable')
            else:
                key = ('2283' if tdd else '2284') if nr and is_aas else ('2322' if tdd else '2321') if nr else ('2203' if tdd else '2217') if is_aas else '2367'
                # NR TDD non-AAS uses 5MHz BW units (sample nodes MIN1802,
                # MIN3745, MIN3785 and MIN5134); AAS uses 10MHz units.
                add(key, mo, bw, 10 if is_aas else 5)
            if not nr and not tdd:
                relations = [ra for rm, ra in mos.items() if rm.startswith(mo + ',')
                             and rm.split(',')[-1].startswith('GUtranCellRelation=')]
                shared = any(str(attr(ra, 'essEnabled')).lower() == 'true' for ra in relations)
                pair = str(attr(carrier or {}, 'essScPairId'))
                if shared and pair not in ('', '0'):
                    add('2411', mo, bw, 5)
                elif shared or pair not in ('', '0'):
                    errors['2411'].append(f'{mo}: incomplete ESS evidence')
        for key, label in RULES.items():
            hits = [a for mo, a in mos.items() if mo.endswith('CapacityState=CXC401' + key)]
            granted = number(attr(hits[0], 'grantedCapacityLevel')) if len(hits) == 1 else None
            problems = errors[key]
            if granted is None:
                problems = problems + ['grantedCapacityLevel unavailable/invalid']
            required = totals[key]
            status = 'NotFound' if problems else 'Match' if granted >= required else 'Mismatch'
            source = '; '.join(evidence[key] + problems) or 'No applicable configured carriers'
            results.append(AuditResult('bandwidth-license', node,
                'SystemFunctions=1,Lm=1,CapacityState=CXC401' + key, label,
                '(unverified)' if errors[key] else display(required), display(granted),
                status, source, node))
    return results
