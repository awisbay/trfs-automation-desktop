import tempfile
import unittest
from cutover_model import CutoverCell, GroupState, GSM, NodeSession, RunPhase
from cutover_runner import gsm_band_for_group
from test_cutover_capture import NodeFake, build_engine


class GsmBandActionTests(unittest.TestCase):
    def test_group_mapping_is_explicit(self):
        self.assertEqual(gsm_band_for_group('LB'),'GSM900')
        self.assertEqual(gsm_band_for_group('MB'),'GSM1800')
        self.assertEqual(gsm_band_for_group('HB'),'')

    def test_lb_sector_routes_matching_gsm_band(self):
        with tempfile.TemporaryDirectory() as folder:
            eng=build_engine(folder,['NODE'])
            called=[]
            eng._spawn=lambda target,name: target()
            eng._grouped_action=lambda groups,sector=None,gsm_band='': called.append(
                (groups,sector,gsm_band))
            eng.unlock_group('LB','1')
            eng.unlock_group('MB','2')
            eng.unlock_group('HB','3')
            eng.unlock_group('LB')
            self.assertEqual(called,[
                (['LB',GSM],'1','GSM900'),
                (['MB',GSM],'2','GSM1800'),
                (['HB'],'3',''),
                (['LB'],None,''),
            ])
            locked=[]
            eng._lock_action=lambda groups,sector=None,gsm_band='': locked.append(
                (groups,sector,gsm_band))
            eng.lock_group('LB','1')
            self.assertEqual(locked,[(['LB',GSM],'1','GSM900')])

    def test_gsm_execution_activates_only_requested_band_and_sector(self):
        with tempfile.TemporaryDirectory() as folder:
            fake=NodeFake({},delay=0)
            eng=build_engine(folder,['NODE'])
            eng.cfg['dry_run']=True
            eng.cfg['require_confirmation']=False
            eng.run.phase=RunPhase.READY
            eng.run.sessions['NODE']=NodeSession(node_name='NODE',ssh=fake)
            cells=[
                CutoverCell('NODE','GeranCell','SITE9S1',rat='GSM',group=GSM,
                            band_key='GSM900',sector='1',geran_state='HALTED'),
                CutoverCell('NODE','GeranCell','SITE8S1',rat='GSM',group=GSM,
                            band_key='GSM1800',sector='1',geran_state='HALTED'),
                CutoverCell('NODE','GeranCell','SITE9S2',rat='GSM',group=GSM,
                            band_key='GSM900',sector='2',geran_state='HALTED'),
            ]
            eng.run.groups[GSM]=GroupState(name=GSM)
            for cell in cells:
                eng.run.cells.append(cell)
                eng.run.by_key[cell.key]=cell
                eng.run.groups[GSM].cell_keys.append(cell.key)
            eng._persist_checkpoint=lambda: ''
            eng._start_group_logs=lambda *a: None
            eng._stop_group_logs=lambda *a: None
            eng._start_gsm_assurance=lambda *a: None
            eng._run_gsm_group('1',band_key='GSM900')
            self.assertTrue(cells[0].was_unlocked_by_run)
            self.assertFalse(cells[1].was_unlocked_by_run)
            self.assertFalse(cells[2].was_unlocked_by_run)
            self.assertIn('[dry run] set ACTIVE',cells[0].status_detail)
            for cell in cells:
                cell.admin_state='UNLOCKED'
            cells[0].geran_state='ACTIVE'
            cells[1].geran_state='ACTIVE'
            cells[2].geran_state='ACTIVE'
            eng.run.phase=RunPhase.READY
            eng._lock_gsm('1',band_key='GSM900')
            self.assertEqual(cells[0].admin_state,'LOCKED')
            self.assertEqual(cells[1].admin_state,'UNLOCKED')
            self.assertEqual(cells[2].admin_state,'UNLOCKED')

if __name__=='__main__': unittest.main()
