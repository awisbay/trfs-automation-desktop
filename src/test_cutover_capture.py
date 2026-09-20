"""Cut Over evidence capture: parallel alt/st cell/stzrc screenshots, the live
alarm strip, and BSC traffic (rlcrp over a nested SSH/MML session).

    python -m unittest test_cutover_capture
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bsc_mml
import cutover_runner
from cutover_parsers import parse_bsc_connectivity_ip
from cutover_model import CutoverCell, NodeSession
from cutover_parsers import parse_rlcrp_busy_tch

RLCRP = """<rlcrp:cell=M12098S3;
CELL RESOURCES

CELL      BCCH  CBCH  SDCCH  NOOFTCH    QUEUED  ECBCCH
M12098S3     1     1     15    13-  26       0       0

CHGR
 0
BPC   CHANNEL      CHRATE  SPV    STATE  ICMBAND  CHBAND  64K     USE
60023 SDCCH-352132                IDLE   1        1800
      CBCH-352130                 BUSY            1800
59970 TCH-352150   FR      1,2,   IDLE   1        1800    NONE
                           3,5
60029 BCCH-352124                 BUSY            1800

CHGR
 1
BPC   CHANNEL      CHRATE  SPV    STATE  ICMBAND  CHBAND  64K     USE
59992 TCH-352258   FR      1,2,   BUSY   1        1800    EGPRS   GPRS
                           3,5
      TCH-352257   HR      1,3    LOCK   1        1800
