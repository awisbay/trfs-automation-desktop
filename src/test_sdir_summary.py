import unittest
import tempfile
import os
from sdir_summary import build_sdir_summary
from terminal_renderer import strip_ansi, ansi_text_runs
from test_cutover_capture import build_engine, NodeFake, drain
from cutover_model import NodeSession

RAW='''NODE> sdirc
Total: 1 CPRI links (1 OK, 0 OKW, 0 NOK, 0 NT)
FRU ;LNH ;BOARD ;ST ;PRODUCTNUMBER
B0B28_RRU1 ;fru1 ;RRU449944B0A44B28C ;1 ;KRD
-----
ID ;RiL ;Type ;Res ;MO1-MO2 ;BOARD1-BOARD2 ;Issue (Failed checks)
1 ;B0B28_RRU1 ;O101 ;OK ;BB-1 RRU1 ;RANP6655 WRONGBOARD ;Passed
-----
FRU ;LNH ;BOARD ;RF ;VSWR (RL) ;Cells
B0B28_RRU1 ;fru1 ;RRU4499 ;A ;1.17 (24.5) ;CELL1
B0B28_RRU1 ;fru1 ;RRU4499 ;B ;1.29 (23.0) ;CELL1
-----
ID ;LINK ;RiL ;BER1 ;BER2 ;DlLoss ;UlLoss
1 ;link1 ;B0B28_RRU1 ;0 ;0/0 ;-1.36 ;-2.62
-----
ID ;T ;RiL ;BPBP ;RATE ;RATE
1 ;O ;B0B28_RRU1 ;A ;10.1G ;10.1G
-----
radioClockState : RNT_TIME_LOCKED
Prio ;ST ;syncRefType ;Reference
*1 ;1 ;GNSS_RECEIVER ;GNSS1
-----
'''

class SdirSummaryTests(unittest.TestCase):
    def test_aas_missing_vswr_is_not_unreadable_or_falsely_healthy(self):
        aas=RAW.replace('B0B28_RRU1','AAS_B41_RRU1').replace('RRU449944B0A44B28C','AIR3265B41')
        aas=aas.replace('1.17 (24.5)','-').replace('1.29 (23.0)','-')
        summary=strip_ansi(build_sdir_summary(aas,'NODE'))
        self.assertIn('VSWR  : N/A (AAS)',summary)
        self.assertNotIn('Unreadable/missing',summary)
        self.assertNotIn('all ports <=',summary)
        self.assertNotIn('VSWR-A',summary)
        mixed=strip_ansi(build_sdir_summary(RAW+'\n'+aas,'NODE'))
        self.assertIn('VSWR-A',mixed)
        self.assertIn('AAS_B41_RRU1',mixed)
        self.assertNotIn('Unreadable/missing',mixed)
        # A real high measurement remains a warning even on an AAS radio.
        high=strip_ansi(build_sdir_summary(aas.replace(';A ;- ;',';A ;1.8 ;'),'NODE'))
        self.assertIn('Port A AAS_B41_RRU1 (1.8)',high)

    def test_loss_digit_columns_reserve_minus_position(self):
        second=RAW.replace('B0B28_RRU1','B0B28_RRU2').replace(';-1.36 ;-2.62',';0.30 ;1.52')
        plain=strip_ansi(build_sdir_summary(RAW+'\n'+second,'NODE'))
        rows=[line for line in plain.splitlines() if line.startswith('B0B28_RRU')]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0].index('-1.36')+1,rows[1].index('0.30'))
        self.assertEqual(rows[0].index('-2.62')+1,rows[1].index('1.52'))

    def test_summary_matches_supplied_columns(self):
        summary=build_sdir_summary(RAW,'NODE')
        plain=strip_ansi(summary)
        for value in ('sdir Summary NODE','1 CPRI links','all ports <= 1.4',
                      'GPS - OK (RNT_TIME_LOCKED)','RRU449944 B0B28','Fiber Check',
                      '10.1G','-1.36','-2.62','VSWR-A','VSWR-B','1.17','1.29'):
            self.assertIn(value,plain)
        self.assertNotIn('WRONGBOARD',plain)
        self.assertNotIn('BER',plain)
        self.assertIn('\x1b[1;32mPassed',summary)

    def test_hot_ports_rate_and_ber_errors(self):
        raw=RAW.replace('1.29 (23.0)','1.80 (23.0)').replace('10.1G ;10.1G','2.5G ;10.1G').replace(';0 ;0/0 ;',';0 ;5.4 ;')
        plain=strip_ansi(build_sdir_summary(raw,'NODE'))
        self.assertIn('Port B B0B28_RRU1 (1.80)',plain)
        self.assertIn('Unmatch Low 2.5G - Top 10.1G',plain)
        self.assertIn('5.4 (BER2)',plain)

    def test_unreadable_is_never_all_ports_ok_and_missing_sections(self):
        summary=strip_ansi(build_sdir_summary(RAW.replace('1.29 (23.0)','NA'),'NODE'))
        self.assertIn('Unreadable/missing',summary)
        self.assertNotIn('all ports <=',summary)
        summary=strip_ansi(build_sdir_summary('Total: 2 CPRI links (0 OK, 0 OKW, 2 NOK, 0 NT)\n1 RADIO1 O101 NOK BB-1 RANP6655 (L) Failed','NODE'))
        self.assertIn('No RRU detected',summary)
        self.assertIn('unavailable',summary)
        self.assertNotIn('VSWR-A',summary)
        self.assertIsNone(build_sdir_summary('command failed','NODE'))

    def test_ansi_runs_preserve_visible_text_and_colors(self):
        runs=ansi_text_runs('Radio \x1b[1;32mPassed\x1b[0m 1.17',(255,255,255))
        self.assertEqual(''.join(t for t,c in runs),'Radio Passed 1.17')
        self.assertIn(('Passed',(80,250,123)),runs)

    def test_cached_preview_executes_no_ssh_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=build_engine(folder,['NODE'])
            fake=NodeFake({},delay=0)
            engine.run.sessions['NODE']=NodeSession(node_name='NODE',ssh=fake,read_ssh=fake)
            engine._ensure_sessions=lambda: (_ for _ in ()).throw(AssertionError('must not connect'))
            engine._cache_vswr_output('NODE',RAW)
            stamp=engine._vswr_cache['NODE']['at']
            engine._cache_vswr_output('NODE','command failed')
            self.assertEqual(engine._vswr_cache['NODE']['at'],stamp)
            self.assertTrue(engine.capture_evidence('vswr'))
            engine._capture_threads['vswr'].join(20)
            [event]=drain(engine,'evidence_ready')
            self.assertEqual(fake.sent,[])
            self.assertEqual(event.group,'vswr')
            with open(os.path.splitext(event.images[0][1])[0]+'.txt',encoding='utf-8') as fh:
                text=fh.read()
            self.assertIn(stamp,text)
            self.assertIn('VSWR-A',text)
            self.assertNotIn('\x1b',text)

    def test_empty_cache_does_not_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=build_engine(folder,['NODE'])
            engine.capture_evidence('vswr')
            engine._capture_threads['vswr'].join(20)
            self.assertIn('Update Traffic & VSWR',drain(engine,'diagnostic')[0].message)

if __name__=='__main__':
    unittest.main()
