#!/usr/bin/env python3
"""
Convert NBR Relation Scripts data files (txt) to XML format.
Uses Relation.txt as template and source data from ZIP file.

Template sections are marked by comments like:
  <!-- 13_Eutran_CellRelation Start -->
  ...
  <!-- 13_Eutran_CellRelation End -->

Each section number+name maps to a source data file like:
  01_External_GNodeBFunction_<SITENAME>.txt

Usage:
  python txt_to_xml_converter.py --site MIN149_SANMIGUELTAGUMDDNB01
  python txt_to_xml_converter.py --list-sites
"""

import zipfile
import os
import re
import sys
import argparse
import tempfile


def parse_crn_line(crn_line):
    """Parse a CRN line into a dict of key=value pairs."""
    result = {}
    content = crn_line.strip()
    if content.startswith('crn '):
        content = content[4:]
    for pair in content.split(','):
        if '=' in pair:
            key, value = pair.split('=', 1)
            result[key.strip()] = value.strip()
    return result


def parse_data_file(filepath):
    """Parse a source data file into a list of record dicts.
    
    Each record has:
      _crn: dict of CRN key=value pairs
      _type: the most specific key in the CRN
      _fields: dict of key=value pairs after the CRN line
    """
    records = []
    current_record = None

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                if current_record and current_record.get('_crn'):
                    records.append(current_record)
                current_record = None
                continue

            if line.startswith('crn '):
                if current_record and current_record.get('_crn'):
                    records.append(current_record)
                crn_data = parse_crn_line(line)
                current_record = {'_crn': crn_data, '_fields': {}}
                for key in reversed(list(crn_data.keys())):
                    if key not in ('ENodeBFunction', 'ManagedElement', 'eNodeBFunctionId',
                                'EUtraNetwork', 'GUtraNetwork', 'GNBCUCPFunction',
                                'NRNetwork', 'GeraNetwork', 'EUtraNetwork'):
                        current_record['_type'] = key
                        break
            elif current_record is not None:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    current_record['_fields'][parts[0]] = parts[1]
                elif len(parts) == 1:
                    current_record['_fields'][parts[0]] = ''

        if current_record and current_record.get('_crn'):
            records.append(current_record)

    return records


def crn_val(record, key):
    """Get a value from a record's CRN line."""
    return record.get('_crn', {}).get(key, '')


def field_val(record, key):
    """Get a value from a record's fields."""
    return record.get('_fields', {}).get(key, '')


def extract_from_ref(ref_str, ref_key):
    """Extract a specific key value from a reference string."""
    for pair in ref_str.split(','):
        if '=' in pair:
            key, value = pair.split('=', 1)
            if key.strip() == ref_key:
                return value.strip()
    return ''


