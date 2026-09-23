import unittest

from audit.ess_audit import audit_ess


class EssAuditTests(unittest.TestCase):
    def test_nr_carrier_uses_cdd_ess_local_id_not_nr_cell_local_id(self):
        node = 'MIN1_B01'
        prefix = f'ManagedElement={node},'
        pair = {'node': node, 'lte_cell': 'CELL-L171', 'lte_local': '171',
                'gnb_id': '902765', 'nr_cell': 'CELL-P501', 'nr_local': '501',
                'ess_local': '171', 'ess_pair': '5010000000171'}
        records = {
            prefix + 'ENodeBFunction=1,EUtranCellFDD=CELL-L171': {'cellId': '171'},
            prefix + 'ENodeBFunction=1,SectorCarrier=B28_S1': {
                'essScLocalId': '171', 'essScPairId': '5010000000171'},
            prefix + 'ENodeBFunction=1,EUtranCellFDD=CELL-L171,GUtranFreqRelation=1,'
                     'GUtranCellRelation=5152-0000000000902765-501': {'essEnabled': 'true'},
            prefix + 'GNBCUCPFunction=1,NRCellCU=CELL-P501': {'cellLocalId': '501'},
            prefix + 'GNBCUCPFunction=1,NRCellCU=CELL-P501,'
                     'EUtranCellRelation=CELL-L171': {'essEnabled': 'true'},
            prefix + 'GNBDUFunction=1,NRSectorCarrier=N28_S1': {
                'essScLocalId': '171', 'essScPairId': '5010000000171'},
        }
        row = audit_ess([pair], records)[0]
        self.assertEqual(row.status, 'Match')
        self.assertEqual(row.nrsc_local, '171')


if __name__ == '__main__':
    unittest.main()
