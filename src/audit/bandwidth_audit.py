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


def _nr_bands(value):
    # Ignore array-count markers: i[0] = is an empty list, not band zero.
    text = re.sub(r'(?:i)?\[\d+\]\s*=?', '', str(value or ''))
    return {int(b) for b in re.findall(r'\d+', text)}


def audit_bandwidth_license(records, nodes=None):
    grouped = defaultdict(dict)
    for dn, attrs in records.items():
        m = re.search(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)', dn)
        if m:
            grouped[m[1]][dn[m.end():]] = attrs
    results = []
    for node in sorted(set(nodes) if nodes is not None else grouped):
        mos = grouped.get(node, {})
        classes = {mo.split(',')[-1].split('=')[0] for mo in mos}
        has_lte = bool(classes & {'EUtranCellFDD', 'EUtranCellTDD'})
        has_nr = bool(classes & {'NRCellDU', 'NRSectorCarrier'})
        if not has_lte and not has_nr:
            continue
        totals = {k: Decimal(0) for k in RULES}
        errors = defaultdict(list)
        fallback_notes = defaultdict(set)
        if has_nr and 'NRSectorCarrier' not in classes:
            for key in ('2290', '2283', '2284', '2321', '2322'):
                errors[key].append('NR cells present but NRSectorCarrier evidence missing')

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
            sef_ref = attr(carrier, 'sectorEquipmentFunctionRef' if nr else 'sectorFunctionRef')
            sef = resolve(sef_ref)
            if sef is None:
                return None, set(), f'Reference path incomplete: SectorCarrier -> SectorEquipmentFunction [{sef_ref or "reference missing"}]'
            refs = str(attr(sef, 'rfBranchRef')).split(';')
            kinds = set()
            fallback_frus = set()
            for ref in refs:
                if 'FieldReplaceableUnit=' in ref:
                    radio_ref = ref
                else:
                    branch = resolve(ref)
                    if branch is None:
                        return None, set(), f'Reference path incomplete: SectorCarrier -> SectorEquipmentFunction [{sef_ref}] -> RfBranch [{ref or "reference missing"}]'
                    radio_ref = attr(branch, 'rfPortRef') or ref
                fru_ref = re.sub(r',(?:RfPort|Transceiver)=.*$', '', str(radio_ref))
                fru = resolve(fru_ref)
                if fru is None:
                    return None, set(), f'Reference path incomplete: SectorCarrier -> SectorEquipmentFunction [{sef_ref}] -> RfBranch [{ref}] -> FieldReplaceableUnit [{fru_ref or "reference missing"}]'
                product = str(attr(fru, 'productData.productName') or attr(fru, 'productName')).strip()
                if not product:
                    match = re.search(r'productName=([^,}]+)', str(attr(fru, 'productData')))
                    product = match[1].strip() if match else ''
                match = re.search(r'(?:^|,)FieldReplaceableUnit=([^,]+)$', fru_ref)
                name = match[1] if match else ''
                # Operator convention is authoritative: AAS FRUs also have
                # an RRU suffix, so check AAS before RRU.
                if re.search(r'(?:^|[^A-Z0-9])AAS(?:$|[^A-Z0-9])', name.upper()):
                    kinds.add(True)
                    fallback_frus.add(name)
                elif re.search(r'(?:^|[^A-Z0-9])RRU\d*(?:$|[^A-Z0-9])', name.upper()):
                    kinds.add(False)
                    fallback_frus.add(name)
                elif product.upper().startswith('AIR '):
                    kinds.add(True)
                elif product.upper().startswith('RADIO '):
                    kinds.add(False)
                else:
                    return None, set(), f'Radio type cannot be identified: FieldReplaceableUnit={name}; AAS/RRU naming and radio product unavailable or unrecognized'
            if len(kinds) != 1:
                return None, set(), 'Unexpected mixed AAS/non-AAS radio references; verify the carrier reference path'
            return kinds.pop(), fallback_frus, ''

        def add(key, mo, bw, divisor):
            totals[key] += bw / Decimal(divisor)

        for cell_mo, cell_attrs in mos.items():
            if not cell_mo.split(',')[-1].startswith('NRCellDU='):
                continue
            refs = str(attr(cell_attrs, 'nRSectorCarrierRef')).split(';')
            if any(not re.search(r'(?:^|,)NRSectorCarrier=[^,]+$', ref.strip())
                   or resolve(ref) is None for ref in refs):
                for key in ('2290', '2283', '2284', '2321', '2322'):
                    errors[key].append(f'{cell_mo}: NRCellDU is not linked to a valid NRSectorCarrier; '
                                       f'nRSectorCarrierRef=[{attr(cell_attrs, "nRSectorCarrierRef") or "reference missing"}]')

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
                bands = set()
                for ca in cells:
                    bands.update(_nr_bands(attr(ca, 'bandList')) or _nr_bands(attr(ca, 'bandListManual')))
                if cells and not bands:
                    # Last resort: established N41_S1 / N28_S1 carrier naming.
                    name = mo.split('NRSectorCarrier=', 1)[-1]
                    match = re.fullmatch(r'N(\d+)(?:_.*)?', name, re.IGNORECASE)
                    if match:
                        bands = {int(match[1])}
                        for key in ('2290', '2283', '2284', '2321', '2322'):
                            fallback_notes[key].add('NR band from NRSectorCarrier=' + name)
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
                reasons = []
                if nr and not cells:
                    reasons.append('NRCellDU is not linked to this NRSectorCarrier; check nRSectorCarrierRef')
                if bw is None or bw <= 0:
                    reasons.append('Bandwidth DL missing or invalid; verify dump completeness')
                if ul is None or ul != bw:
                    reasons.append('Bandwidth UL missing/invalid or differs from DL; verify dump values')
                if tdd is None and (not nr or cells):
                    reasons.append('NR duplex cannot be identified from bandList, bandListManual or NRSectorCarrier name; band unsupported or inconsistent')
                for k in affected:
                    errors[k].append(f'{mo}: ' + '; '.join(reasons))
                continue
            add('2290' if nr else '1622', mo, bw, 10 if tdd else 5)
            classification = aas(carrier, nr) if carrier is not None else (None, set(),
                f'Reference path incomplete: Cell [{mo}] -> SectorCarrier [{attr(a, "sectorCarrierRef") or "reference missing"}]')
            is_aas, fallback_frus, reference_error = classification
            if is_aas is None:
                for k in (['2283', '2284', '2321', '2322'] if nr else ['2367', '2203' if tdd else '2217']):
                    errors[k].append(f'{mo}: {reference_error}')
            else:
                key = ('2283' if tdd else '2284') if nr and is_aas else ('2322' if tdd else '2321') if nr else ('2203' if tdd else '2217') if is_aas else '2367'
                fallback_notes[key].update(fallback_frus)
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
                    errors['2411'].append(f'{mo}: ESS configuration incomplete/inconsistent (essEnabled versus essScPairId); review the ESS audit')
        for key, label in RULES.items():
            if key in ('1622', '2367', '2203', '2217') and not has_lte:
                continue
            if key in ('2290', '2283', '2284', '2321', '2322') and not has_nr:
                continue
            # ESS belongs to the LTE anchor and may target NR on another BB.
            # Do not require local NR, but skip when no sharing is configured.
            if key == '2411' and not (has_lte and (totals[key] or errors[key])):
                continue
            hits = [a for mo, a in mos.items() if mo.endswith('CapacityState=CXC401' + key)]
            granted = number(attr(hits[0], 'grantedCapacityLevel')) if len(hits) == 1 else None
            problems = errors[key]
            if granted is None:
                problems = problems + ['grantedCapacityLevel unavailable/invalid']
            required = totals[key]
            status = 'NotFound' if problems else 'Match' if granted >= required else 'Mismatch'
            source = 'CapacityState=CXC401' + key
            if problems:
                remark = '; '.join(problems)
            elif granted < required:
                remark = f'Insufficient bandwidth license; shortage {display(required - granted)} capacity units.'
            elif required == 0:
                remark = 'No applicable configured carriers.'
            else:
                remark = 'Bandwidth license capacity is sufficient.'
            if fallback_notes[key]:
                remark += ' Classification based on naming: ' + ', '.join(sorted(fallback_notes[key])) + '.'
            results.append(AuditResult('bandwidth-license', node,
                'SystemFunctions=1,Lm=1,CapacityState=CXC401' + key, label,
                '(unverified)' if errors[key] else display(required), display(granted),
                status, source, node, remark=remark))
    return results