def resolve_placeholder(placeholder, record, section_name, all_data, nodename):
    """Resolve a $VARIABLE placeholder using source data and section context."""
    p = placeholder

    if p == '$NODENAME':
        return nodename

    # === Section 01: External_GNodeBFunction ===
    if p == '$NODEUSERLABEL':
        for key in ['ExternalGNodeBFunction', 'externalGNodeBFunctionId',
                     'ExternalENodeBFunction']:
            val = crn_val(record, key)
            if val:
                return val
        # Check fields
        val = field_val(record, 'externalGNodeBFunctionId')
        if val:
            return val

    if p == '$NODEID':
        for key in ['gNodeBId', 'eNBId']:
            val = field_val(record, key)
            if val:
                return val

    if p == '$CUPINIPV4':
        val = field_val(record, 'ipAddress')
        if val:
            return val

    # === Section 02: External_GutraCell ===
    if p == '$NREXTERNALCELLID':
        for key in ['ExternalGUtranCell']:
            val = crn_val(record, key)
            if val:
                return val

    if p == '$GUTRANSYNCFREQID':
        ref = field_val(record, 'gUtranSyncSignalFrequencyRef')
        if ref:
            match = re.search(r'GUtranSyncSignalFrequency=(\S+)', ref)
            if match:
                return match.group(1)

    if p == '$PCIGNRCIQ':
        return field_val(record, 'physicalLayerCellIdGroup')
    if p == '$PSCINRCIQ':
        return field_val(record, 'physicalLayerSubCellId')
    if p == '$CELLIDNRCIQ':
        return field_val(record, 'localCellId')

    # === Section 03: GUtran_CellRelation ===
    if section_name == '03_GUtran_CellRelation':
        if p == '$CELLNAMELTECIQ':
            for key in ['EUtranCellFDD', 'EUtranCellTDD']:
                val = crn_val(record, key)
                if val:
                    return val

        if p == '$TARGETARFCNNR':
            val = crn_val(record, 'GUtranFreqRelation')
            if val:
                return val

        if p == '$NODEUSERLABEL':
            ref = field_val(record, 'neighborCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalGNodeBFunction')
                if val:
                    return val

        if p == '$NREXTERNALCELLID':
            val = crn_val(record, 'GUtranCellRelation')
            if val:
                return val
            ref = field_val(record, 'neighborCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalGUtranCell')
                if val:
                    return val

    # === Section 04: External_ENodeBFunction ===
    if p == '$NODEUSERLABELEXT':
        for key in ['ExternalENodeBFunction']:
            val = crn_val(record, key)
            if val:
                return val

    if p == '$NODEIDEXT':
        val = field_val(record, 'eNBId')
        if val:
            return val
        # Try to extract from ExternalENodeBFunction ID: "5152-404014" -> "404014"
        ext_enb = crn_val(record, 'ExternalENodeBFunction')
        if ext_enb and '-' in ext_enb:
            return ext_enb.split('-', 1)[1]

    # === Section 05: TermpointtoENB (nested inside 04) ===
    if p == '$CUPINIPV4' and section_name == '05_TermpointtoENB':
        return field_val(record, 'ipAddress')

    # === Section 06: External_Eutrancell (nested inside 04) ===
    if section_name == '06_External_Eutrancell':
        if p == '$NODEUSERLABELEXT':
            return crn_val(record, 'ExternalENodeBFunction')
        if p == '$NODEIDEXT':
            # Extract from ExternalENodeBFunction ID or lookup
            ext_enb = crn_val(record, 'ExternalENodeBFunction')
            if ext_enb and '-' in ext_enb:
                return ext_enb.split('-', 1)[1]
        if p == '$CELLIDLTECIQ':
            return field_val(record, 'localCellId')
        if p == '$PCIGLTECIQ':
            return field_val(record, 'physicalLayerCellIdGroup')
        if p == '$PSCILTECIQ':
            return field_val(record, 'physicalLayerSubCellId')
        if p == '$TACLTECIQ':
            return field_val(record, 'tac')
        if p == '$ARFCNDLLTECIQ':
            ref = field_val(record, 'eutranFrequencyRef')
            if ref:
                match = re.search(r'EUtranFrequency=(\S+)', ref)
                if match:
                    return match.group(1)

    # === Section 07: External_Eutrancell_relation ===
    if section_name == '07_External_Eutrancell_relation':
        if p == '$CELLNAMELTECIQ':
            for key in ['EUtranCellFDD', 'EUtranCellTDD']:
                val = crn_val(record, key)
                if val:
                    return val
        if p == '$TARGETARFCNLTE':
            return crn_val(record, 'EUtranFreqRelation')
        if p == '$TARGETCELLNAMELTE':
            return crn_val(record, 'EUtranCellRelation')
        if p == '$NODEUSERLABELEXT':
            ref = field_val(record, 'neighborCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalENodeBFunction')
                if val:
                    return val
        if p == '$NODEIDEXT':
            ref = field_val(record, 'neighborCellRef')
            if ref:
                ext_enb = extract_from_ref(ref, 'ExternalENodeBFunction')
                if ext_enb and '-' in ext_enb:
                    return ext_enb.split('-', 1)[1]
            # Fallback: lookup in section 04 data
            ext_enb_val = ''
            ref = field_val(record, 'neighborCellRef')
            if ref:
                ext_enb_val = extract_from_ref(ref, 'ExternalENodeBFunction')
            if ext_enb_val:
                enb_data = all_data.get('04_External_ENodeBFunction', [])
                for enb_rec in enb_data:
                    if crn_val(enb_rec, 'ExternalENodeBFunction') == ext_enb_val:
                        return field_val(enb_rec, 'eNBId')
        if p == '$CELLIDLTECIQ':
            ref = field_val(record, 'neighborCellRef')
            if ref:
                ext_cell = extract_from_ref(ref, 'ExternalEUtranCellFDD')
                if not ext_cell:
                    ext_cell = extract_from_ref(ref, 'ExternalEUtranCellTDD')
                if ext_cell:
                    parts = ext_cell.rsplit('-', 1)
                    if len(parts) >= 2:
                        return parts[-1]
            # Fallback: lookup in section 06 data
            ext_cell_id = ''
            ref = field_val(record, 'neighborCellRef')
            if ref:
                ext_cell_id = extract_from_ref(ref, 'ExternalEUtranCellFDD')
                if not ext_cell_id:
                    ext_cell_id = extract_from_ref(ref, 'ExternalEUtranCellTDD')
            if ext_cell_id:
                cell_data = all_data.get('06_External_Eutrancell', [])
                for cell_rec in cell_data:
                    cell_name = crn_val(cell_rec, 'ExternalEUtranCellFDD')
                    if not cell_name:
                        cell_name = crn_val(cell_rec, 'ExternalEUtranCellTDD')
                    if cell_name == ext_cell_id:
                        return field_val(cell_rec, 'localCellId')

    # === Section 08: External_GNBCUCPfunction ===
    if section_name == '08_External_GNBCUCPfunction':
        if p == '$NODEUSERLABELEXT':
            val = crn_val(record, 'ExternalGNBCUCPFunction')
            if val:
                return val
        if p == '$NODEIDEXT':
            val = field_val(record, 'gNBId')
            if val:
                return val
            ext_gnb = crn_val(record, 'ExternalGNBCUCPFunction')
            if ext_gnb and '-' in ext_gnb:
                return ext_gnb.split('-', 1)[1]

    # === Section 09: External_NRCellCU ===
    if section_name == '09_External_NRCellCU':
        if p == '$NODEUSERLABELEXT':
            val = crn_val(record, 'ExternalGNBCUCPFunction')
            if val:
                return val
        if p == '$CELLINAMENR':
            val = crn_val(record, 'ExternalNRCellCU')
            if val:
                return val
        if p == '$CELLIDNRCIQ':
            return field_val(record, 'cellLocalId')
        if p == '$GUTRANSYNCFREQID':
            ref = field_val(record, 'nRFrequencyRef')
            if ref:
                match = re.search(r'NRFrequency=(\S+)', ref)
                if match:
                    return match.group(1)
        if p == '$PCINRCIQ':
            return field_val(record, 'nRPCI')
        if p == '$TACNRCIQ':
            return field_val(record, 'nRTAC')

    # === Section 10: ExternalNRCellRelation ===
    if section_name == '10_ExternalNRCellRelation':
        if p == '$CELLNAMENRCIQ':
            val = crn_val(record, 'NRCellCU')
            if val:
                return val
        if p == '$TARGETCELLNAMENR':
            val = crn_val(record, 'NRCellRelation')
            if val:
                return val
        if p == '$NODEUSERLABELEXT':
            ref = field_val(record, 'nRCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalGNBCUCPFunction')
                if val:
                    return val
        if p == '$NCI':
            ref = field_val(record, 'nRCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalNRCellCU')
                if val:
                    return val
        if p == '$TARGETARFCNNR':
            ref = field_val(record, 'nRFreqRelationRef')
            if ref:
                match = re.search(r'NRFreqRelation=(\S+)', ref)
                if match:
                    return match.group(1).rstrip(',')

    # === Section 11: External_Grancell ===
    if section_name == '11_External_Grancell':
        if p == '$GERANID':
            val = crn_val(record, 'ExternalGeranCell')
            if val:
                return val
        if p == '$GERANLAC':
            return field_val(record, 'lac')
        if p == '$GERANCELLID':
            return field_val(record, 'cellIdentity')
        if p == '$GERANBCC':
            return field_val(record, 'bcc')
        if p == '$GERANNCC':
            return field_val(record, 'ncc')
        if p == '$GERANRAC':
            return field_val(record, 'rac')
        if p == '$GERANFREQ':
            ref = field_val(record, 'geranFrequencyRef')
            if ref:
                match = re.search(r'GeranFrequency=(\S+)', ref)
                if match:
                    return match.group(1)

    # === Section 12: Geran_CellRelation ===
    if section_name == '12_Geran_CellRelation':
        if p == '$CELLNAMELTECIQ':
            for key in ['EUtranCellFDD', 'EUtranCellTDD', 'EutranCellFDD', 'EutranCellTDD']:
                val = crn_val(record, key)
                if val:
                    return val
        if p == '$GERANID':
            val = crn_val(record, 'GeranCellRelation')
            if val:
                return val
            ref = field_val(record, 'extGeranCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalGeranCell')
                if val:
                    return val

    # === Section 14: Internal_NRCellRelation ===
    if section_name == '14_Internal_NRCellRelation':
        if p == '$CELLNAMENRCIQ':
            val = crn_val(record, 'NRCellCU')
            if val:
                return val
        if p == '$TARGETCELLNAMENR':
            val = crn_val(record, 'NRCellRelation')
            if val:
                return val
            ref = field_val(record, 'nRCellRef')
            if ref:
                val = extract_from_ref(ref, 'NRCellCU')
                if val:
                    return val
        if p == '$TARGETARFCNNR':
            ref = field_val(record, 'nRFreqRelationRef')
            if ref:
                match = re.search(r'NRFreqRelation=(\S+)', ref)
                if match:
                    return match.group(1).rstrip(',')

    # === Section 15: ExternalENodeBFunction (GNBCUCP) ===
    if section_name == '15_ExternalENodeBFunction':
        if p == '$NODEUSERLABEL':
            val = crn_val(record, 'ExternalENodeBFunction')
            if val:
                return val
        if p == '$NODEID':
            val = field_val(record, 'eNodeBId')
            if val:
                return val

    # === Section 16: ExternalEUtranCell (GNBCUCP) ===
    if section_name == '16_ExternalEUtranCell':
        if p == '$NODEUSERLABEL':
            val = crn_val(record, 'ExternalENodeBFunction')
            if val:
                return val
        if p == '$EXTERNALEUTRANCELLLTE':
            val = crn_val(record, 'ExternalEUtranCell')
            if val:
                return val
        if p == '$CELLIDLTECIQ':
            return field_val(record, 'cellLocalId')
        if p == '$TARGETARFCNESS':
            ref = field_val(record, 'eUtranFrequencyRef')
            if ref:
                match = re.search(r'EUtranFrequency=(\S+)', ref)
                if match:
                    return match.group(1)
        if p == '$PCILTECIQ':
            return field_val(record, 'physicalLayerCellId')
        if p == '$TACLTECIQ':
            return field_val(record, 'tac')

    # === Section 17: EUtranCellRelation (GNBCUCP) ===
    if section_name == '17_EUtranCellRelation':
        if p == '$CELLNAMENRCIQ':
            val = crn_val(record, 'NRCellCU')
            if val:
                return val
        if p == '$EXTERNALEUTRANCELLLTE':
            val = crn_val(record, 'EUtranCellRelation')
            if val:
                return val
            ref = field_val(record, 'neighborCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalEUtranCell')
                if val:
                    return val
        if p == '$NODEUSERLABEL':
            ref = field_val(record, 'neighborCellRef')
            if ref:
                val = extract_from_ref(ref, 'ExternalENodeBFunction')
                if val:
                    return val

    # === Section 13: Eutran_CellRelation ===
    if section_name == '13_Eutran_CellRelation':
        if p == '$CELLNAMELTECIQ':
            for key in ['EUtranCellFDD', 'EUtranCellTDD']:
                val = crn_val(record, key)
                if val:
                    return val
        if p == '$TARGETARFCNLTE':
            return crn_val(record, 'EUtranFreqRelation')
        if p == '$TARGETCELLNAMELTE':
            return crn_val(record, 'EUtranCellRelation')

    # === Generic fallback: try CRN and fields ===
    var_name = p.lstrip('$')
    val = field_val(record, var_name)
    if val:
        return val
    val = crn_val(record, var_name)
    if val:
        return val

    return p


