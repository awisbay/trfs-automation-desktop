"""Read-only external radio sharing audit using LLD and physical topology."""
import re
from collections import defaultdict
from openpyxl import load_workbook
from .audit_core import AuditResult
from .lld_audit import (_sheet_rows, _row_matches_node, _node_bb_index,
                        _bbid_index, _node_sync_ports, _radio_type_matches)


def _attr(attrs, key):
    return next((v for k,v in attrs.items() if k.casefold()==key.casefold()), '')


def _bool(value):
    value=str(value).strip().casefold()
    return True if value in ('true','1','yes') else False if value in ('false','0','no') else None


def _serial(attrs):
    value=_attr(attrs,'productData.serialNumber') or _attr(attrs,'serialNumber')
    if not value:
        match=re.search(r'serialNumber\s*=\s*([^,}]+)',str(_attr(attrs,'productData')),re.I)
        value=match[1] if match else ''
    value=str(value).strip().casefold()
    return value if value not in ('','0','-1','null','none','unknown','n/a') else ''


def audit_radio_sharing(records, nodes=None, lld_path=None, log=lambda m:None):
    """No set/restart commands. Missing evidence never implies non-shared."""
    indexed={}
    for dn,attrs in records.items():
        owner=re.search(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)',dn,re.I)
        if owner:
            indexed[(owner[1].casefold(),dn[owner.end():].casefold())]=(owner[1],dn[owner.end():],attrs)
    def resolve(ref,node):
        owner=re.search(r'(?:^|,)ManagedElement=([^,]+)(?:,|$)',ref,re.I)
        target=owner[1] if owner else node
        local=ref[owner.end():] if owner else ref
        hits=[v for (n,d),v in indexed.items() if n==target.casefold() and
              (d==local.casefold() or d.endswith(','+local.casefold()))]
        return hits[0] if len(hits)==1 else None
    def port(ref,node):
        match=re.match(r'^(.*(?:^|,)FieldReplaceableUnit=[^,]+),RiPort=([^,;\s]+)$',str(ref).strip(),re.I)
        if not match:
            return None
        fru=resolve(match[1],node)
        return (fru,match[2].upper()) if fru else None

    links=defaultdict(list)
    serial_users=defaultdict(set)
    for (node,mo),(_,_,attrs) in indexed.items():
        if mo.split(',')[-1].split('=')[0]!='rilink':
            continue
        left=port(_attr(attrs,'riPortRef1'),node)
        right=port(_attr(attrs,'riPortRef2'),node)
        if not left or not right:
            continue
        if left[0][1].split(',')[-1].casefold()=='fieldreplaceableunit=bb-1':
            bb,radio=left,right
        elif right[0][1].split(',')[-1].casefold()=='fieldreplaceableunit=bb-1':
            bb,radio=right,left
        else:
            continue  # Cascade is not direct evidence of a second BB.
        if bb[0][0].casefold()!=node:
            continue
        fru=radio[0]
        key=(fru[0].casefold(),fru[1].casefold())
        links[key].append((node,bb[1],radio[1]))
        serial=_serial(fru[2])
        if serial:
            serial_users[serial].add(node)

    planned=defaultdict(list)
    lld_error='LLD not supplied'
    if lld_path:
        try:
            wb=load_workbook(lld_path,read_only=True,data_only=True)
            try:
                col,data=_sheet_rows(wb,'CPRI connectivity',2)
                required={'PLA ID','BBID','BB RI Port','Radio DATA Port','Radio Shared between BB'}
                if not col or not required.issubset(col):
                    lld_error='LLD sharing/port columns missing'
                else:
                    lld_error=''
                    for node in set(v[0] for v in indexed.values()):
                        for row in data:
                            def cell(name):
                                i=col.get(name,-1)
                                return str(row[i] or '').strip() if 0<=i<len(row) else ''
                            if _row_matches_node(row,col,node.casefold()) and _bbid_index(cell('BBID'))==_node_bb_index(node) and _node_bb_index(node):
                                shared_raw = cell('Radio Shared between BB')
                                # In the LLD template, shared radios are marked
                                # "Yes" and ordinary/non-shared radios leave
                                # this cell blank. Treating blank as unknown hid
                                # real FRU=true mismatches.
                                shared = (False if not shared_raw
                                          else _bool(shared_raw))
                                planned[(node.casefold(),cell('BB RI Port').upper())].append(
                                    (shared,cell('Radio DATA Port').upper(),cell('Radio Type')))
            finally:
                wb.close()
        except Exception as exc:
            lld_error=f'LLD sharing evidence unavailable: {exc}'
            log('[audit/radio-sharing] '+lld_error)

    selected={n.casefold() for n in nodes} if nodes is not None else None
    out=[]
    for key,(node,mo,attrs) in sorted(indexed.items()):
        if selected is not None and node.casefold() not in selected:
            continue
        if mo.split(',')[-1].split('=')[0].casefold()!='fieldreplaceableunit':
            continue
        if mo.split(',')[-1].casefold()=='fieldreplaceableunit=bb-1':
            continue
        all_attached=links[key]
        targets=[]
        for (owner,child),(_,child_mo,child_attrs) in indexed.items():
            if owner==key[0] and child.startswith(key[1]+',riport=') and child.count(',')==key[1].count(',')+1:
                port_name=child_mo.rsplit('=',1)[1].upper()
                port_links=[link for link in all_attached if link[2]==port_name]
                if port_links:
                    targets.append((child_mo,child_attrs,port_links))
        # isSharedWithExternalMe is an FRU attribute. Collapse the connected
        # RiPorts back to their owning physical radio so the report checks the
        # actual MO/parameter instead of inventing a RiPort attribute.
        has_sharing_attr = any(
            k.casefold() == 'issharedwithexternalme' for k in attrs)
        if has_sharing_attr:
            targets = [(mo, attrs, all_attached)]
        elif not targets and all_attached:
            targets.append((mo,attrs,all_attached))
        elif targets:
            covered={t[0].rsplit('=',1)[1].upper() for t in targets}
            for port_name in sorted({link[2] for link in all_attached}-covered):
                targets.append((mo+',RiPort='+port_name,{},
                                [link for link in all_attached if link[2]==port_name]))
        for target_mo,target_attrs,attached in targets:
            issues=[]
            flags=[]
            details=[]
            for bb,bbport,radioport in attached:
                candidates=planned.get((bb,bbport),[])
                duplicate=sum(1 for ls in links.values() for link in ls if link[:2]==(bb,bbport))
                if duplicate!=1:
                    issues.append(f'{bb} RI {bbport}: duplicate/conflicting links')
                if len(candidates)!=1:
                    issues.append(f'{bb} RI {bbport}: LLD row missing/ambiguous')
                    continue
                flag,expected_port,radio_type=candidates[0]
                if flag is None or not expected_port:
                    issues.append(f'{bb} RI {bbport}: LLD sharing value/data port missing or invalid')
                elif expected_port!=radioport or (radio_type and not _radio_type_matches(radio_type,mo)):
                    issues.append(f'{bb} RI {bbport}: LLD radio/data-port topology differs from dump')
                else:
                    flags.append(flag)
                details.append(f'{bb} RI {bbport} -> {node}/{mo} RI {radioport}; LLD shared={flag}')
            if not attached:
                issues.append('No resolved direct BB RiLink for this radio')
            serial=_serial(attrs)
            peers=serial_users.get(serial,set()) if serial else set()
            external=len(peers)>1 or len({bb for bb,_,_ in attached})>1
            sync=any(bbport in _node_sync_ports(records,bb) for bb,bbport,_ in attached)
            expected=None
            if len(set(flags))>1:
                issues.append('LLD sharing declarations conflict for this physical radio')
            elif flags and not issues:
                expected=flags[0]
                if not expected and (external or sync):
                    issues.append('LLD says not shared but dump has external sharing/sync evidence')
                    expected=None
                elif expected and not (external or sync):
                    issues.append('LLD says shared but external use/sync cannot be verified from dump')
                    expected=None
            elif external and not lld_path and attached:
                expected=True
                # Positive cross-NE serial evidence can establish sharing without LLD.
                issues=[i for i in issues if 'LLD row missing/ambiguous' not in i]
            if expected is None and lld_error:
                issues.append(lld_error)
            actual=_bool(_attr(target_attrs,'isSharedWithExternalMe'))
            status='NotFound' if expected is None or issues or actual is None else ('Match' if actual==expected else 'Mismatch')
            if actual is None:
                issues.append('isSharedWithExternalMe missing or invalid')
            remark='; '.join(issues) if issues else 'External radio sharing flag agrees with available topology evidence and LLD when supplied.'
            remark+='\n'+'\n'.join(details)
            remark+=f'\nSerial: {serial or "unavailable"}; connected NEs: {", ".join(sorted(peers)) or "unverified"}; sync evidence: {sync}.'
            remark+='\nReport only. Dump configuration does not establish restart/effective runtime state.'
            out.append(AuditResult('radio-sharing',node,target_mo,'isSharedWithExternalMe',
                       str(expected).lower() if expected is not None else '(unavailable)',
                       str(actual).lower() if actual is not None else '(unavailable)',status,
                       'LLD CPRI connectivity + RiLink/FRU serial + NodeGroupSyncMember',node,remark=remark))
    return out
