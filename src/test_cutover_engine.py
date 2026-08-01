"""
Cut Over engine smoke test — drives the whole state machine against a fake
node, so no SSH gateway, no hardware and no GUI are involved.

The fake node speaks the **real `stzrc` format** (`;`-delimited LTECell /
NRCell tables with `S` and `UEs` columns), because that is what the engine
now polls for both cell state and traffic.

    python3 src/test_cutover_engine.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cutover_runner
from cutover_model import CellStatus, GroupStatus, RunPhase, UNMAPPED

_failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        _failures.append(name)


# Band 99 is deliberately absent from LTE_FREQ_BAND_MAP — the unmapped case.
LTE_BANDS = """MO                                freqBand
EUtranCellFDD=SITEA-11    ;8
EUtranCellFDD=SITEA-12    ;28
EUtranCellFDD=SITEA-21    ;3
EUtranCellFDD=SITEA-22    ;1
EUtranCellTDD=SITEA-31    ;40
EUtranCellFDD=SITEA-98    ;99

6 MOs found
"""

NR_BANDS = """MO                       bandListManual
NRCellDU=SITEA-401  ;i[1] = 41
                    ;i[2] = 78

1 MOs found
"""


class FakeSSH:
    """Stand-in for IntegrationSSH that speaks the real `stzrc` table format."""

    def __init__(self, enable_after=1, traffic_after=2, reject_traffic=False,
                 no_cell_table=False, pre_unlocked=(), barred=(),
                 blip_on=None):
        self.enable_after = enable_after
        self.traffic_after = traffic_after
        self.reject_traffic = reject_traffic
        self.no_cell_table = no_cell_table
        self.barred = set(barred)
        #: When set, UEs is non-zero on exactly this one poll and zero on every
        #: other — a transient that must NOT be accepted as real traffic.
        self.blip_on = blip_on
        self.stzrc_calls = 0
        self.sent = []
        self.unlocked = set(pre_unlocked)     # cells currently UNLOCKED
        self.pre_unlocked = set(pre_unlocked)
        self.all_cells = ["SITEA-11", "SITEA-12", "SITEA-21", "SITEA-22",
                          "SITEA-31", "SITEA-98", "SITEA-401"]
        self.shell = None
        self.client = None

    def connect(self, timeout=30): pass
    def disconnect(self): pass
    def enter_amos(self, node, timeout=90): return ""
    def exit_amos(self): return ""
    def set_live_sink(self, fn): pass
    def start_step_log(self, path): pass
    def stop_step_log(self): pass

    def run_amos_set_with_confirm(self, command, node, answer="y", timeout=60):
        return self.run_amos_command_safe(command, node, timeout)

    def _mo_of(self, dn):
        if dn.endswith("401"):
            return "DU"
        return "TDD" if dn.endswith("31") else "FDD"

    def _stzrc(self):
        self.stzrc_calls += 1
        if self.no_cell_table:
            return "no tables here\n"
        up_now = self.stzrc_calls > self.enable_after
        if self.blip_on is not None:
            has_traffic = self.stzrc_calls == self.blip_on
        else:
            has_traffic = self.stzrc_calls > self.traffic_after

        lte, nr = [], []
        for dn in self.all_cells:
            mo = self._mo_of(dn)
            # Pre-unlocked cells are up from the very first poll.
            if dn in self.pre_unlocked:
                state, ue = "1", 42
            elif dn in self.unlocked:
                state = "1" if up_now else "L"
                ue = 7 if (up_now and has_traffic) else 0
            else:
                state, ue = "L", 0
            row = (f" 1 ;{mo}=CCL_{dn} ;{state} ;-------- ;  - ; {ue:>3} ; 22 "
                   f";44805 ;305 ;817 ;171302166 ;2000 ;20000 ;2115 ;1715 "
                   f";10 ;10 ;   4 ;120 ;120 ;4/4 ;4/4 ;0 ;35 ;- ;8")
            (nr if mo == "DU" else lte).append(row)

        def _table(title, rows):
            hdr = (f"Id ;{title} ;S ;TABREMDF ;Alm ; UEs ;cId ;  tac ;pci ;rsi "
                   f";      eci ;arfcnDL ;arfcnUL ;freqDL ;freqUL ;dlBW ;ulBW "
                   f";Band ;cnfP ;maxP ;C-T/R ;U-T/R ;M-T ;Rng ;Ess ;Fru")
            up = sum(1 for r in rows if r.split(";")[2].strip() == "1")
            return ("=" * 100 + f"\n{hdr}\n" + "=" * 100 + "\n"
                    + "\n".join(rows) + "\n" + "-" * 100
                    + f"\nTotal: {len(rows)} Cells ({up} up)\n")

        return ("Collecting Alarms...\n" + _table("LTECell", lte)
                + "\n" + _table("NRCell", nr))

    def run_amos_command_safe(self, command, node, timeout=120, in_amos=True):
        self.sent.append(command)
        c = command.strip()

        if c.startswith("hgetc") and "eutrancell" in c.lower():
            return LTE_BANDS
        if c.startswith("hgetc") and "nrcelldu" in c.lower():
            return NR_BANDS
        if c.startswith("hget") and "cellbarred" in c.lower():
            dn = c.split("=", 1)[1].split()[0]
            short = dn.replace("CCL_", "")
            return (f"{dn} cellBarred BARRED\n" if short in self.barred
                    else f"{dn} cellBarred NOT_BARRED\n")
        if c.startswith("ldeb"):
            self.unlocked.add(c.split("=", 1)[1].strip().replace("CCL_", ""))
            return "Setting administrativeState=UNLOCKED\n1 MOs set\n"
        if c.startswith("bl "):
            self.unlocked.discard(c.split("=", 1)[1].strip().replace("CCL_", ""))
            return "Setting administrativeState=LOCKED\n1 MOs set\n"
        if c.startswith("st B"):
            return (" Proxy(MO)          AdmState  OpState\n"
                    " Carrier=B4         UNLOCKED  ENABLED\n")
        if c.startswith("stzrc"):
            if self.reject_traffic:
                return "stzrc\nUnknown command: stzrc\n"
            return self._stzrc()
        if c.startswith("st "):
            return " Proxy(MO)  AdmState OpState AvailStatus\n\n0 MOs found\n"
        if c.startswith("alt"):
            return ("=============  ACTIVE ALARMS  =============\n"
                    "*** No Active alarms ***\n")
        return ""


def build_engine(tmpdir, fake, **over):
    cfg = cutover_runner.load_cutover_config(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    cfg["enable_poll"]["interval_s"] = 0.02
    cfg["enable_poll"]["timeout_s"] = 4
    cfg["enable_poll"]["backoff_after_s"] = 999
    cfg["traffic"]["interval_s"] = 0.02
    cfg["traffic"]["timeout_s"] = 4
    cfg["unlock"]["inter_command_delay_s"] = 0
    cfg["diagnosis"]["command_timeout_s"] = 5
    cfg["require_confirmation"] = False
    for k, v in over.items():
        section, _, key = k.partition("__")
        if key:
            cfg[section][key] = v
        else:
            cfg[section] = v

    form = {"shortcode": "TEST", "node_name": "NODEA", "host": "h",
            "port": 5023, "username": "u", "password": "p"}
    eng = cutover_runner.CutoverEngine(form, cfg=cfg, log_dir=tmpdir)
    eng._connect_node = lambda name: _session(name, fake)
    return eng


def _session(name, fake):
    from cutover_model import NodeSession
    s = NodeSession(node_name=name, ssh=fake)
    s.connected = True
    s.in_amos = True
    return s


def wait_idle(eng, timeout=30):
    t0 = time.time()
    while eng.is_busy() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return not eng.is_busy()


tmpdir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "cutover_test_logs")

# ──────────────────────────────────────────────────────────────────
print("\n[1] discovery reads state from the stzrc tables")
fake = FakeSSH()
eng = build_engine(tmpdir, fake)
eng.start_discovery()
check("discovery finished", wait_idle(eng))
run = eng.run
check("phase READY", run.phase == RunPhase.READY, str(run.phase))
check("7 cells discovered", len(run.cells) == 7, str(len(run.cells)))
check("LB = 900 + 700", len(run.cells_of("LB")) == 2,
      str([c.cell_dn for c in run.cells_of("LB")]))
check("MB = 1800 + 2100", len(run.cells_of("MB")) == 2)
check("HB = 2300 + NR2600", len(run.cells_of("HB")) == 2)
check("band 99 -> UNMAPPED", len(run.cells_of(UNMAPPED)) == 1,
      str([c.band_key for c in run.cells_of(UNMAPPED)]))
check("UNMAPPED not unlockable", run.unlockable_cells_of(UNMAPPED) == [])
check("alarm baseline captured", "NODEA" in run.alarm_baseline)
check("pre-state ran before any unlock",
      not any(s.startswith("ldeb") for s in fake.sent))
check("nothing wrongly flagged already-in-service",
      not any(c.already_in_service for c in run.cells))

# ──────────────────────────────────────────────────────────────────
print("\n[2] unlock LB — happy path via stzrc")
eng.unlock_group("LB")
check("LB finished", wait_idle(eng))
lb = run.cells_of("LB")
check("both LB cells TRAFFIC_OK",
      all(c.status == CellStatus.TRAFFIC_OK for c in lb),
      str([(c.cell_dn, c.status.value) for c in lb]))
check("group LB DONE", run.groups["LB"].status == GroupStatus.DONE)
check("one ldeb per LB cell",
      sum(1 for s in fake.sent if s.startswith("ldeb")) == 2)
check("no wildcard in any unlock command",
      all("*" not in s for s in fake.sent if s.startswith("ldeb")))
check("MB untouched", all(c.status == CellStatus.PENDING
                          for c in run.cells_of("MB")))
check("UE count read from the UEs column", all(c.ue_peak == 7 for c in lb),
      str([c.ue_peak for c in lb]))
check("UNMAPPED cell never unlocked",
      not any("SITEA-98" in s for s in fake.sent if s.startswith("ldeb")))

# ──────────────────────────────────────────────────────────────────
print("\n[3] rollback touches only what this run unlocked")
before = [s for s in fake.sent if s.startswith("bl ")]
eng.relock_group("LB")
check("rollback finished", wait_idle(eng))
bls = [s for s in fake.sent if s.startswith("bl ")]
check("bl sent for exactly the 2 LB cells", len(bls) - len(before) == 2, str(bls))
check("LB cells marked RELOCKED",
      all(c.status == CellStatus.RELOCKED for c in run.cells_of("LB")),
      str([c.status.value for c in run.cells_of("LB")]))
check("no bl for cells never unlocked",
      not any("SITEA-21" in s or "SITEA-98" in s for s in bls))
check("relockable set is now empty", run.relockable_cells_of("LB") == [])

# ──────────────────────────────────────────────────────────────────
print("\n[4] a cell already in service is never touched")
fake4 = FakeSSH(pre_unlocked=["SITEA-11"])
eng4 = build_engine(tmpdir, fake4)
eng4.start_discovery(); wait_idle(eng4)
r4 = eng4.run
already = [c for c in r4.cells if c.already_in_service]
check("pre-unlocked cell detected", len(already) == 1 and
      already[0].cell_dn == "SITEA-11", str([c.cell_dn for c in already]))
check("marked ALREADY_IN_SERVICE",
      already[0].status == CellStatus.ALREADY_IN_SERVICE)
check("excluded from unlockable", already[0] not in r4.unlockable_cells_of("LB"))
check("excluded from relockable", not already[0].is_relockable)

eng4.unlock_group("LB"); wait_idle(eng4)
check("no ldeb for the in-service cell",
      not any("SITEA-11" in s for s in fake4.sent if s.startswith("ldeb")),
      str([s for s in fake4.sent if s.startswith('ldeb')]))
eng4.relock_all(); wait_idle(eng4)
check("rollback never locks the in-service cell",
      not any("SITEA-11" in s for s in fake4.sent if s.startswith("bl ")),
      str([s for s in fake4.sent if s.startswith('bl ')]))
check("in-service cell still ALREADY_IN_SERVICE",
      already[0].status == CellStatus.ALREADY_IN_SERVICE)

# ──────────────────────────────────────────────────────────────────
print("\n[5] a barred cell fails fast instead of burning the timeout")
fake5 = FakeSSH(barred=["SITEA-11"])
eng5 = build_engine(tmpdir, fake5)
eng5.start_discovery(); wait_idle(eng5)
t0 = time.time()
eng5.unlock_group("LB")
check("LB finished", wait_idle(eng5))
elapsed = time.time() - t0
lb5 = {c.cell_dn: c for c in eng5.run.cells_of("LB")}
check("barred cell marked BARRED",
      lb5["SITEA-11"].status == CellStatus.BARRED,
      lb5["SITEA-11"].status.value)
check("barred cell never claimed as traffic_ok",
      lb5["SITEA-11"].status != CellStatus.TRAFFIC_OK)
check("its sibling still reaches TRAFFIC_OK",
      lb5["SITEA-12"].status == CellStatus.TRAFFIC_OK,
      lb5["SITEA-12"].status.value)
check("did not wait out the full traffic timeout", elapsed < 8, f"{elapsed:.1f}s")

# ──────────────────────────────────────────────────────────────────
print("\n[6] enable timeout is diagnosed, not silent")
fake6 = FakeSSH(enable_after=10_000)
eng6 = build_engine(tmpdir, fake6)
eng6.start_discovery(); wait_idle(eng6)
eng6.unlock_group("LB")
check("LB finished", wait_idle(eng6))
lb6 = eng6.run.cells_of("LB")
check("no false traffic_ok",
      not any(c.status == CellStatus.TRAFFIC_OK for c in lb6))
check("radio status was consulted",
      any(s.startswith("st B") for s in fake6.sent),
      str([s for s in fake6.sent if s.startswith('st B')]))
check("group FAILED", eng6.run.groups["LB"].status == GroupStatus.FAILED)
check("cells still relockable after a failed unlock",
      len(eng6.run.relockable_cells_of("LB")) == 2)

# ──────────────────────────────────────────────────────────────────
print("\n[7] a transient UE blip does not confirm traffic")
# UEs is non-zero on exactly one poll, then back to zero — the case the
# consecutive-sample latch exists to reject.
fake7 = FakeSSH(blip_on=3)
eng7 = build_engine(tmpdir, fake7, traffic__required_consecutive_samples=2)
eng7.start_discovery(); wait_idle(eng7)
eng7.unlock_group("LB")
check("LB finished", wait_idle(eng7))
lb7 = eng7.run.cells_of("LB")
check("a single blip is not accepted as traffic",
      not any(c.status == CellStatus.TRAFFIC_OK for c in lb7),
      str([(c.status.value, c.traffic_samples) for c in lb7]))
check("timed out honestly instead",
      all(c.status == CellStatus.TRAFFIC_TIMEOUT for c in lb7),
      str([c.status.value for c in lb7]))
check("the blip was seen (peak recorded) but not trusted",
      all(c.ue_peak >= 1 for c in lb7), str([c.ue_peak for c in lb7]))

# Sustained traffic across consecutive polls IS accepted.
fake7b = FakeSSH(traffic_after=2)
eng7b = build_engine(tmpdir, fake7b, traffic__required_consecutive_samples=2)
eng7b.start_discovery(); wait_idle(eng7b)
eng7b.unlock_group("LB"); wait_idle(eng7b)
check("sustained traffic is accepted",
      all(c.status == CellStatus.TRAFFIC_OK for c in eng7b.run.cells_of("LB")),
      str([(c.status.value, c.traffic_samples)
           for c in eng7b.run.cells_of("LB")]))

# ──────────────────────────────────────────────────────────────────
print("\n[8] traffic command rejected by moshell")
fake8 = FakeSSH(reject_traffic=True)
eng8 = build_engine(tmpdir, fake8)
eng8.start_discovery(); wait_idle(eng8)
eng8.unlock_group("LB")
check("LB finished", wait_idle(eng8))
check("no cell claimed traffic",
      not any(c.status == CellStatus.TRAFFIC_OK
              for c in eng8.run.cells_of("LB")),
      str([c.status.value for c in eng8.run.cells_of("LB")]))

# ──────────────────────────────────────────────────────────────────
print("\n[9] dry run sends nothing")
fake9 = FakeSSH()
eng9 = build_engine(tmpdir, fake9, dry_run=True)
eng9.start_discovery(); wait_idle(eng9)
eng9.unlock_group("LB"); check("LB finished", wait_idle(eng9))
check("no ldeb sent", not any(s.startswith("ldeb") for s in fake9.sent))
eng9.relock_group("LB"); wait_idle(eng9)
check("no bl sent either", not any(s.startswith("bl ") for s in fake9.sent))

# ──────────────────────────────────────────────────────────────────
print("\n[10] confirmation gates both unlock and rollback")
fake10 = FakeSSH()
eng10 = build_engine(tmpdir, fake10, require_confirmation=True)
eng10._confirm_cb = lambda group, lines: False
eng10.start_discovery(); wait_idle(eng10)
eng10.unlock_group("LB"); wait_idle(eng10)
check("declining sends nothing",
      not any(s.startswith("ldeb") for s in fake10.sent))
check("cells stay PENDING",
      all(c.status == CellStatus.PENDING for c in eng10.run.cells_of("LB")))

shown = {}
eng10._confirm_cb = lambda g, l: shown.update(g=g, l=l) or True
eng10.unlock_group("MB"); wait_idle(eng10)
check("dialog lists literal unlock commands",
      any(x.startswith("ldeb EUtranCellFDD=") for x in shown.get("l", [])),
      str(shown.get("l"))[:120])
eng10.relock_group("MB"); wait_idle(eng10)
check("rollback dialog is labelled as a roll back",
      "ROLL BACK" in shown.get("g", ""), shown.get("g"))
check("rollback dialog lists literal lock commands",
      any(x.startswith("bl EUtranCellFDD=") for x in shown.get("l", [])),
      str(shown.get("l"))[:120])

# ──────────────────────────────────────────────────────────────────
print("\n[11] EN-DC: NR unlocked with no LTE anchor warns")
fake11 = FakeSSH()
eng11 = build_engine(tmpdir, fake11)
eng11.start_discovery(); wait_idle(eng11)
while not eng11.event_queue.empty():
    eng11.event_queue.get_nowait()
eng11.unlock_group("HB")      # HB holds the NR2600 cell; no LTE is up yet
wait_idle(eng11)
evs = []
while not eng11.event_queue.empty():
    evs.append(eng11.event_queue.get_nowait())
check("anchor warning emitted",
      any(e.kind == "diagnostic" and "anchor" in e.message.lower() for e in evs),
      str([(e.kind, e.message[:40]) for e in evs]))

# ──────────────────────────────────────────────────────────────────
print("\n[12] final verification")
fake12 = FakeSSH()
eng12 = build_engine(tmpdir, fake12)
eng12.start_discovery(); wait_idle(eng12)
eng12.run_final_verification()
check("verification finished", wait_idle(eng12))
check("phase DONE", eng12.run.phase == RunPhase.DONE, str(eng12.run.phase))
check("configured steps ran",
      all(s.status in ("pass", "fail") for s in eng12.run.final_steps),
      str([(s.key, s.status) for s in eng12.run.final_steps]))

# ──────────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("All cut-over engine checks passed.")