def substitute_placeholders(text, record, section_name, all_data, nodename):
    """Replace all $VARIABLE placeholders in text with values from record."""
    placeholders = set(re.findall(r'\$[A-Z][A-Z0-9_]*', text))
    result = text
    for ph in placeholders:
        value = resolve_placeholder(ph, record, section_name, all_data, nodename)
        result = result.replace(ph, str(value))
    return result


def convert_txt_to_xml(template_path, zip_path, output_path, site_name=None):
    """Main conversion function."""
    extract_dir = tempfile.mkdtemp(prefix='nbr_')
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)

    zip_root = os.path.join(extract_dir, 'NBR_Relation_Scripts_Intra_Inter_T7_PA2')
    if not os.path.exists(zip_root):
        for item in os.listdir(extract_dir):
            item_path = os.path.join(extract_dir, item)
            if os.path.isdir(item_path):
                zip_root = item_path
                break

    if site_name:
        site_folder = os.path.join(zip_root, site_name)
    else:
        sites = sorted([d for d in os.listdir(zip_root)
                        if os.path.isdir(os.path.join(zip_root, d))])
        if not sites:
            print("No site folders found.")
            return
        site_folder = os.path.join(zip_root, sites[0])
        site_name = sites[0]

    nodename = site_name
    print(f"Processing site: {site_name}")
    print(f"Node name: {nodename}")

    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    # Find all source data files
    all_data = {}
    source_files = {}
    for filename in os.listdir(site_folder):
        if not filename.endswith('.txt'):
            continue
        filepath = os.path.join(site_folder, filename)
        prefix = None
        min_matches = list(re.finditer(r'_MIN\d', filename))
        if min_matches:
            raw_prefix = filename[:min_matches[-1].start()]
            # Strip any trailing suffix groups like _GNBCUCP that aren't part of template section names
            # Template sections use format: NN_Description (e.g., 15_ExternalENodeBFunction)
            # Files may add suffixes like _GNBCUCP (e.g., 15_ExternalENodeBFunction_GNBCUCP)
            # Keep removing trailing _WORD groups until we have at most NN_WORD format
            match = re.match(r'^(\d+_[A-Za-z]+(?:_[A-Za-z]+)*)$', raw_prefix)
            if match:
                # Check if this matches a known suffix pattern; strip trailing known suffixes
                for suffix in ['_GNBCUCP']:
                    if raw_prefix.endswith(suffix):
                        raw_prefix = raw_prefix[:-len(suffix)]
                        break
            prefix = raw_prefix
        else:
            match = re.match(r'^(\d+_[A-Za-z]+(?:_[A-Za-z]+)*)_', filename)
            if match:
                prefix = match.group(1)
            else:
                parts = filename.rsplit('_', 2)
                if len(parts) >= 3:
                    prefix = '_'.join(parts[:-2])
        if prefix:
            records = parse_data_file(filepath)
            all_data[prefix] = records
            source_files[prefix] = filepath
            print(f"  Loaded {len(records)} records from {filename}")

    section_pattern = re.compile(
        r'<!--\s*(\d+_\w+)\s+Start\s*-->(.*?)<!--\s*\1\s+End\s*-->',
        re.DOTALL
    )

    sections_found = []
    for match in section_pattern.finditer(template):
        section_name = match.group(1)
        section_content = match.group(2)
        sections_found.append({
            'name': section_name,
            'content': section_content,
            'start': match.start(),
            'end': match.end(),
            'full_match': match.group(0)
        })

    result = template
    result = result.replace('$NODENAME', nodename)

    for sec in sections_found:
        section_name = sec['name']
        section_content = sec['content']

        records = all_data.get(section_name, [])

        if section_name == '01_External_GNodeBFunction':
            xml_blocks = process_section_01(section_content, records, all_data, nodename)
        elif section_name == '02_External_GutraCell':
            xml_blocks = process_section_02(section_content, records, all_data, nodename)
        elif section_name == '03_GUtran_CellRelation':
            xml_blocks = process_section_03(section_content, records, all_data, nodename)
        elif section_name == '04_External_ENodeBFunction':
            xml_blocks = process_section_04(section_content, records, all_data, nodename)
        elif section_name == '07_External_Eutrancell_relation':
            xml_blocks = process_section_07(section_content, records, all_data, nodename)
        elif section_name == '13_Eutran_CellRelation':
            xml_blocks = process_section_13(section_content, records, all_data, nodename)
        elif section_name == '08_External_GNBCUCPfunction':
            xml_blocks = process_section_08(section_content, records, all_data, nodename)
        elif section_name == '09_External_NRCellCU':
            xml_blocks = process_section_09(section_content, records, all_data, nodename)
        elif section_name == '10_ExternalNRCellRelation':
            xml_blocks = process_section_10(section_content, records, all_data, nodename)
        elif section_name == '11_External_Grancell':
            xml_blocks = process_section_11(section_content, records, all_data, nodename)
        elif section_name == '12_Geran_CellRelation':
            xml_blocks = process_section_12(section_content, records, all_data, nodename)
        elif section_name == '14_Internal_NRCellRelation':
            xml_blocks = process_section_14(section_content, records, all_data, nodename)
        elif section_name == '15_ExternalENodeBFunction':
            xml_blocks = process_section_15(section_content, records, all_data, nodename)
        elif section_name == '16_ExternalEUtranCell':
            xml_blocks = process_section_16(section_content, records, all_data, nodename)
        elif section_name == '17_EUtranCellRelation':
            xml_blocks = process_section_17(section_content, records, all_data, nodename)
        else:
            xml_blocks = []
            for rec in records:
                block = substitute_placeholders(
                    section_content, rec, section_name, all_data, nodename
                )
                xml_blocks.append(block)

        replacement = '\n'.join(xml_blocks)
        result = result.replace(sec['full_match'], replacement, 1)

    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)

    print(f"\nOutput written to: {output_path}")
    print(f"Total lines: {len(result.splitlines())}")
    return output_path


