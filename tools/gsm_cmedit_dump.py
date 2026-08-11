#!/usr/bin/python3
"""
NodeCraft GSM BSC cmedit dump — run this on the ENM scripting host.

Enter the SITE ID (e.g. MIN340). The script collects every GSM GeranCell
(+ child MO) parameter the CDD audit needs, scoped PRECISELY to that site
(band-anchored M<digits>8*/M<digits>9*, so MIN3407 / MIN3405 are excluded),
and writes them to <SITEID>_gsm_cmedit_<timestamp>.txt.

Upload that .txt in NodeCraft → CDD Audit → "GSM cmedit log" to run the GSM
audit offline (no SSH from the tool needed).
"""
import enmscripting
import sys
import re
import datetime

# (MO, command) pairs — ``__PFX__`` is replaced with each band-anchored prefix.
COMMANDS = [
    ('GeranCell', 'cmedit get *BS* GeranCell.(gerancellid==__PFX__*,cgi,ncc,bcc,bcchNo,userLabel,rac,cSysType,bcchType,irc) -t'),
    ('IdleModeAndPaging', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,IdleModeAndPaging.(mFrms,t3212,att,maxRet,accMin,crh,cb,cro,to,pt,cre,nccPerm) -t'),
    ('PowerControlUplink', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PowerControlUplink.(msTxPwr,msrPwrOffset,cchPwr,dtxU,bsRxMin,msRxMin,bsRxSuff,ssDesUl,qDesUl,lCompUl,qCompUl) -t'),
    ('PowerControlDownlink', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PowerControlDownlink.(bsRPwrOffset,dlPcE,dlPcE2a,initDlPcE,initDlPcE2a,initDlPcG,dtxD,dlPcG,bsTxPwr,bsPwr) -t'),
    ('DynamicFrHrModeAdaption', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,DynamicFrHrModeAdaption.(dmQb,dmQbAmr,dmQbNAmr,dmQg,dmQgAmr,dmQgNAmr,dmtFAmr,dmtFNAmr,dmtHAmr,dmtHNAmr,dmPr) -t'),
    ('InterRanMobility', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,InterRanMobility.(ratPrio,prioThr,hPrio,tres,isHoLev,sPrio,qsc,qsci) -t'),
    ('RadioLinkTimeout', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,RadioLinkTimeout.(rLinkTaHr,rLinkUpAfr,rLinkUpAhr,rLinkTAwb,rLinkUpAwb,rLinkT,rLinkUp) -t'),
    ('ChannelAllocAndOpt', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,ChannelAllocAndOpt.(chap,fPdch,sPdch,csPsAlloc,gprsPrio,qtaStatus) -t'),
    ('Mobility', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,Mobility.(aw,mbcr,maxTa,emrState) -t'),
    ('HierarchicalCellStructure', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,HierarchicalCellStructure.(layer,layerThr) -t'),
    ('PowerControl', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PowerControl.(amrPcState,hpbState) -t'),
    ('Dtm', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,Dtm.(dtmState) -t'),
    ('MsQueuing', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,MsQueuing.(qLength) -t'),
    ('ChannelGroup', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,ChannelGroup.(sdcch,numReqBpc,dchNo,hsn,hop,maio) -t'),
    ('PhysicalData', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PhysicalData.(latitude,longitude) -t')
]

siteid = input("Enter SITE ID (e.g. MIN340): ").strip()
m = re.match(r"([A-Za-z])[A-Za-z]*(\d+)", siteid)
if not m:
    print("Invalid SITE ID — expected letters+digits like MIN340.")
    sys.exit(1)
cellbase = m.group(1) + m.group(2)              # MIN340 -> M340
# GSM cell id = <letter><siteDigits>[89]<sectorLetter>; anchoring the band digit
# scopes queries to THIS site (M3408*/M3409*).
prefixes = [cellbase + "8", cellbase + "9"]
# The band-prefix wildcard still can't exclude a LONGER-numbered neighbour whose
# site prefix equals ours+band — e.g. auditing MIN278 (M2788*) also matches
# MIN2788's cells M2788'9'S…  So keep only rows whose cell id has a NON-DIGIT
# (the sector letter S/L/R) right after the band digit — MIN2788 has a digit
# there, so it's dropped; MIN278's own M278[89]S… stay.
site_rx = re.compile("^" + re.escape(cellbase) + r"[89][A-Za-z]")
cell_rx = re.compile(r"\bM\d+[A-Za-z]\d*\b")


def _belongs(line):
    """True if the line has no cell id, or at least one that is THIS site's."""
    toks = cell_rx.findall(line)
    return (not toks) or any(site_rx.match(t) for t in toks)

stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
outname = "%s_gsm_cmedit_%s.txt" % (siteid, stamp)

session = enmscripting.open()
terminal = session.terminal()
n_ok = 0
with open(outname, "w") as fh:
    fh.write("# NodeCraft GSM cmedit dump | SITE=%s | %s\n\n" % (siteid, stamp))
    for mo, tmpl in COMMANDS:
        for pfx in prefixes:
            cmd = tmpl.replace("__PFX__", pfx)
            fh.write("##### NODECRAFT-CMEDIT MO=%s CMD=%s #####\n" % (mo, cmd))
            try:
                res = terminal.execute(cmd)
                if res.is_command_result_available():
                    for line in res.get_output():
                        if _belongs(line):        # drop foreign-site rows
                            fh.write(line + "\n")
                    n_ok += 1
                else:
                    fh.write("# (no command result)\n")
            except Exception as exc:
                fh.write("# ERROR: %s\n" % exc)
            fh.write("\n")

enmscripting.close(session)
print("Done. %d query block(s) written to: %s" % (n_ok, outname))
print("Upload this file in NodeCraft → CDD Audit → GSM cmedit log.")
