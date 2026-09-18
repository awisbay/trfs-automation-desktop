import itertools
import json
import tempfile
import unittest
from pathlib import Path

from audit.audit_core import audit_features, generate_moshell_scripts


class MixedModeTests(unittest.TestCase):
    codes = ('CXC4012017', 'CXC4012015', 'CXC4012026', 'CXC4011018')
    markers = {'gsm': 'BtsFunction=1', 'lte': 'ENodeBFunction=1', 'nr': 'GNBDUFunction=1'}

    @classmethod
    def setUpClass(cls):
        cls.rules = json.loads(Path(__file__).with_name('audit_map.json').read_text(encoding='utf-8'))['feature_rules']

    def fixture(self, node, techs, state='0', license_state='1'):
        prefix = f'ManagedElement={node},'
        records = {prefix + self.markers[tech]: {} for tech in techs}
        for code in self.codes:
            records[prefix + 'SystemFunctions=1,Lm=1,FeatureState=' + code] = {
                'featureState': state, 'licenseState': license_state}
        return records

    def rows(self, records, **kwargs):
        return {row.mo.split('=')[-1]: row for row in audit_features(records, self.rules, **kwargs)
                if row.mo.split('=')[-1] in self.codes}

    def test_all_technology_combinations_activate_only_correct_features(self):
        for size in range(1, 4):
            for combo in itertools.combinations(self.markers, size):
                techs = set(combo)
                with self.subTest(techs=techs):
                    required = {'CXC4012026', 'CXC4011018'}
                    if 'gsm' in techs and techs & {'lte', 'nr'}:
                        required.add('CXC4012017')
                    if 'lte' in techs and techs & {'gsm', 'nr'}:
                        required.add('CXC4012015')
                    off_rows = self.rows(self.fixture('BB1', techs))
                    self.assertEqual(set(off_rows), required)
                    self.assertTrue(all(r.expected.startswith('ACTIVATED') for r in off_rows.values()))
                    on_rows = self.rows(self.fixture('BB1', techs, state='1'))
                    self.assertEqual(set(on_rows), set(self.codes) - required)
                    self.assertTrue(all(r.expected.startswith('DEACTIVATED') for r in on_rows.values()))

    def test_separate_basebands_and_external_neighbours_do_not_enable_mixed_mode(self):
        records = {}
        for node, tech in [('BB1', 'gsm'), ('BB2', 'lte'), ('BB3', 'nr')]:
            records.update(self.fixture(node, {tech}))
        records['ManagedElement=BB2,ExternalGNodeBFunction=NR'] = {'gNodeBId': '99'}
        records['ManagedElement=BB1,ExternalEUtranCellFDD=LTE'] = {}
        for node in ('BB1', 'BB2', 'BB3'):
            self.assertEqual(set(self.rows(records, nodes=[node])), {'CXC4012026', 'CXC4011018'})

    def test_missing_required_features_are_reported_without_baseline_suppression(self):
        records = {'ManagedElement=BB1,BtsFunction=1': {}, 'ManagedElement=BB1,ENodeBFunction=1': {}}
        rows = self.rows(records)
        self.assertEqual(set(rows), set(self.codes))
        self.assertTrue(all(row.status == 'NotFound' for row in rows.values()))
        # Always-on features are required even in a dump with no RAT markers.
        rows = self.rows({'ManagedElement=BB2,Equipment=1': {}})
        self.assertEqual(set(rows), {'CXC4012026', 'CXC4011018'})

    def test_license_disabled_blocks_generated_activation(self):
        records = self.fixture('BB1', {'gsm', 'lte'}, license_state='0')
        rows = list(self.rows(records).values())
        self.assertTrue(all(r.status == 'Mismatch' and not r.feature_activation_allowed for r in rows))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(generate_moshell_scripts(rows, folder, 'SITE', 'audit.xlsx'), [])
            for attrs in records.values():
                if 'licenseState' in attrs:
                    attrs['licenseState'] = '1'
            rows = list(self.rows(records).values())
            script = Path(generate_moshell_scripts(rows, folder, 'SITE', 'audit.xlsx')[0]).read_text(encoding='utf-8')
            for code in self.codes:
                self.assertIn(f'FeatureState={code} featureState 1', script)

    def test_cell_markers_also_detect_technology_and_never_reuse_other_node_license(self):
        records = self.fixture('BB1', {'gsm', 'nr'})
        records['ManagedElement=BB1,GsmSector=S1'] = records.pop('ManagedElement=BB1,BtsFunction=1')
        records['ManagedElement=BB1,GNBDUFunction=1,NRCellDU=N1'] = records.pop('ManagedElement=BB1,GNBDUFunction=1')
        self.assertIn('CXC4012017', self.rows(records))
        del records['ManagedElement=BB1,SystemFunctions=1,Lm=1,FeatureState=CXC4012017']
        records['ManagedElement=BB2,SystemFunctions=1,Lm=1,FeatureState=CXC4012017'] = {
            'featureState': '1', 'licenseState': '1'}
        self.assertEqual(self.rows(records, nodes=['BB1'])['CXC4012017'].status, 'NotFound')


if __name__ == '__main__':
    unittest.main()