def process_section_01(section_content, records, all_data, nodename):
    """Process section 01: External_GNodeBFunction with embedded TermPointToGNB."""
    gnb_records = [r for r in records if r.get('_type') == 'ExternalGNodeBFunction']
    tp_records = [r for r in records if r.get('_type') == 'TermPointToGNB']

    tp_lookup = {}
    for tp in tp_records:
        ext_gnb = crn_val(tp, 'ExternalGNodeBFunction')
        tp_lookup[ext_gnb] = tp

    # Extract TemplatePointToGNB sub-section
    tp_match = re.search(
        r'(<TermPointToGNB>.*?</TermPointToGNB>)',
        section_content, re.DOTALL
    )

    blocks = []
    for gnb_rec in gnb_records:
        gnb_id = crn_val(gnb_rec, 'ExternalGNodeBFunction')

        # Merge GNodeBFunction and TermPointToGNB data
        merged = {
            '_crn': {**gnb_rec['_crn']},
            '_type': gnb_rec['_type'],
            '_fields': {**gnb_rec['_fields']}
        }
        if gnb_id in tp_lookup:
            tp = tp_lookup[gnb_id]
            merged['_fields']['ipAddress'] = field_val(tp, 'ipAddress')
            merged['_fields']['administrativeState'] = field_val(tp, 'administrativeState')

        block = substitute_placeholders(
            section_content, merged, '01_External_GNodeBFunction', all_data, nodename
        )

        # Replace TermPointToGNB subsection with actual data
        if tp_match and gnb_id in tp_lookup:
            tp = tp_lookup[gnb_id]
            tp_template = tp_match.group(1)
            tp_xml = substitute_placeholders(
                tp_template, tp, '01_External_GNodeBFunction', all_data, nodename
            )
            block = block.replace(tp_template, tp_xml)

        # Handle gNodeBIdLength from data
        gnb_id_length = field_val(gnb_rec, 'gNodeBIdLength')
        if gnb_id_length:
            block = block.replace('<gNodeBIdLength>22</gNodeBIdLength>',
                                  f'<gNodeBIdLength>{gnb_id_length}</gNodeBIdLength>')

        # Handle PLMN from data
        plmn = field_val(gnb_rec, 'gNodeBPlmnId')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '02')
            mnc_len = plmn_parts.get('mncLength', '2')
            # Replace only the PLMN within this block (first occurrence)
            block = block.replace(
                '<mcc>515</mcc>\n\t\t\t\t\t\t\t\t<mnc>2</mnc>\n\t\t\t\t\t\t\t\t<mncLength>2</mncLength>',
                f'<mcc>{mcc}</mcc>\n\t\t\t\t\t\t\t\t<mnc>{mnc}</mnc>\n\t\t\t\t\t\t\t\t<mncLength>{mnc_len}</mncLength>',
                1
            )

        # Handle administrativeState (1 = LOCKED, 0 = UNLOCKED for TermPointToGNB)
        admin_state = field_val(merged, 'administrativeState')
        if admin_state:
            state_str = 'LOCKED' if admin_state == '1' else 'UNLOCKED'
            block = block.replace('<administrativeState>UNLOCKED</administrativeState>',
                                  f'<administrativeState>{state_str}</administrativeState>', 1)

        blocks.append(block)

    return blocks