59996 TCH-352255   FR      1,2,   BUSY   1        1800    EGPRS   GPRS
END
"""

ALT_BASE = "1 ;Major ;LinkFailure ;RiLink=1\n"
ALT_NOW = ALT_BASE + "2 ;Critical ;CellDown ;EUtranCellFDD=X-1\n"


class NodeFake:
    """AMOS session stand-in: every command takes ``delay`` seconds."""

    def __init__(self, outputs, delay=0.4, fail=False):
        self.outputs, self.delay, self.fail = outputs, delay, fail
        self.sent = []

    def run_amos_command_safe(self, command, node, timeout=120):
        self.sent.append(command)
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("SSH channel closed")
        return self.outputs.get(command, "")


def build_engine(tmpdir, nodes):
    nodes = list(nodes) or ["MIN823_GINGOOB03"]
    cfg = cutover_runner.load_cutover_config(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    cfg["persistence"]["enabled"] = False
    form = {"shortcode": "MIN823", "node_name": nodes[0],
            "node2_name": nodes[1] if len(nodes) > 1 else "",
            "host": "h", "port": 5023, "username": "u", "password": "p",
            "bsc_name": ""}
    eng = cutover_runner.CutoverEngine(form, cfg=cfg, log_dir=tmpdir)
    eng.run.node_names = list(nodes)
    return eng


def drain(eng, kind):
    events = []
    while True:
        try:
            events.append(eng.event_queue.get_nowait())
        except queue.Empty:
            return [e for e in events if e.kind == kind]


class ParserTests(unittest.TestCase):
    def test_bsc_connectivity_ip_requires_exact_unique_valid_row(self):
        output = (">> cmedit get MINBS01 bscconnectivityinformation.ipaddress -t\n"
                  "NetworkElement,BscConnectivityInformation\n"
                  "NodeId BscConnectivityInformationId ipAddress\n"
                  "MINBS01 1 10.14.204.197\n\n1 instance(s)\n>>")
        self.assertEqual(parse_bsc_connectivity_ip(output, "minbs01"),
                         "10.14.204.197")
        self.assertIsNone(parse_bsc_connectivity_ip(
            output.replace("MINBS01 1", "OTHER 1"), "MINBS01"))
        self.assertIsNone(parse_bsc_connectivity_ip(
            output.replace("10.14.204.197", "999.14.204.197"), "MINBS01"))
        self.assertIsNone(parse_bsc_connectivity_ip(
            output.replace("\n\n1 instance", "\nMINBS01 2 10.14.204.198\n\n2 instance"),
            "MINBS01"))
        self.assertEqual(cutover_runner.bsc_connectivity_mo("MINBS01"),
                         "BscConnectivityInformation")
        self.assertEqual(cutover_runner.bsc_connectivity_mo("MINVBS02"),
                         "VBscConnectivityInformation")

    def test_busy_tch_counts_only_traffic_channels(self):
        # BCCH/CBCH are BUSY but are signalling, not traffic.
        self.assertEqual(parse_rlcrp_busy_tch(RLCRP), 2)

    def test_no_tch_rows_is_unknown_not_zero(self):
        self.assertIsNone(parse_rlcrp_busy_tch("NOT ACCEPTED\nFAULT CODE 7"))


class CaptureTests(unittest.TestCase):
    def test_traffic_parts_preserve_sections_and_group_under_node(self):
        from cutover_runner import split_traffic_output
        output = ('NODE INFO\nId ;FRU\nRADIO INFO\nId ;Alarm\nALARM INFO\n\n'
                  '================\nId ;LTECell ;UEs\nLTE ROW\nTotal: 1 Cells\n\n'
                  '================\nId ;NRCell ;UEs\nNR ROW\nTotal: 1 Cells\n')
        part1,part2=split_traffic_output(output)
        self.assertEqual(part1+'\n'+part2,output)
        self.assertIn('ALARM INFO',part1)
        self.assertNotIn('LTECell',part1)
        self.assertTrue(part2.startswith('================\nId ;LTECell'))
        self.assertIn('NR ROW',part2)
        self.assertEqual(split_traffic_output('command failed'),None)
        self.assertIsNotNone(split_traffic_output('Id ;Alarm\n===\nId ;NRCell\nNR ROW'))
        eng=self._engine({'MIN823_GINGOOB01':NodeFake({'stzrc':output},delay=0)})
        eng.capture_evidence('traffic')
        self._wait(eng,'traffic')
        [ev]=drain(eng,'evidence_ready')
        self.assertEqual([label for label,_ in ev.images],
                         ['All','GINGOOB01','GINGOOB01 / Part 1','GINGOOB01 / Part 2'])
        for label,path in ev.images:
            if ' / Part ' in label:
                with open(os.path.splitext(path)[0]+'.txt',encoding='utf-8') as fh:
                    text=fh.read()
                if label.endswith('1'):
                    self.assertIn('ALARM INFO',text)
                    self.assertNotIn('LTE ROW',text)
                else:
                    self.assertIn('LTE ROW',text)
                    self.assertIn('NR ROW',text)
                    self.assertNotIn('ALARM INFO',text)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, fakes, cells=()):
        eng = build_engine(self.tmp, list(fakes))
        for node, fake in fakes.items():
            eng.run.sessions[node] = NodeSession(node_name=node, ssh=fake,
                                                 read_ssh=fake)
        eng.run.cells = list(cells)
        return eng

    def _wait(self, eng, kind):
        eng._capture_threads[kind].join(timeout=30)
        self.assertFalse(eng._capture_threads[kind].is_alive())

    def test_combined_commands_and_gsm_filter_with_saved_evidence(self):
        lte = NodeFake({'alt': ALT_NOW, 'st cell': 'EUtranCellFDD=X-1 UNLOCKED ENABLED',
                        'stzr': 'Combined traffic evidence'}, delay=0.1)
        gsm = NodeFake({'alt': ALT_NOW}, delay=0.1)
        cells = [CutoverCell(node_name='MIN823_GINGOOB01', mo_type='EUtranCellFDD',
                             cell_dn='X-1', rat='LTE'),
                 CutoverCell(node_name='MIN823_GINGOOB03', mo_type='GeranCell',
                             cell_dn='G-1', rat='GSM')]
        eng = self._engine({'MIN823_GINGOOB01': lte, 'MIN823_GINGOOB03': gsm}, cells)
        self.assertTrue(eng.capture_evidence('combined'))
        self.assertFalse(eng.capture_evidence('alarms'))
        self.assertFalse(eng.capture_evidence('combined'))
        self._wait(eng, 'combined')
        self.assertEqual(lte.sent, ['alt', 'st cell', 'stzr'])
        self.assertEqual(gsm.sent, ['alt'])
        [ev] = drain(eng, 'evidence_ready')
        self.assertEqual(ev.group, 'combined')
        self.assertEqual([label for label,_ in ev.images],
                         ['All','GINGOOB01','GINGOOB01 / All','GINGOOB01 / Part 1: alt + st cell',
                          'GINGOOB01 / Part 2: stzr','GINGOOB03',
                          'GINGOOB03 / All','GINGOOB03 / Part 1: alt + st cell'])
        for label,path in ev.images:
            if label == 'GINGOOB01 / All':
                with open(os.path.splitext(path)[0]+'.txt',encoding='utf-8') as fh:
                    combined_text=fh.read()
                for command in ('> alt','> st cell','> stzr'):
                    self.assertIn(command,combined_text)
        for label,path in ev.images:
            if ' / Part ' in label:
                with open(os.path.splitext(path)[0]+'.txt',encoding='utf-8') as fh:
                    part_text=fh.read()
                if 'Part 1:' in label:
                    self.assertIn('> alt',part_text)
                    self.assertNotIn('> stzr',part_text)
                    if label.startswith('GINGOOB01'):
                        self.assertIn('> st cell',part_text)
                else:
                    self.assertIn('> stzr',part_text)
                    self.assertNotIn('> alt',part_text)
                    self.assertNotIn('> st cell',part_text)
        with open(os.path.splitext(ev.images[0][1])[0]+'.txt', encoding='utf-8') as fh:
            text = fh.read()
        for command in ('> alt', '> st cell', '> stzr', 'Combined traffic evidence'):
            self.assertIn(command, text)
        self.assertIn('MIN823_GINGOOB01', eng.run.alarm_status)

    def test_combined_keeps_other_commands_after_failure(self):
        class PartialFake(NodeFake):
            def run_amos_command_safe(self, command, node, timeout=120):
                if command == 'st cell':
                    self.sent.append(command)
                    raise RuntimeError('status unavailable')
                return super().run_amos_command_safe(command, node, timeout)
        fake = PartialFake({'alt': ALT_NOW, 'stzr': 'Traffic still captured'}, delay=0)
        eng = self._engine({'MIN823_GINGOOB01': fake})
        eng.capture_evidence('combined')
        self._wait(eng, 'combined')
        [ev] = drain(eng, 'evidence_ready')
        with open(os.path.splitext(ev.images[0][1])[0]+'.txt', encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('CAPTURE FAILED', text)
        self.assertIn('Traffic still captured', text)

    def test_alarm_width_is_bounded_and_complete_raw_evidence_retained(self):
        from PIL import Image
        output = '\n'.join(f'{i:03d} '+('W'*200)+' RIGHT_EDGE' for i in range(120))
        eng = self._engine({'MIN823_GINGOOB01': NodeFake({'alt': output}, delay=0)})
        eng.cfg['capture']['max_width'] = 600
        eng.capture_evidence('alarms')
        self._wait(eng, 'alarms')
        [ev] = drain(eng, 'evidence_ready')
        with Image.open(ev.images[0][1]) as original:
            self.assertEqual(original.width,1800)
        self.assertEqual(len(ev.images),2)
        with open(os.path.splitext(ev.images[0][1])[0]+'.txt',encoding='utf-8') as fh:
            self.assertIn('119 '+('W'*200)+' RIGHT_EDGE',fh.read())

    def test_combined_width_follows_stzr_and_raw_alarm_is_retained(self):
        from PIL import Image
        from terminal_renderer import _get_font
        alarm='ALARM '+('X'*500)+' ALARM_END'
        traffic='-'*100+' TRAFFIC_END'
        eng=self._engine({'MIN823_GINGOOB01':NodeFake({
            'alt':alarm,'st cell':'EUtranCellFDD=ABC UNLOCKED ENABLED','stzr':traffic},delay=0)})
        eng.capture_evidence('combined')
        self._wait(eng,'combined')
        [ev]=drain(eng,'evidence_ready')
        style=eng.cfg['report']['terminal_style']
        font=_get_font(style['font'],max(22,style['font_size']))
        expected_width=max(600,style['padding']*2+10+int(font.getlength(traffic)))
        with Image.open(ev.images[0][1]) as image:
            self.assertEqual(image.width,expected_width)
        with open(os.path.splitext(ev.images[0][1])[0]+'.txt',encoding='utf-8') as fh:
            self.assertIn('ALARM_END',fh.read())

    def test_alarms_run_in_parallel_and_produce_images(self):
        fakes = {f"MIN823_GINGOOB0{i}": NodeFake({"alt": ALT_NOW})
                 for i in (1, 2, 3)}
        eng = self._engine(fakes)
        eng.run.alarm_baseline["MIN823_GINGOOB01"] = ALT_BASE
        started = time.monotonic()
        self.assertTrue(eng.capture_evidence("alarms"))
        self._wait(eng, "alarms")
        elapsed = time.monotonic() - started
        # 3 nodes x 0.4 s serial would be >= 1.2 s.
        self.assertLess(elapsed, 1.1, f"not parallel: {elapsed:.2f}s")
        [ev] = drain(eng, "evidence_ready")
        labels = [label for label, _ in ev.images]
        self.assertEqual(labels, ["All", "GINGOOB01", "GINGOOB02", "GINGOOB03"])
        for _, png in ev.images:
            self.assertTrue(os.path.isfile(png), png)
            self.assertTrue(os.path.isfile(os.path.splitext(png)[0] + ".txt"))
        self.assertIn("EVIDENCE", ev.images[0][1])
        # Live strip: new-vs-baseline only where a baseline exists.
        st = eng.run.alarm_status
        self.assertEqual(st["MIN823_GINGOOB01"]["new"], 1)
        self.assertIsNone(st["MIN823_GINGOOB02"]["new"])
        with open(os.path.splitext(ev.images[1][1])[0] + ".txt",
                  encoding="utf-8") as fh:
            text = fh.read()
            self.assertNotIn("NEW since cut over started", text)
            self.assertNotIn("no new alarms", text)
            self.assertIn("CellDown", text)

    def test_refresh_alarms_updates_strip_without_dialog(self):
        eng = self._engine({"MIN823_GINGOOB01": NodeFake({"alt": ALT_NOW},
                                                         delay=0)})
        eng.refresh_alarms()
        self._wait(eng, "alarms")
        self.assertEqual(drain(eng, "evidence_ready"), [])
        self.assertEqual(eng.run.alarm_status["MIN823_GINGOOB01"]["error"], "")

    def test_failing_node_does_not_abort_the_image(self):
        fakes = {"MIN823_GINGOOB01": NodeFake({"alt": ALT_NOW}, delay=0),
                 "MIN823_GINGOOB02": NodeFake({}, delay=0, fail=True)}
        eng = self._engine(fakes)
        eng.capture_evidence("alarms")
        self._wait(eng, "alarms")
        [ev] = drain(eng, "evidence_ready")
        self.assertIn("Failed: MIN823_GINGOOB02", ev.message)
        self.assertEqual(len(ev.images), 3)
        self.assertTrue(eng.run.alarm_status["MIN823_GINGOOB02"]["error"])

    def test_cell_status_and_traffic_skip_gsm_only_baseband(self):
        lte = NodeFake({"st cell": "EUtranCellFDD=X-1 UNLOCKED ENABLED"}, delay=0)
        gsm = NodeFake({}, delay=0)
        cells = [CutoverCell(node_name="MIN823_GINGOOB01",
                             mo_type="EUtranCellFDD", cell_dn="X-1", rat="LTE"),
                 CutoverCell(node_name="MIN823_GINGOOB03", mo_type="GeranCell",
                             cell_dn="M8239S1", rat="GSM")]
        eng = self._engine({"MIN823_GINGOOB01": lte, "MIN823_GINGOOB03": gsm},
                           cells)
        eng.capture_evidence("cell_status")
        self._wait(eng, "cell_status")
        self.assertEqual(lte.sent, ["st cell"])
        self.assertEqual(gsm.sent, [])

    def test_bsc_traffic_missing_live_ip_names_the_bsc(self):
        cells = [CutoverCell(node_name="MIN823_GINGOOB03", mo_type="GeranCell",
                             cell_dn="M8239S1", rat="GSM", sector="1",
                             gsm_fdn="SubNetwork=ONRM_ROOT_MO_R,MeContext=MINBS01,"
                                     "ManagedElement=MINBS01,GeranCell=M8239S1")]
        fake = NodeFake({}, delay=0)
        eng = self._engine({"MIN823_GINGOOB03": fake}, cells)
        eng.capture_bsc_traffic()
        self._wait(eng, "bsc_traffic")
        [diag] = drain(eng, "diagnostic")
        self.assertIn("MINBS01", diag.message)
        self.assertIn("BscConnectivityInformation.ipAddress", diag.message)
        self.assertTrue(any("cmedit get minbs01 bscconnectivityinformation.ipaddress -t"
                            in command.lower() for command in fake.sent))

    def test_bsc_traffic_per_sector_tabs_and_busy_tch(self):
        fdn = "MeContext=MINBS01,ManagedElement=MINBS01,GeranCell="
        cells = [CutoverCell(node_name="B03", mo_type="GeranCell", cell_dn=dn,
                             rat="GSM", sector=sec, gsm_fdn=fdn + dn)
                 for dn, sec in (("M8239S1", "1"), ("M8238S1", "1"),
                                 ("M8239S2", "2"))]
        class BscIpFake(NodeFake):
            def run_amos_command_safe(self, command, node, timeout=120):
                self.sent.append(command)
                if "bscconnectivityinformation.ipaddress" in command.lower():
                    return ("NodeId  BscConnectivityInformationId ipAddress\n"
                            "MINBS01 1 10.14.204.197\n\n1 instance(s)")
                return self.outputs.get(command, "")
        fake = BscIpFake({}, delay=0)
        eng = self._engine({"B03": fake}, cells)
        seen = {}

        def fake_rlcrp(factory, ip, user, password, cell_list, log, **kw):
            seen.update(ip=ip, cells=list(cell_list))
            return [(c, RLCRP.replace("M12098S3", c)) for c in cell_list]

        orig = bsc_mml.run_rlcrp
        bsc_mml.run_rlcrp = fake_rlcrp
        try:
            eng.capture_bsc_traffic()
            self._wait(eng, "bsc_traffic")
        finally:
            bsc_mml.run_rlcrp = orig
        self.assertEqual(seen["ip"], "10.14.204.197")
        self.assertEqual(sum("bscconnectivityinformation.ipaddress" in c.lower()
                             for c in fake.sent), 1)
        [ev] = drain(eng, "evidence_ready")
        self.assertEqual([l for l, _ in ev.images],
                         ["All", "S1", "S1 / M8239S1", "S1 / M8238S1", "S2", "S2 / M8239S2"])
        for label,path in ev.images:
            if ' / ' in label:
                with open(os.path.splitext(path)[0]+'.txt',encoding='utf-8') as fh:
                    text=fh.read()
                cell=label.split(' / ')[1]
                self.assertIn('rlcrp:cell='+cell+';',text)
                self.assertEqual(text.count('### '),1)
        self.assertTrue(all(c.gsm_busy_tch == 2 for c in cells))


class ScriptedShell:
    """A shell channel that answers each sent line from a script."""

    def __init__(self, answers):
        self.answers = answers        # list of (expected_substring, reply)
        self.pending = ""
        self.lines = []

    def send(self, data):
        line = data.rstrip("\n")
        self.lines.append(line)
        for i, (expect, reply) in enumerate(self.answers):
            if expect in line:
                self.pending += reply
                self.answers.pop(i)
                return

    def recv_ready(self):
        return bool(self.pending)

    def recv(self, n):
        data, self.pending = self.pending[:n], self.pending[n:]
        return data.encode()


class ScriptedSSH:
    def __init__(self, answers):
        self.shell = ScriptedShell(answers)
        self.connected = self.disconnected = False

    def connect(self, timeout=30):
        self.connected = True

    def send(self, text):
        self.shell.send(text + "\n")

    def _channel_dead(self):
        return False

    def disconnect(self):
        self.disconnected = True


class BscMmlTests(unittest.TestCase):
    def _answers(self, password_reply):
        return [
            ("ssh -p 22 u@10.14.204.197",
             "*****\nWELCOME TO MINBS01, IF YOU ARE NOT AN AUTHORIZED USER\n"
             "*****\nPassword: "),
            ("secret", password_reply),
            ("mml", "mml\nWO      MINBS01_G24Q4_B05  AD-739  TIME 260918 181854\n<"),
            ("rlcrp:cell=M12098S3;", RLCRP + "\n<"),
        ]

    def test_login_mml_and_printout(self):
        ssh = ScriptedSSH(self._answers("\n>"))
        out = bsc_mml.run_rlcrp(lambda: ssh, "10.14.204.197", "u", "secret",
                                ["M12098S3"], lambda m: None,
                                login_timeout=3, cell_timeout=3)
        self.assertEqual(out[0][0], "M12098S3")
        self.assertIn("CELL RESOURCES", out[0][1])
        self.assertTrue(out[0][1].rstrip().endswith("END"))
        self.assertTrue(ssh.disconnected)

    def test_wrong_password_is_a_clear_error_and_still_disconnects(self):
        ssh = ScriptedSSH(self._answers("\nPermission denied, please try again.\nPassword: "))
        with self.assertRaises(bsc_mml.BscMmlError) as ctx:
            bsc_mml.run_rlcrp(lambda: ssh, "10.14.204.197", "u", "secret",
                              ["M12098S3"], lambda m: None,
                              login_timeout=3, cell_timeout=3)
        self.assertIn("rejected the login", str(ctx.exception))
        self.assertTrue(ssh.disconnected)

    def test_password_never_sent_through_logged_send(self):
        ssh = ScriptedSSH(self._answers("\n>"))
        logged = []
        bsc_mml.run_rlcrp(lambda: ssh, "10.14.204.197", "u", "secret",
                          ["M12098S3"], logged.append,
                          login_timeout=3, cell_timeout=3)
        self.assertFalse(any("secret" in m for m in logged))


if __name__ == "__main__":
    unittest.main()
