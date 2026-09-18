import unittest

from audit.etilt_audit import _ret_identity, audit_etilt


class TiltFallbackTests(unittest.TestCase):
    def test_parent_sector_not_r_port(self):
        parent = 'ManagedElement=BB2,Equipment=1,AntennaUnitGroup=RB_MODEL_S2,AntennaNearUnit=3,RetSubUnit=1'
        self.assertEqual(_ret_identity('GINGOOLY_R1', parent), ('GINGOOLY', '2'))
        self.assertEqual(_ret_identity('GINGOOLY_R2', parent), ('GINGOOLY', '2'))
        self.assertEqual(_ret_identity('GINGOOLY-L3_R1/R2', parent), ('GINGOOLY', '3'))
        self.assertIsNone(_ret_identity('N/A_Y2', parent))
        self.assertIsNone(_ret_identity('GINGOOLY_R2', parent.replace('_S2', '_UNKNOWN')))

    def test_both_ret_branches_preserved(self):
        prefix = 'ManagedElement=BB2,Equipment=1,AntennaUnitGroup=RB_MODEL_S2,'
        records = {
            prefix + 'AntennaNearUnit=3,RetSubUnit=1': {'userLabel': 'GINGOOLY_R2', 'electricalAntennaTilt': '60'},
            prefix + 'AntennaNearUnit=4,RetSubUnit=1': {'userLabel': 'GINGOOLY_R1', 'electricalAntennaTilt': '20'},
            prefix + 'AntennaNearUnit=1,RetSubUnit=1': {'userLabel': 'N/A_Y4', 'electricalAntennaTilt': '20'},
        }
        targets = [('GINGOOL-172', 'BB2', '6'), ('GINGOOY-122', 'BB2', '6')]
        rows = audit_etilt(targets, records)
        self.assertEqual([(r.ref_cell, r.actual, r.status) for r in rows], [
            ('GINGOOL-172', '6', 'Match'), ('GINGOOL-172', '2', 'Mismatch'),
            ('GINGOOY-122', '6', 'Match'), ('GINGOOY-122', '2', 'Mismatch')])
        self.assertTrue(all('RB_MODEL_S2' in r.mo for r in rows))
