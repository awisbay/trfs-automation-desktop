import tempfile
import unittest
from pathlib import Path

from audit.audit_core import (AuditItem, _detect_feature_conditions,
                             audit_features, cdd_lte_tx_by_carrier,
                             generate_moshell_scripts, _collect_set_rows)


class FeatureDetectionTests(unittest.TestCase):
    node = 'MIN1_B01'

    def fixture(self):
        prefix = 'ManagedElement=' + self.node + ','
        return prefix, {
            prefix + 'ENodeBFunction=1,EUtranCellFDD=C1': {'sectorCarrierRef': prefix + 'SectorCarrier=SC1'},
            prefix + 'SectorCarrier=SC1': {'noOfTxAntennas': '2', 'sectorFunctionRef': prefix + 'SectorEquipmentFunction=S1'},
            prefix + 'SectorEquipmentFunction=S1': {'rfBranchRef': ';'.join(prefix + f'AntennaUnitGroup=R1,RfBranch={i}' for i in range(1, 5))},
            **{prefix + f'AntennaUnitGroup=R1,RfBranch={i}': {'rfPortRef': prefix + f'RfPort={i}'} for i in range(1, 5)},
        }

    def conditions(self, records, **kwargs):
        return _detect_feature_conditions(self.node, records, **kwargs)

    def test_cdd_overrides_config_and_shared_radio_ports(self):
        prefix, records = self.fixture()
        records[prefix + 'SectorCarrier=SC1']['noOfTxAntennas'] = '8'
        self.assertEqual(self.conditions(records, cdd_tx_by_node={self.node: {2}}), {'lte'})
        self.assertEqual(self.conditions(records, cdd_tx_by_carrier={(self.node.casefold(), 'sectorcarrier=sc1'): {2}}), {'lte'})

    def test_config_overrides_branch_fallback_and_nr_carriers_do_not_gate_lte(self):
        prefix, records = self.fixture()
        records[prefix + 'NRSectorCarrier=NR1'] = {'noOfTxAntennas': '8'}
        self.assertEqual(self.conditions(records), {'lte'})
        records[prefix + 'SectorCarrier=SC1']['noOfTxAntennas'] = '4'
        self.assertEqual(self.conditions(records), {'lte', '4t4r'})

    def test_attached_branches_only_when_config_unavailable(self):
        prefix, records = self.fixture()
        records[prefix + 'SectorCarrier=SC1']['noOfTxAntennas'] = '-1'
        self.assertEqual(self.conditions(records), {'lte', '4t4r'})
        records[prefix + 'SectorCarrier=SC1']['rfBranchTxRef'] = ';'.join(
            prefix + f'AntennaUnitGroup=R1,RfBranch={i}' for i in (1, 2))
        self.assertEqual(self.conditions(records), {'lte'})
        del records[prefix + 'AntennaUnitGroup=R1,RfBranch=2']
        self.assertEqual(self.conditions(records), {'lte'})

    def test_unrelated_carrier_does_not_trigger_lte_mimo_features(self):
        prefix, records = self.fixture()
        records[prefix + 'SectorCarrier=UNUSED'] = {'noOfTxAntennas': '8'}
        self.assertEqual(self.conditions(records), {'lte'})

    def test_mixed_carriers_retain_each_carriers_cdd_priority(self):
        prefix, records = self.fixture()
        records[prefix + 'ENodeBFunction=1,EUtranCellFDD=C2'] = {'sectorCarrierRef': prefix + 'SectorCarrier=SC2'}
        records[prefix + 'SectorCarrier=SC2'] = {'noOfTxAntennas': '8'}
        planned = {(self.node.casefold(), 'sectorcarrier=sc1'): {2}}
        self.assertEqual(self.conditions(records, cdd_tx_by_carrier=planned), {'lte', '8t8r'})

    def test_cdd_resolves_to_owning_lte_carrier_without_using_nr_or_other_node(self):
        prefix, records = self.fixture()
        item = AuditItem('cell', 'lte_nr', 'ENodeBFunction=1,EUtranCellFDD=C1',
                         'noOfTxAntennas', '2', 'C1', 'CDD!MIMO', node=self.node, via_ref='sectorCarrierRef')
        self.assertEqual(cdd_lte_tx_by_carrier([item], records), {(self.node.casefold(), 'sectorcarrier=sc1'): {2}})
        item.tech = 'nr'
        self.assertEqual(cdd_lte_tx_by_carrier([item], records), {})
        item.tech = 'lte_nr'
        records[prefix + item.mo_local]['sectorCarrierRef'] = 'ManagedElement=OTHER,SectorCarrier=SC1'
        self.assertEqual(cdd_lte_tx_by_carrier([item], records), {})

    def test_aas_product_name_fallback_supports_dump_representations(self):
        for attrs in ({'productName': 'AIR 3265 B41'},
                      {'productData.productName': 'AIR 3265 B41'},
                      {'productData': '{productName=AIR 3265 B41, productNumber=KRD}'},
                      {'PRODUCTNAME': 'AIR 3265 B41'}):
            with self.subTest(attrs=attrs):
                prefix, records = self.fixture()
                records[prefix + 'Equipment=1,FieldReplaceableUnit=RADIO1'] = attrs
                self.assertIn('aas_b41_lte', self.conditions(records))

    def test_fru_id_remains_authoritative_over_product_name(self):
        prefix, records = self.fixture()
        records[prefix + 'Equipment=1,FieldReplaceableUnit=AAS_B41_RRU1'] = {'productName': 'AIR 3285 B1 B3'}
        conds = self.conditions(records)
        self.assertIn('aas_b41_lte', conds)
        self.assertNotIn('aas_b1b3', conds)

    def test_aas_b1b3_fallback_and_non_air_exclusion(self):
        prefix, records = self.fixture()
        records[prefix + 'Equipment=1,FieldReplaceableUnit=RADIO1'] = {'productName': 'AIR 3285 B1/B3'}
        self.assertIn('aas_b1b3', self.conditions(records))
        records[prefix + 'Equipment=1,FieldReplaceableUnit=RADIO1'] = {'productName': 'Radio 4480 B41'}
        self.assertNotIn('aas_b41_lte', self.conditions(records))

    def test_capacity_feature_uses_real_mo_for_state_and_generated_script(self):
        prefix, records = self.fixture()
        mo = 'SystemFunctions=1,Lm=1,CapacityState=CXC4012411'
        records[prefix + mo] = {'featureState': '0 (DEACTIVATED)', 'licenseState': '1 (ENABLED)'}
        rules = {'lte': {'detect': 'lte', 'features': ['CXC4012411'], 'baseline': True}}
        rows = audit_features(records, rules)
        self.assertEqual([(r.mo, r.status) for r in rows], [(mo, 'Mismatch')])
        self.assertEqual(_collect_set_rows(rows, ('Mismatch',), 'MIN1'), {})
        with tempfile.TemporaryDirectory() as folder:
            script = Path(generate_moshell_scripts(rows, folder, 'MIN1', 'audit.xlsx')[0]).read_text(encoding='utf-8')
            self.assertIn(f'set {mo} featureState 1', script)
            self.assertNotIn('FeatureState=CXC4012411', script)
        records[prefix + mo]['featureState'] = '1 (ACTIVATED)'
        self.assertEqual(audit_features(records, rules), [])

    def test_capacity_feature_disabled_license_omits_activation(self):
        prefix, records = self.fixture()
        records[prefix + 'SystemFunctions=1,Lm=1,CapacityState=CXC4012411'] = {
            'featureState': '0', 'licenseState': '0'}
        rows = audit_features(records, {'lte': {'detect': 'lte', 'features': ['CXC4012411']}})
        self.assertFalse(rows[0].feature_activation_allowed)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(generate_moshell_scripts(rows, folder, 'MIN1', 'audit.xlsx'), [])


if __name__ == '__main__':
    unittest.main()
