"""Local sdir/sdirc summary, adapted from the user supplied Perl parser."""
import re
from terminal_renderer import strip_ansi

RED='\x1b[1;31m'
YELLOW='\x1b[1;33m'
GREEN='\x1b[1;32m'
CYAN='\x1b[1;36m'
RESET='\x1b[0m'


def build_sdir_summary(output,node):
    lines=strip_ansi(output).replace('\r','').splitlines()
    board,radio,vswr,optical,rates={},{},{},{},{}
    clock=''
    sync=''
    cpri=''
    headers=None
    table=None
    for line in lines:
        match=re.search(r'(\d+\s+CPRI\s+links\s*\([^)]*\))',line)
        if match and not cpri:
            cpri=match[1]
        match=re.search(r'radioClockState\s*:\s*(\S+)',line)
        if match and not clock:
            clock=match[1]
        if ';' in line:
            fields=[f.strip() for f in line.split(';')]
            upper=[f.upper() for f in fields]
            new=None
            if 'FRU' in upper and 'BOARD' in upper and any('PRODUCTNUMBER' in h for h in upper):
                new='hardware'
            elif 'FRU' in upper and 'RF' in upper and any('VSWR' in h for h in upper):
                new='vswr'
            elif 'ID' in upper and 'RIL' in upper and any(h.startswith('ISSUE') for h in upper):
                new='summary'
            elif 'ID' in upper and 'RIL' in upper and 'DLLOSS' in upper and 'ULLOSS' in upper:
                new='optical'
            elif 'ID' in upper and 'RIL' in upper and 'BPBP' in upper and upper.count('RATE')>=2:
                new='rate'
            elif 'SYNCREFTYPE' in upper:
                new='sync'
            if new:
                table,headers=new,upper
                continue
            if headers and table:
                def get(name,prefix=False):
                    indices=[i for i,h in enumerate(headers) if h.startswith(name)] if prefix else [i for i,h in enumerate(headers) if h==name]
                    return fields[indices[0]] if indices and indices[0]<len(fields) else ''
                if table=='hardware' and get('FRU'):
                    board[get('FRU')]=get('BOARD')
                elif table=='summary' and re.match(r'^\d+$',fields[0]):
                    name=get('RIL')
                    boards=get('BOARD1',True).split()
                    if name:
                        radio.setdefault(name,{'board':boards[-1] if boards else '', 'issue':get('ISSUE',True) or 'NA'})
                elif table=='vswr' and re.fullmatch('[A-Z]',get('RF')):
                    value=re.sub(r'\s*\(.*','',get('VSWR',True)).strip() or '-'
                    vswr.setdefault(get('FRU'),{})[get('RF')]=value
                elif table=='optical' and re.match(r'^\d+$',fields[0]):
                    optical[get('RIL')]={n:get(n) for n in ('DLLOSS','ULLOSS','BER1','BER2')}
                elif table=='rate' and re.match(r'^\d+$',fields[0]):
                    values=[fields[i] if i<len(fields) else '' for i,h in enumerate(headers) if h=='RATE']
                    rates[get('RIL')]=values[:2]
                elif table=='sync' and re.match(r'^\*?\d+$',fields[0]) and not sync:
                    sync=get('SYNCREFTYPE')
        elif re.match(r'^\s*-{5,}',line):
            table=headers=None
        elif re.match(r'^\s*\d+\s+\S+\s+O\d+\s+',line):
            name=line.split()[1]
            model=re.search(r'\bRAN\w+\s+(\S+)',line) or re.search(r'\b((?:RRU|AIR|Radio)\S+)',line)
            issue=re.search(r'\)\s+([^()]+?)\s*$',line)
            radio.setdefault(name,{'board':model[1] if model else '', 'issue':issue[1] if issue else 'NA'})
    if not radio and not vswr and not cpri:
        return None
    # RF data remains useful even when the connectivity summary is absent.
    for name in vswr:
        radio.setdefault(name,{'board':'','issue':'Unreadable'})
    def color(text,code):
        return code+text+RESET
    def numeric(value):
        return float(value) if re.fullmatch(r'-?\d+(?:\.\d+)?',value or '') else None
    def rru_type(name,data):
        value=board.get(name) or data['board']
        if not value or value.startswith('(') or re.match(r'^RAN[A-Z]',value,re.I):
            return 'N/A'
        model=re.match(r'^([A-Za-z]+\d+(?:[A-Za-z]{2}(?=\d))?)',value)
        band=name.split('_RRU')[0].split('_')[-1] if '_RRU' in name else name
        return (model[1] if model else value)+' '+band
    def is_aas(name,data):
        product=board.get(name) or data['board']
        return bool(re.match(r'^AIR\d+',product,re.I) or
                    (not product and re.match(r'^AAS_',name,re.I)))
    aas={name for name,data in radio.items() if is_aas(name,data)}
    # Missing VSWR is expected on AAS. Include their ports only if a real
    # numeric reading exists, so any actual high measurement is still reported.
    ports=sorted({p for name,values in vswr.items() for p,value in values.items()
                  if name not in aas or numeric(value) is not None})
    def loss_fmt(value):
        return ' '+value if numeric(value) is not None and not value.startswith('-') else value
    rows=[]
    hot=[]
    missing=[]
    for name,data in radio.items():
        rate=rates.get(name,['',''])
        if not any(rate):
            rate_text='-'
        elif not rate[0] or not rate[1] or rate[0]==rate[1]:
            rate_text=rate[0] or rate[1]
        else:
            def rate_number(v):
                m=re.search(r'[\d.]+',v)
                return float(m[0]) if m else 0
            low,top=sorted(rate,key=rate_number)
            rate_text=f'Unmatch Low {low} - Top {top}'
        o=optical.get(name,{})
        ber=[]
        for end in ('1','2'):
            if o:
                value=o.get('BER'+end,'')
                if value not in ('0','0/0'):
                    ber.append(('Unreadable' if not value or 'NA' in value.upper() else value)+f' (BER{end})')
        row=[name,rru_type(name,data),data['issue'],rate_text,
             loss_fmt(o.get('DLLOSS') or '-'),loss_fmt(o.get('ULLOSS') or '-'),', '.join(ber) or '-']
        for port in ports:
            value=vswr.get(name,{}).get(port,'-')
            row.append(value)
            n=numeric(value)
            if n is not None and n>1.4:
                hot.append(f'Port {port} {name} ({value})')
            elif n is None and name not in aas:
                missing.append(f'{name}/{port}')
        rows.append(row)
    columns=['RilinkID','RRU','Fiber Check','LinkRate','DlLoss','UlLoss','BER']+['VSWR-'+p for p in ports]
    keep=[i for i in range(len(columns)) if i not in (3,4,5,6) or any(row[i]!='-' for row in rows)]
    widths={i:max([len(columns[i])]+[len(row[i]) for row in rows]) for i in keep}
    sep='='*max(37,sum(widths.values())+2*(len(keep)-1))
    fiber=cpri or 'N/A'
    fiber=re.sub(r'(\d+)\s+(OKW|NOK|NT)\b',lambda m: color(m[0],YELLOW if m[2]=='OKW' else RED) if int(m[1]) else m[0],fiber)
    result=[f'sdir Summary {node}',sep,'Fiber : '+fiber]
    if hot:
        result.append('VSWR  : '+color(', '.join(hot),RED))
    elif ports and not missing:
        result.append('VSWR  : all ports <= 1.4')
    elif ports:
        result.append('VSWR  : '+color('Unreadable/missing: '+', '.join(missing),YELLOW))
    elif radio and len(aas)==len(radio):
        result.append('VSWR  : N/A (AAS)')
    else:
        result.append('VSWR  : unavailable (no RF port readings)')
    if clock or sync:
        label='GPS' if 'GNSS_RECEIVER' in sync.upper() else sync or 'Sync'
        ok=clock in ('RNT_TIME_LOCKED','TIME_OFFSET_LOCKED','FREQUENCY_LOCKED')
        result.append(f'Sync  : {label} - '+color('OK' if ok else 'Not OK',GREEN if ok else RED)+f' ({clock or "unavailable"})')
    else:
        result.append('Sync  : unavailable')
    if rows and all(row[1]=='N/A' for row in rows):
        result.append('RRU   : '+color('No RRU detected',RED))
    result += [sep,'  '.join(columns[i].ljust(widths[i]) for i in keep),sep]
    for row in rows:
        cells=[]
        for i in keep:
            value=row[i].ljust(widths[i])
            code=''
            if i==2:
                code=GREEN if row[i].lower()=='passed' else YELLOW if row[i].lower().startswith('conditionally passed') else RED
            elif i==3 and row[i].startswith('Unmatch') or i==6 and row[i]!='-':
                code=RED
            elif i in (4,5) and row[i]!='-':
                code=CYAN
            elif i>=7 and numeric(row[i]) is not None and numeric(row[i])>1.4:
                code=RED
            cells.append(color(value,code) if code else value)
        result.append('  '.join(cells))
    result += [sep,'',node+'>']
    return '\n'.join(result)
