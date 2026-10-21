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
audit offline (no SSH from the tool needed). The first block (GeranCellFDN,
no -t) records each cell's full FDN, so the generated scripts use the BSC's
real SubNetwork path.

Each query has a time limit (QUERY_TIMEOUT) and one retry on a fresh session.
Run ``gsmaudit --debug`` (or GSMAUDIT_DEBUG=1) to also write
<output>_debug.log: per-query timing, errors, and the stack of any query that
hangs.
"""
import enmscripting
import sys
import os
import re
import getpass
import datetime
import threading

# A cmedit query that hangs on ENM must not stall the whole dump: each query
# gets QUERY_TIMEOUT seconds, then one retry on a fresh session, then it is
# written as TIMEOUT and the dump moves on.
QUERY_TIMEOUT = 30

try:
    TimeoutError
except NameError:              # Python 2 on older ENM hosts
    class TimeoutError(Exception):
        pass

# (MO, command) pairs — ``__PFX__`` is replaced with each band-anchored prefix.
# GeranCellFDN runs WITHOUT -t on purpose: it prints one "FDN : …" line per
# cell with the BSC's full SubNetwork path, which the generated cmedit/cmbulk
# set commands need (the -t table does not show SubNetwork values).
COMMANDS = [
    ('GeranCellFDN', 'cmedit get *BS* GeranCell.(gerancellid==__PFX__*)'),
    ('GeranCell', 'cmedit get *BS* GeranCell.(gerancellid==__PFX__*,cgi,ncc,bcc,bcchNo,userLabel,rac,cSysType,bcchType,irc) -t'),
    ('IdleModeAndPaging', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,IdleModeAndPaging.(mFrms,t3212,att,maxRet,accMin,crh,cb,cro,to,pt,cre,nccPerm) -t'),
    ('PowerControlUplink', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PowerControlUplink.(msTxPwr,msrPwrOffset,cchPwr,dtxU,bsRxMin,msRxMin,bsRxSuff,ssDesUl,qDesUl,lCompUl,qCompUl) -t'),
    ('PowerControlDownlink', 'cmedit get *BS* GeranCell.gerancellid==__PFX__*,PowerControlDownlink.* -t'),
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

# ── Debug log (gsmaudit --debug, or GSMAUDIT_DEBUG=1) ──────────────
# Writes <output>_debug.log next to the output: every query with timing, line
# counts and errors, and — when a query hangs — the stack of the stuck thread,
# showing exactly where inside enmscripting it is waiting.
DEBUG = ("--debug" in sys.argv[1:]
         or os.environ.get("GSMAUDIT_DEBUG", "").strip() not in ("", "0"))
debugname = outname[:-4] + "_debug.log"
_t0 = datetime.datetime.now()
_dbg_lock = threading.Lock()


def _dbg(msg):
    if not DEBUG:
        return
    now = datetime.datetime.now()
    line = "%s +%7.1fs [%s] %s\n" % (now.strftime("%H:%M:%S.%f")[:-3],
                                       (now - _t0).total_seconds(),
                                       threading.current_thread().name, msg)
    with _dbg_lock:
        with open(debugname, "a") as dfh:      # append + close: survives kill
            dfh.write(line)


def _thread_stack(t):
    """The current Python stack of thread ``t`` (where it is stuck)."""
    import traceback
    frame = sys._current_frames().get(t.ident)
    if frame is None:
        return "  (thread has no frame)"
    return "".join(traceback.format_stack(frame)).rstrip()


if DEBUG:
    import platform
    _dbg("=== gsmaudit debug | site=%s | out=%s" % (siteid, outname))
    _dbg("python %s | host %s | user %s | timeout %ss"
         % (sys.version.split()[0], platform.node(), getpass.getuser(),
            QUERY_TIMEOUT))
    _dbg("enmscripting %s from %s"
         % (getattr(enmscripting, "__version__", "?"),
            getattr(enmscripting, "__file__", "?")))
    print("Debug log: %s" % debugname)

_dbg("opening ENM session #1")
session = enmscripting.open()
terminal = session.terminal()
_sessions = [session]
_dbg("session #1 open")


def _new_terminal():
    """Fresh session after a hung query — the old one may still be busy."""
    global terminal
    _dbg("opening ENM session #%d (previous one may be stuck)" % (len(_sessions) + 1))
    s_ = enmscripting.open()
    _sessions.append(s_)
    terminal = s_.terminal()
    _dbg("session #%d open" % len(_sessions))


_qn = [0]


def _execute(cmd, timeout=QUERY_TIMEOUT):
    """Run one cmedit query with a time limit, INCLUDING reading its output
    (get_output can block too). Returns (available, lines); raises
    TimeoutError when ENM does not answer in time."""
    _qn[0] += 1
    qid = "Q%03d" % _qn[0]
    box = {"stage": "execute"}

    def _run(term=terminal):
        try:
            t1 = datetime.datetime.now()
            res = term.execute(cmd)
            box["t_exec"] = (datetime.datetime.now() - t1).total_seconds()
            box["stage"] = "is_command_result_available"
            avail = res.is_command_result_available()
            lines = []
            if avail:
                box["stage"] = "get_output"
                t2 = datetime.datetime.now()
                lines = list(res.get_output())
                box["t_out"] = (datetime.datetime.now() - t2).total_seconds()
            box["res"] = (avail, lines)
        except Exception as exc:          # surfaced to the caller below
            import traceback
            box["err"] = exc
            box["tb"] = traceback.format_exc().rstrip()

    _dbg("%s START session#%d | %s" % (qid, len(_sessions), cmd))
    t = threading.Thread(target=_run, name=qid)
    t.daemon = True
    t.start()
    t.join(timeout)
    if t.is_alive():
        _dbg("%s TIMEOUT after %ss, stuck in stage '%s'. Stack of the stuck "
             "thread:\n%s" % (qid, timeout, box.get("stage"), _thread_stack(t)))
        raise TimeoutError("no answer from ENM after %ds (stuck in %s)"
                           % (timeout, box.get("stage")))
    if "err" in box:
        _dbg("%s ERROR in stage '%s': %s\n%s"
             % (qid, box.get("stage"), box["err"], box.get("tb", "")))
        raise box["err"]
    avail, lines = box["res"]
    _dbg("%s DONE execute=%.1fs output=%.1fs available=%s lines=%d first=%r"
         % (qid, box.get("t_exec", 0), box.get("t_out", 0), avail, len(lines),
            lines[:2]))
    return avail, lines


def _close_all():
    """Close every session without letting a stuck one block the exit."""
    for i, s_ in enumerate(_sessions, 1):
        t = threading.Thread(target=enmscripting.close, args=(s_,))
        t.daemon = True
        t.start()
        t.join(10)
        _dbg("session #%d close %s" % (i, "hung (left behind)" if t.is_alive() else "ok"))


def _site_has_cells():
    """Quick pre-check: does this site have any GeranCell? Returns the first
    matching cell id, else None."""
    for pfx in prefixes:
        cmd = "cmedit get *BS* GeranCell.(gerancellid==%s*,cgi) -t" % pfx
        try:
            avail, lines = _execute(cmd)
        except TimeoutError:
            _new_terminal()
            continue
        except Exception:
            continue
        for line in lines:
            for tok in cell_rx.findall(line):
                if site_rx.match(tok):
                    return tok
    return None


print("Searching GSM cells for %s ..." % siteid)
_found = _site_has_cells()
if not _found:
    print("%s%s CELL NOT FOUND for %s %s" % (BOLD, RED, siteid, RESET))
    _dbg("pre-check: no cell for prefixes %s - stop" % prefixes)
    _close_all()
    sys.exit(1)
print("%s%s Cell found: %s — collecting ...%s" % (BOLD, GREEN, _found, RESET))
_dbg("pre-check: found %s" % _found)

n_ok = 0
n_timeout = 0
done = 0
print("Collecting GSM cmedit for %s -> %s" % (siteid, outname))
with open(outname, "w") as fh:
    fh.write("# NodeCraft GSM cmedit dump | SITE=%s | %s\n\n" % (siteid, stamp))
    for mo, tmpl in COMMANDS:
        for pfx in prefixes:
            cmd = tmpl.replace("__PFX__", pfx)
            _progress(done, total, "%s %s*" % (mo, pfx))
            fh.write("##### NODECRAFT-CMEDIT MO=%s CMD=%s #####\n" % (mo, cmd))
            result = None
            for attempt in (1, 2):
                try:
                    result = _execute(cmd)
                    break
                except TimeoutError as exc:
                    _new_terminal()          # the old session may be stuck
                    if attempt == 2:
                        n_timeout += 1
                        fh.write("# TIMEOUT: %s (retried once)\n" % exc)
                    else:
                        _progress(done, total, "%s %s* retry" % (mo, pfx))
                except Exception as exc:
                    fh.write("# ERROR: %s\n" % exc)
                    break
            if result is not None:
                avail, lines = result
                if avail:
                    for line in lines:
                        if _belongs(line):        # drop foreign-site rows
                            fh.write(line + "\n")
                    n_ok += 1
                else:
                    fh.write("# (no command result)\n")
            fh.write("\n")
            fh.flush()
            done += 1
            _progress(done, total, "%s %s*" % (mo, pfx))
    sys.stdout.write("\n")

_close_all()
_dbg("=== finished: %d ok, %d timed out" % (n_ok, n_timeout))
print("Done. %d query block(s) written to: %s" % (n_ok, outname))
if n_timeout:
    print("%s%d query block(s) timed out - those parameters will show as not "
          "found in the audit; re-run to collect them.%s"
          % (RED, n_timeout, RESET))
if DEBUG:
    print("Debug log: %s" % debugname)
print("Upload this file in NodeCraft → CDD Audit → GSM cmedit log.")
