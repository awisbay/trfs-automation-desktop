"""
Cut Over engine smoke test — drives the whole state machine against a fake
node, so no SSH gateway, no hardware and no GUI are involved.

    python3 src/test_cutover_engine.py
"""
import os
import sys
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


LTE_BANDS = """MO                                freqBand
EUtranCellFDD=SITEA-11    ;8
EUtranCellFDD=SITEA-12    ;28
EUtranCellFDD=SITEA-21    ;3
EUtranCellFDD=SITEA-22    ;1
EUtranCellTDD=SITEA-31    ;40
EUtranCellFDD=SITEA-98    ;5

6 MOs found
"""

NR_BANDS = """MO                       bandListManual
NRCellDU=SITEA-401  ;i[1] = 41
                    ;i[2] = 78

1 MOs found
"""


class FakeSSH:
    """Minimal stand-in for IntegrationSSH.

    Cells report DISABLED for the first `enable_after` status polls, then
    ENABLED. UE counts stay 0 for the first `traffic_after` traffic polls.
    """

    def __init__(self, enable_after=1, traffic_after=1, reject_traffic=False,
                 ue_header=True):
        self.enable_after = enable_after
        self.traffic_after = traffic_after
        self.reject_traffic = reject_traffic
        self.ue_header = ue_header
        self.status_polls = 0
        self.traffic_polls = 0
        self.sent = []
        self.unlocked = set()
        self.shell = None
        self.client = None

    # lifecycle no-ops
    def connect(self, timeout=30): pass
    def disconnect(self): pass
    def enter_amos(self, node, timeout=90): return ""
    def exit_amos(self): return ""
    def set_live_sink(self, fn): pass
    def start_step_log(self, path): pass
    def stop_step_log(self): pass

    def run_amos_set_with_confirm(self, command, node, answer="y", timeout=60):
        return self.run_amos_command_safe(command, node, timeout)

    def run_amos_command_safe(self, command, node, timeout=120, in_amos=True):
        self.sent.append(command)
        c = command.strip()

        if c.startswith("hgetc") and "eutrancell" in c.lower():
            return LTE_BANDS
        if c.startswith("hgetc") and "nrcelldu" in c.lower():
            return NR_BANDS

        if c.startswith("ldeb"):
            self.unlocked.add(c.split("=", 1)[1].strip())
            return "Setting administrativeState=UNLOCKED\n1 MOs set\n"

        if c.startswith("st cell"):
            self.status_polls += 1
            up = self.status_polls > self.enable_after
            lines = [" Proxy(MO)                AdmState  OpState  AvailStatus"]
            for dn in sorted(self.unlocked):
                mo = "NRCellDU" if dn.endswith("401") else (
                    "EUtranCellTDD" if dn.endswith("31") else "EUtranCellFDD")
                if up:
                    lines.append(f" {mo}={dn}    UNLOCKED ENABLED  null")
                else:
                    lines.append(f" {mo}={dn}    UNLOCKED DISABLED null")
            lines.append(f"\n{len(self.unlocked)} MOs found")
            return "\n".join(lines)

        if c.startswith("stzrc"):
            self.traffic_polls += 1
            if self.reject_traffic:
                return "stzrc\nUnknown command: stzrc\n"
            has = self.traffic_polls > self.traffic_after
            hdr = (" Proxy(MO)                 UE    DL"
                   if self.ue_header else " Proxy(MO)                 XX    DL")
            lines = [hdr]
            for dn in sorted(self.unlocked):
                mo = "NRCellDU" if dn.endswith("401") else (
                    "EUtranCellTDD" if dn.endswith("31") else "EUtranCellFDD")
                lines.append(f" {mo}={dn}    {'7' if has else '0'}   120")
            return "\n".join(lines)

        if c.startswith("alt"):
            return "=============  ACTIVE ALARMS  =============\n*** No Active alarms ***\n"
        return ""