def process_section_02(section_content, records, all_data, nodename):
    """Process section 02: External_GutraCell."""
    cell_records = [r for r in records if r.get('_type') == 'ExternalGUtranCell']
    blocks = []
    for rec in cell_records:
        block = substitute_placeholders(
            section_content, rec, '02_External_GutraCell', all_data, nodename
        )
        blocks.append(block)
    return blocks


def process_section_03(section_content, records, all_data, nodename):
    """Process section 03: GUtran_CellRelation."""
    rel_records = [r for r in records if r.get('_type') == 'GUtranCellRelation']
    blocks = []
    for rec in rel_records:
        # Additional fields from data
        ess_enabled = field_val(rec, 'essEnabled')
        is_endc = field_val(rec, 'isEndcAllowed')
        is_ho = field_val(rec, 'isHoAllowed')
        is_voice_ho = field_val(rec, 'isVoiceHoAllowed')
        is_remove = field_val(rec, 'isRemoveAllowed')
        neighbor_ref = field_val(rec, 'neighborCellRef')

        block = substitute_placeholders(
            section_content, rec, '03_GUtran_CellRelation', all_data, nodename
        )

        # Update boolean fields from source data
        if ess_enabled:
            block = block.replace('<essEnabled>false</essEnabled>',
                                  f'<essEnabled>{ess_enabled.lower()}</essEnabled>', 1)
        if is_endc:
            block = block.replace('<isEndcAllowed>true</isEndcAllowed>',
                                  f'<isEndcAllowed>{is_endc.lower()}</isEndcAllowed>', 1)
        if neighbor_ref:
            # Update neighborCellRef with actual reference from data
            old_ref = 'ManagedElement=1,ENodeBFunction=1,GUtraNetwork=1,ExternalGNodeBFunction=$NODEUSERLABEL,ExternalGUtranCell=$NREXTERNALCELLID'
            # Check if old_ref (with unresolved placeholders) is in the block
            # After substitution, $NODEUSERLABEL and $NREXTERNALCELLID should be resolved
            pass

        blocks.append(block)
    return blocks


