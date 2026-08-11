#!/usr/bin/python3
"""
NodeCraft GSM BSC cmedit dump — run this on the ENM scripting host.

Enter the SITE ID (e.g. MIN340). The script first checks the site has GSM
cells: green "Cell found" → it proceeds; red "CELL NOT FOUND" → it stops
immediately. It then collects every GSM GeranCell (+ child MO) parameter the
CDD audit needs, scoped PRECISELY to that site (band-anchored
M<digits>8*/M<digits>9*, so MIN3407 / MIN3405 are excluded), shows a CLI
progress bar, and writes the result to
/home/shared/<user>/AUDIT/<SITEID>_gsm_cmedit_<timestamp>.txt.

Upload that .txt in NodeCraft → CDD Audit → "GSM cmedit log" to run the GSM
audit offline (no SSH from the tool needed).
"""
import enmscripting
import sys
import os
import re
import getpass
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

# Save under /home/shared/<user>/AUDIT/ (created if needed).
save_dir = "/home/shared/%s/AUDIT" % getpass.getuser()
try:
    os.makedirs(save_dir)
except OSError:
    pass                       # already exists (py2/py3-safe, no exist_ok)
outname = os.path.join(save_dir, "%s_gsm_cmedit_%s.txt" % (siteid, stamp))

total = len(COMMANDS) * len(prefixes)


def _progress(done, total, label=""):
    """In-place CLI progress bar, e.g. [########----------]  40%  (12/30) MO."""
    width = 30
    frac = float(done) / total if total else 1.0
    filled = int(round(width * frac))
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write("\r[%s] %3d%%  (%d/%d)  %-24s"
                     % (bar, int(round(frac * 100)), done, total, label))
    sys.stdout.flush()


# ── Terminal colours ───────────────────────────────────────────────
GREEN, RED, BOLD, RESET = "\033[92m", "\033[91m", "\033[1m", "\033[0m"

session = enmscripting.open()
terminal = session.terminal()


def _site_has_cells():
    """Quick pre-check: does this site have any GeranCell? Returns the first
    matching cell id, else None."""
    for pfx in prefixes:
        cmd = "cmedit get *BS* GeranCell.(gerancellid==%s*,cgi) -t" % pfx
        try:
            res = terminal.execute(cmd)
        except Exception:
            continue
        if res.is_command_result_available():
            for line in res.get_output():
                for tok in cell_rx.findall(line):
                    if site_rx.match(tok):
                        return tok
    return None


print("Searching GSM cells for %s ..." % siteid)
_found = _site_has_cells()
if not _found:
    print("%s%s CELL NOT FOUND for %s %s" % (BOLD, RED, siteid, RESET))
    enmscripting.close(session)
    sys.exit(1)
print("%s%s Cell found: %s — collecting ...%s" % (BOLD, GREEN, _found, RESET))

n_ok = 0
done = 0
print("Collecting GSM cmedit for %s -> %s" % (siteid, outname))
with open(outname, "w") as fh:
    fh.write("# NodeCraft GSM cmedit dump | SITE=%s | %s\n\n" % (siteid, stamp))
    for mo, tmpl in COMMANDS:
        for pfx in prefixes:
            cmd = tmpl.replace("__PFX__", pfx)
            _progress(done, total, "%s %s*" % (mo, pfx))
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
            done += 1
            _progress(done, total, "%s %s*" % (mo, pfx))
    sys.stdout.write("\n")

enmscripting.close(session)
print("Done. %d query block(s) written to: %s" % (n_ok, outname))
print("Upload this file in NodeCraft → CDD Audit → GSM cmedit log.")