def build_engine(tmpdir, fake, **cfg_over):
    cfg = cutover_runner.load_cutover_config(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    cfg["enable_poll"]["interval_s"] = 0.05
    cfg["enable_poll"]["timeout_s"] = 5
    cfg["enable_poll"]["backoff_after_s"] = 999
    cfg["traffic"]["interval_s"] = 0.05
    cfg["traffic"]["timeout_s"] = 5
    cfg["unlock"]["inter_command_delay_s"] = 0
    cfg["require_confirmation"] = False
    for k, v in cfg_over.items():
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


def wait_idle(eng, timeout=20):
    t0 = time.time()
    while eng.is_busy() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return not eng.is_busy()


TMP = os.environ.get("TMPDIR", "/tmp")
tmpdir = os.path.join(TMP, "cutover_test_logs")

# ──────────────────────────────────────────────────────────────────
print("\n[1] discovery")
fake = FakeSSH()
eng = build_engine(tmpdir, fake)
eng.start_discovery()
check("discovery finished", wait_idle(eng))
run = eng.run
check("phase READY", run.phase == RunPhase.READY, str(run.phase))
check("7 cells discovered", len(run.cells) == 7, str(len(run.cells)))
check("LB has 2 cells (900 + 700)", len(run.cells_of("LB")) == 2,
      str([c.cell_dn for c in run.cells_of("LB")]))
check("MB has 2 cells (1800 + 2100)", len(run.cells_of("MB")) == 2)
check("HB has 2 cells (2300 + NR2600)", len(run.cells_of("HB")) == 2,
      str([c.cell_dn for c in run.cells_of("HB")]))
check("band 5 cell landed in UNMAPPED", len(run.cells_of(UNMAPPED)) == 1)
check("UNMAPPED cell marked SKIPPED",
      run.cells_of(UNMAPPED)[0].status == CellStatus.SKIPPED)
check("UNMAPPED excluded from unlockable", run.unlockable_cells_of(UNMAPPED) == [])

# ──────────────────────────────────────────────────────────────────
print("\n[2] unlock LB — full happy path")
eng.unlock_group("LB")
check("LB finished", wait_idle(eng))
lb = run.cells_of("LB")
check("both LB cells TRAFFIC_OK",
      all(c.status == CellStatus.TRAFFIC_OK for c in lb),
      str([(c.cell_dn, c.status) for c in lb]))
check("group LB DONE", run.groups["LB"].status == GroupStatus.DONE,
      str(run.groups["LB"].status))
check("ldeb sent once per LB cell",
      sum(1 for s in fake.sent if s.startswith("ldeb")) == 2,
      str([s for s in fake.sent if s.startswith('ldeb')]))
check("ldeb used one MO per command",
      all("*" not in s and ".*" not in s
          for s in fake.sent if s.startswith("ldeb")))
check("MB untouched", all(c.status == CellStatus.PENDING
                          for c in run.cells_of("MB")))
check("UE count recorded", all(c.ue_peak >= 1 for c in lb),
      str([c.ue_peak for c in lb]))
check("screenshot rendered", os.path.isfile(run.groups["LB"].screenshot_path or ""),
      run.groups["LB"].screenshot_path)
events = []
while not eng.event_queue.empty():
    events.append(eng.event_queue.get_nowait())
check("handoff event emitted",
      any(e.kind == "handoff" and e.group == "LB" for e in events),
      str([e.kind for e in events]))

# ──────────────────────────────────────────────────────────────────
print("\n[3] unlock All — remaining groups")
eng.unlock_all()
check("unlock all finished", wait_idle(eng, timeout=30))
check("MB all traffic OK",
      all(c.status == CellStatus.TRAFFIC_OK for c in run.cells_of("MB")))
check("HB all traffic OK",
      all(c.status == CellStatus.TRAFFIC_OK for c in run.cells_of("HB")))
check("UNMAPPED cell never unlocked",
      not any("SITEA-98" in s for s in fake.sent if s.startswith("ldeb")))

# ──────────────────────────────────────────────────────────────────
print("\n[4] enable timeout -> partial-success gate")
fake2 = FakeSSH(enable_after=10_000)          # never enables
asked = {"n": 0}
eng2 = build_engine(tmpdir, fake2)
eng2._wait_for_user = lambda msg: (asked.__setitem__("n", asked["n"] + 1), True)[1]
eng2.start_discovery(); wait_idle(eng2)
eng2.unlock_group("LB"); check("LB finished", wait_idle(eng2, timeout=30))
lb2 = eng2.run.cells_of("LB")
check("cells marked ENABLE_TIMEOUT",
      all(c.status == CellStatus.ENABLE_TIMEOUT for c in lb2),
      str([c.status for c in lb2]))
check("group FAILED (nothing came up)",
      eng2.run.groups["LB"].status == GroupStatus.FAILED,
      str(eng2.run.groups["LB"].status))
check("no false traffic_ok",
      not any(c.status == CellStatus.TRAFFIC_OK for c in lb2))

# ──────────────────────────────────────────────────────────────────
print("\n[5] traffic command rejected by moshell")
fake3 = FakeSSH(reject_traffic=True)
eng3 = build_engine(tmpdir, fake3)
eng3.start_discovery(); wait_idle(eng3)
eng3.unlock_group("LB"); check("LB finished", wait_idle(eng3, timeout=30))
lb3 = eng3.run.cells_of("LB")
check("cells reach ENABLED then error, never TRAFFIC_OK",
      all(c.status == CellStatus.ERROR for c in lb3),
      str([c.status for c in lb3]))
check("traffic polled only once (aborted early)",
      fake3.traffic_polls <= 2, str(fake3.traffic_polls))

# ──────────────────────────────────────────────────────────────────
print("\n[6] UE column unreadable -> manual gate, never a fabricated pass")
fake4 = FakeSSH(ue_header=False)
eng4 = build_engine(tmpdir, fake4)
eng4.start_discovery(); wait_idle(eng4)

import threading
gate_seen = threading.Event()

def _watch():
    t0 = time.time()
    while time.time() - t0 < 20:
        while not eng4.event_queue.empty():
            ev = eng4.event_queue.get_nowait()
            if ev.kind == "confirm_traffic":
                gate_seen.set()
                eng4.confirm_traffic(ev.group, False)   # operator says "no"
                return
        time.sleep(0.02)

threading.Thread(target=_watch, daemon=True).start()
eng4.unlock_group("LB")
check("LB finished", wait_idle(eng4, timeout=30))
check("manual traffic gate was raised", gate_seen.is_set())
lb4 = eng4.run.cells_of("LB")
check("declined gate -> TRAFFIC_TIMEOUT, not TRAFFIC_OK",
      all(c.status == CellStatus.TRAFFIC_TIMEOUT for c in lb4),
      str([c.status for c in lb4]))

# ──────────────────────────────────────────────────────────────────
print("\n[7] dry run sends nothing")
fake5 = FakeSSH()
eng5 = build_engine(tmpdir, fake5, dry_run=True)
eng5.start_discovery(); wait_idle(eng5)
eng5.unlock_group("LB"); check("LB finished", wait_idle(eng5, timeout=20))
check("no ldeb command was sent",
      not any(s.startswith("ldeb") for s in fake5.sent),
      str([s for s in fake5.sent if s.startswith('ldeb')]))
check("dry run still reports cells as OK for rehearsal",
      all(c.status == CellStatus.TRAFFIC_OK for c in eng5.run.cells_of("LB")))

# ──────────────────────────────────────────────────────────────────
print("\n[8] confirmation dialog gates execution")
fake6 = FakeSSH()
eng6 = build_engine(tmpdir, fake6, require_confirmation=True)
eng6._confirm_cb = lambda group, lines: False        # operator declines
eng6.start_discovery(); wait_idle(eng6)
eng6.unlock_group("LB"); wait_idle(eng6, timeout=10)
check("declining the dialog sends nothing",
      not any(s.startswith("ldeb") for s in fake6.sent))
check("cells stay PENDING after decline",
      all(c.status == CellStatus.PENDING for c in eng6.run.cells_of("LB")))

shown = {}
eng6._confirm_cb = lambda group, lines: shown.update(g=group, l=lines) or False
eng6.unlock_group("MB"); wait_idle(eng6, timeout=10)
check("dialog lists the literal commands",
      any(l.startswith("ldeb EUtranCellFDD=") for l in shown.get("l", [])),
      str(shown.get("l")))

# ──────────────────────────────────────────────────────────────────
print("\n[9] final verification")
fake7 = FakeSSH()
eng7 = build_engine(tmpdir, fake7)
eng7.start_discovery(); wait_idle(eng7)
eng7.run_final_verification()
check("verification finished", wait_idle(eng7, timeout=20))
check("phase DONE", eng7.run.phase == RunPhase.DONE, str(eng7.run.phase))
check("both configured steps ran",
      all(s.status in ("pass", "fail") for s in eng7.run.final_steps),
      str([(s.key, s.status) for s in eng7.run.final_steps]))

# ──────────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {_failures}")
    sys.exit(1)
print("All cut-over engine checks passed.")