def process_section_04(section_content, records, all_data, nodename):
    """Process section 04: External_ENodeBFunction with nested sections 05 and 06."""
    enb_records = [r for r in records if r.get('_type') == 'ExternalENodeBFunction']
    tpenb_records = all_data.get('05_TermpointtoENB', [])
    extcell_records = all_data.get('06_External_Eutrancell', [])

    tp_lookup = {}
    for tp in tpenb_records:
        ext_enb = crn_val(tp, 'ExternalENodeBFunction')
        if ext_enb not in tp_lookup:
            tp_lookup[ext_enb] = []
        tp_lookup[ext_enb].append(tp)

    cell_groups = {}
    for cell in extcell_records:
        ext_enb = crn_val(cell, 'ExternalENodeBFunction')
        if ext_enb not in cell_groups:
            cell_groups[ext_enb] = []
        cell_groups[ext_enb].append(cell)

    section_05_match = re.search(
        r'(<!--\s*05_TermpointtoENB\s+Start\s*-->)(.*?)(<!--\s*05_TermpointtoENB\s+End\s*-->)',
        section_content, re.DOTALL
    )
    section_06_match = re.search(
        r'(<!--\s*06_External_Eutrancell\s+Start\s*-->)(.*?)(<!--\s*06_External_Eutrancell\s+End\s*-->)',
        section_content, re.DOTALL
    )

    blocks = []
    for enb_rec in enb_records:
        enb_id = crn_val(enb_rec, 'ExternalENodeBFunction')

        block = section_content

        if section_05_match:
            inner_05 = section_05_match.group(2)
            if enb_id in tp_lookup and tp_lookup[enb_id]:
                tp_rec = tp_lookup[enb_id][0]
                tp_xml = substitute_placeholders(
                    inner_05, tp_rec, '05_TermpointtoENB', all_data, nodename
                )
                tp_xml = tp_xml.replace('$NODEUSERLABELEXT', enb_id)
                eNBId = field_val(enb_rec, 'eNBId')
                tp_xml = tp_xml.replace('$NODEIDEXT', eNBId)
                admin_state = field_val(tp_rec, 'administrativeState')
                if admin_state:
                    state_str = 'LOCKED' if admin_state == '1' else 'UNLOCKED'
                    tp_xml = tp_xml.replace('<administrativeState>UNLOCKED</administrativeState>',
                                           f'<administrativeState>{state_str}</administrativeState>', 1)
            else:
                tp_xml = ''
            full_05 = section_05_match.group(0)
            replacement_05 = section_05_match.group(1) + tp_xml + section_05_match.group(3)
            block = block.replace(full_05, replacement_05, 1)

        if section_06_match:
            inner_06 = section_06_match.group(2)
            cells = cell_groups.get(enb_id, [])
            cell_xml_parts = []
            for cell_rec in cells:
                cell_merged = {
                    '_crn': {**cell_rec['_crn'], 'ExternalENodeBFunction': enb_id},
                    '_type': cell_rec.get('_type', 'ExternalEUtranCellFDD'),
                    '_fields': {**cell_rec['_fields']}
                }
                cell_xml = substitute_placeholders(
                    inner_06, cell_merged, '06_External_Eutrancell', all_data, nodename
                )
                cell_xml = cell_xml.replace('$NODEUSERLABELEXT', enb_id)
                cell_xml = cell_xml.replace('$NODEIDEXT', field_val(enb_rec, 'eNBId'))
                cell_xml_parts.append(cell_xml)

            full_06 = section_06_match.group(0)
            cells_joined = ''.join(cell_xml_parts)
            replacement_06 = section_06_match.group(1) + cells_joined + section_06_match.group(3)
            block = block.replace(full_06, replacement_06, 1)

        # Now substitute placeholders for the ENB record itself
        block = substitute_placeholders(
            block, enb_rec, '04_External_ENodeBFunction', all_data, nodename
        )

        # Handle eNodeBPlmnId from data
        plmn = field_val(enb_rec, 'eNodeBPlmnId')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '02')
            mnc_len = plmn_parts.get('mncLength', '2')
            block = block.replace(
                f'<mcc>{mcc}</mcc>',
                f'<mcc>{mcc}</mcc>', 1
            )

        blocks.append(block)

    return blocks


def process_section_07(section_content, records, all_data, nodename):
    """Process section 07: External_Eutrancell_relation."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '07_External_Eutrancell_relation', all_data, nodename
        )
        blocks.append(block)
    return blocks


def process_section_13(section_content, records, all_data, nodename):
    """Process section 13: Eutran_CellRelation."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue

        block = substitute_placeholders(
            section_content, rec, '13_Eutran_CellRelation', all_data, nodename
        )

        # Update fields from source data
        cell_offset = field_val(rec, 'cellIndividualOffsetEUtran')
        load_bal = field_val(rec, 'loadBalancing')
        lb_bnr = field_val(rec, 'lbBnrAllowed')
        is_ho = field_val(rec, 'isHoAllowed')
        s_cell = field_val(rec, 'sCellCandidate')

        if cell_offset:
            block = block.replace(
                '<cellIndividualOffsetEUtran>0</cellIndividualOffsetEUtran>',
                f'<cellIndividualOffsetEUtran>{cell_offset}</cellIndividualOffsetEUtran>', 1
            )

        # Update loadBalancing: 0 = NOT_ALLOWED, 1 = ALLOWED
        if load_bal:
            lb_val = 'ALLOWED' if load_bal.upper() == '1' or load_bal == 'TRUE' else 'NOT_ALLOWED'
            block = block.replace(
                '<loadBalancing>ALLOWED</loadBalancing>',
                f'<loadBalancing>{lb_val}</loadBalancing>', 1
            )

        # Update lbBnrAllowed
        if lb_bnr:
            lb_bnr_val = 'true' if lb_bnr.upper() == 'TRUE' else 'false'
            block = block.replace(
                '<lbBnrAllowed>false</lbBnrAllowed>',
                f'<lbBnrAllowed>{lb_bnr_val}</lbBnrAllowed>', 1
            )

        # Update sCellCandidate: 0=NOT_ALLOWED, 1=ALLOWED, 2=NOT_ALLOWED
        if s_cell:
            sc_val = 'ALLOWED' if s_cell in ('1', 'TRUE') else 'NOT_ALLOWED'
            block = block.replace(
                '<sCellCandidate>ALLOWED</sCellCandidate>',
                f'<sCellCandidate>{sc_val}</sCellCandidate>', 1
            )

        # Update isHoAllowedBr
        if is_ho:
            is_ho_val = 'true' if is_ho.upper() == 'TRUE' else 'false'
            block = block.replace(
                '<isHoAllowedBr>false</isHoAllowedBr>',
                f'<isHoAllowedBr>{is_ho_val}</isHoAllowedBr>', 1
            )

        blocks.append(block)
    return blocks


def process_section_08(section_content, records, all_data, nodename):
    """Process section 08: External_GNBCUCPfunction."""
    gnb_records = [r for r in records if r.get('_type') == 'ExternalGNBCUCPFunction']
    blocks = []
    for rec in gnb_records:
        block = substitute_placeholders(
            section_content, rec, '08_External_GNBCUCPfunction', all_data, nodename
        )
        gnb_id_length = field_val(rec, 'gNBIdLength')
        if gnb_id_length:
            block = block.replace('<gNBIdLength>25</gNBIdLength>',
                                   f'<gNBIdLength>{gnb_id_length}</gNBIdLength>', 1)
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>false</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        plmn = field_val(rec, 'pLMNId')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '02')
            block = block.replace('<mcc>515</mcc>\n\t\t\t\t\t\t<mnc>02</mnc>',
                                   f'<mcc>{mcc}</mcc>\n\t\t\t\t\t\t<mnc>{mnc}</mnc>', 1)
        blocks.append(block)
    return blocks


def process_section_09(section_content, records, all_data, nodename):
    """Process section 09: External_NRCellCU."""
    cell_records = [r for r in records if r.get('_type') == 'ExternalNRCellCU']
    blocks = []
    for rec in cell_records:
        block = substitute_placeholders(
            section_content, rec, '09_External_NRCellCU', all_data, nodename
        )
        plmn = field_val(rec, 'plmnIdList')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '02')
            block = block.replace('<mcc>515</mcc>\n\t\t\t\t\t\t\t<mnc>02</mnc>',
                                   f'<mcc>{mcc}</mcc>\n\t\t\t\t\t\t\t<mnc>{mnc}</mnc>', 1)
        blocks.append(block)
    return blocks


def process_section_10(section_content, records, all_data, nodename):
    """Process section 10: ExternalNRCellRelation."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '10_ExternalNRCellRelation', all_data, nodename
        )
        cell_offset = field_val(rec, 'cellIndividualOffsetNR')
        if cell_offset:
            block = block.replace('<cellIndividualOffsetNR>0</cellIndividualOffsetNR>',
                                   f'<cellIndividualOffsetNR>{cell_offset}</cellIndividualOffsetNR>', 1)
        cov = field_val(rec, 'coverageIndicator')
        if cov:
            cov_val = 'NOT_ALLOWED' if cov == '0' or cov.upper() == 'FALSE' else ('ALLOWED' if cov == '1' else cov)
            # coverageIndicator maps differently
        is_ho = field_val(rec, 'isHoAllowed')
        if is_ho:
            ho_val = 'true' if is_ho.upper() == 'TRUE' else 'false'
            block = block.replace('<isHoAllowed>true</isHoAllowed>',
                                   f'<isHoAllowed>{ho_val}</isHoAllowed>', 1)
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>false</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        s_cell = field_val(rec, 'sCellCandidate')
        if s_cell:
            sc_val = 'NOT_ALLOWED' if s_cell in ('0', 'FALSE') else ('ALLOWED' if s_cell == '1' else 'NOT_ALLOWED')
            block = block.replace('<sCellCandidate>NOT_ALLOWED</sCellCandidate>',
                                   f'<sCellCandidate>{sc_val}</sCellCandidate>', 1)
        blocks.append(block)
    return blocks


def process_section_11(section_content, records, all_data, nodename):
    """Process section 11: External_Grancell."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '11_External_Grancell', all_data, nodename
        )
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>true</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        plmn = field_val(rec, 'plmnIdentity')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '2')
            mnc_len = plmn_parts.get('mncLength', '2')
            block = block.replace('<mcc>515</mcc>\n\t\t\t\t\t\t\t\t<mnc>2</mnc>\n\t\t\t\t\t\t\t\t<mncLength>2</mncLength>',
                                   f'<mcc>{mcc}</mcc>\n\t\t\t\t\t\t\t\t<mnc>{mnc}</mnc>\n\t\t\t\t\t\t\t\t<mncLength>{mnc_len}</mncLength>', 1)
        blocks.append(block)
    return blocks


def process_section_12(section_content, records, all_data, nodename):
    """Process section 12: Geran_CellRelation."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '12_Geran_CellRelation', all_data, nodename
        )
        cov = field_val(rec, 'coverageIndicator')
        if cov:
            cov_val = 'NONE' if cov in ('0', '') else cov
            block = block.replace('<coverageIndicator>NONE</coverageIndicator>',
                                   f'<coverageIndicator>{cov_val}</coverageIndicator>', 1)
        is_ho = field_val(rec, 'isHoAllowed')
        if is_ho:
            ho_val = 'true' if is_ho.upper() == 'TRUE' else 'false'
            block = block.replace('<isHoAllowed>true</isHoAllowed>',
                                   f'<isHoAllowed>{ho_val}</isHoAllowed>', 1)
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>true</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        blocks.append(block)
    return blocks


def process_section_14(section_content, records, all_data, nodename):
    """Process section 14: Internal_NRCellRelation."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '14_Internal_NRCellRelation', all_data, nodename
        )
        cell_offset = field_val(rec, 'cellIndividualOffsetNR')
        if cell_offset:
            block = block.replace('<cellIndividualOffsetNR>0</cellIndividualOffsetNR>',
                                   f'<cellIndividualOffsetNR>{cell_offset}</cellIndividualOffsetNR>', 1)
        cov = field_val(rec, 'coverageIndicator')
        if cov:
            cov_val = 'NONE' if cov == '0' else cov
            block = block.replace('<coverageIndicator>NONE</coverageIndicator>',
                                   f'<coverageIndicator>{cov_val}</coverageIndicator>', 1)
        is_ho = field_val(rec, 'isHoAllowed')
        if is_ho:
            ho_val = 'true' if is_ho.upper() == 'TRUE' else 'false'
            block = block.replace('<isHoAllowed>true</isHoAllowed>',
                                   f'<isHoAllowed>{ho_val}</isHoAllowed>', 1)
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>false</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        s_cell = field_val(rec, 'sCellCandidate')
        if s_cell:
            sc_val = 'ALLOWED' if s_cell in ('1', 'TRUE') else 'NOT_ALLOWED'
            block = block.replace('<sCellCandidate>ALLOWED</sCellCandidate>',
                                   f'<sCellCandidate>{sc_val}</sCellCandidate>', 1)
        blocks.append(block)
    return blocks


def process_section_15(section_content, records, all_data, nodename):
    """Process section 15: ExternalENodeBFunction (GNBCUCP)."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '15_ExternalENodeBFunction', all_data, nodename
        )
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>false</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        plmn = field_val(rec, 'pLMNId')
        if plmn:
            plmn_parts = {}
            for pair in plmn.split(','):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    plmn_parts[k] = v
            mcc = plmn_parts.get('mcc', '515')
            mnc = plmn_parts.get('mnc', '02')
            block = block.replace('<mcc>515</mcc>\n\t\t\t\t\t\t\t\t<mnc>02</mnc>',
                                   f'<mcc>{mcc}</mcc>\n\t\t\t\t\t\t\t\t<mnc>{mnc}</mnc>', 1)
        blocks.append(block)
    return blocks


def process_section_16(section_content, records, all_data, nodename):
    """Process section 16: ExternalEUtranCell (GNBCUCP)."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '16_ExternalEUtranCell', all_data, nodename
        )
        blocks.append(block)
    return blocks


def process_section_17(section_content, records, all_data, nodename):
    """Process section 17: EUtranCellRelation (GNBCUCP)."""
    blocks = []
    for rec in records:
        if '_crn' not in rec:
            continue
        block = substitute_placeholders(
            section_content, rec, '17_EUtranCellRelation', all_data, nodename
        )
        cell_offset = field_val(rec, 'cellIndividualOffset')
        if cell_offset:
            block = block.replace('<cellIndividualOffset>0</cellIndividualOffset>',
                                   f'<cellIndividualOffset>{cell_offset}</cellIndividualOffset>', 1)
        ess = field_val(rec, 'essEnabled')
        if ess:
            block = block.replace('<essEnabled>true</essEnabled>',
                                   f'<essEnabled>{ess.lower()}</essEnabled>', 1)
        is_ho = field_val(rec, 'isHoAllowed')
        if is_ho:
            ho_val = 'true' if is_ho.upper() == 'TRUE' else 'false'
            block = block.replace('<isHoAllowed>true</isHoAllowed>',
                                   f'<isHoAllowed>{ho_val}</isHoAllowed>', 1)
        is_remove = field_val(rec, 'isRemoveAllowed')
        if is_remove:
            remove_val = 'true' if is_remove.upper() == 'TRUE' else 'false'
            block = block.replace('<isRemoveAllowed>false</isRemoveAllowed>',
                                   f'<isRemoveAllowed>{remove_val}</isRemoveAllowed>', 1)
        blocks.append(block)
    return blocks


def main():
    parser = argparse.ArgumentParser(description='Convert NBR Relation data files to XML')
    parser.add_argument('--template', default='Relation.txt',
                        help='Path to template XML file')
    parser.add_argument('--zip', default='NBR_Relation_Scripts_Intra_Inter_T7_PA2.zip',
                        help='Path to source data ZIP file')
    parser.add_argument('--output', default=None,
                        help='Output XML file path')
    parser.add_argument('--site', default=None,
                        help='Site folder name (e.g., MIN149_SANMIGUELTAGUMDDNB01)')
    parser.add_argument('--list-sites', action='store_true',
                        help='List available sites in the ZIP file')

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(script_dir, args.template) if not os.path.isabs(args.template) else args.template
    zip_path = os.path.join(script_dir, args.zip) if not os.path.isabs(args.zip) else args.zip

    if args.list_sites:
        with zipfile.ZipFile(zip_path, 'r') as z:
            sites = sorted(set([
                n.split('/')[1] for n in z.namelist()
                if n.count('/') >= 2 and not n.endswith('/')
            ]))
            for site in sites:
                print(site)
        return

    if args.output is None:
        args.output = os.path.join(script_dir, 'output.xml')

    convert_txt_to_xml(template_path, zip_path, args.output, args.site)


if __name__ == '__main__':
    main()