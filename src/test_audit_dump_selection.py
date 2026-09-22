import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from audit.dump_selection import select_node_dumps, site_matches


class DumpSelectionTests(unittest.TestCase):
    def write_dump(self, folder, filename, nodes, timestamp, xml=False):
        path = Path(folder) / filename
        if xml:
            body = ''.join(
                f'<ManagedElement id="{node}"><VsDataContainer id="SC1">'
                '<attributes xmlns="genericNrm.xsd"><vsDataType>vsDataSectorCarrier</vsDataType>'
                f'<vsDataSectorCarrier><noOfTxAntennas>{tx}</noOfTxAntennas></vsDataSectorCarrier>'
                '</attributes></VsDataContainer></ManagedElement>' for node, tx in nodes)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('export.xml', '<configData>' + body + '</configData>')
        else:
            path.write_text('\n'.join(
                f'ManagedElement={node},SectorCarrier=SC1 noOfTxAntennas {tx}'
                for node, tx in nodes), encoding='utf-8')
        os.utime(path, (timestamp, timestamp))
        return str(path)

    def test_newer_modump_wins_over_cmdump_without_restoring_old_attributes(self):
        with tempfile.TemporaryDirectory() as folder:
            old = self.write_dump(folder, 'MIN1_B01_cmdump.zip', [('MIN1_B01', 8)], 100, xml=True)
            new = self.write_dump(folder, 'MIN1_B01_modump.log', [('MIN1_B01', 2)], 200)
            with zipfile.ZipFile(old) as archive:
                # The newer snapshot deliberately lacks a feature from the old one.
                body = archive.read('export.xml').decode().replace('</vsDataSectorCarrier>',
                    '<oldParameter>1</oldParameter></vsDataSectorCarrier>')
            with zipfile.ZipFile(old, 'w') as archive:
                archive.writestr('export.xml', body)
            os.utime(old, (100, 100))
            for paths in ([old, new], [new, old]):
                records, nodes, evidence = select_node_dumps(paths, site='MIN1')
                self.assertEqual(nodes, ['MIN1_B01'])
                self.assertEqual(records['ManagedElement=MIN1_B01,SectorCarrier=SC1'], {'noOfTxAntennas': '2'})
                self.assertEqual(evidence[0]['path'], new)

    def test_modump_has_priority_over_newer_cmdump(self):
        with tempfile.TemporaryDirectory() as folder:
            modump = self.write_dump(folder, 'MIN1_B01_modump.log', [('MIN1_B01', 2)], 100)
            cmdump = self.write_dump(folder, 'MIN1_B01_cmdump.zip', [('MIN1_B01', 8)], 200, xml=True)
            records, _, evidence = select_node_dumps([cmdump, modump], site='MIN1')
            self.assertEqual(evidence[0]['path'], modump)
            self.assertEqual(records['ManagedElement=MIN1_B01,SectorCarrier=SC1']['noOfTxAntennas'], '2')

    def test_capture_filename_time_precedes_copy_mtime(self):
        with tempfile.TemporaryDirectory() as folder:
            old = self.write_dump(folder, 'MIN1_B01_modump_20260916_100000.log', [('MIN1_B01', 8)], 9999999999)
            new = self.write_dump(folder, 'MIN1_B01_modump_20260917_100000.log', [('MIN1_B01', 4)], 100)
            _, _, evidence = select_node_dumps([old, new], site='MIN1')
            self.assertEqual(evidence[0]['path'], new)
            self.assertEqual(evidence[0]['time_basis'], 'filename capture time')

    def test_mislabeled_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_dump(folder, 'MIN1_B01_modump.log', [('MIN1_B02', 4)], 100)
            logs = []
            self.assertEqual(select_node_dumps([path], site='MIN1', log=logs.append)[:2], ({}, []))
            self.assertTrue(any('differs from payload' in line for line in logs))

    def test_generic_filename_uses_payload_identity_and_site_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_dump(folder, 'download.log', [('MIN1_B01', 4), ('MIN10_B01', 8)], 100)
            records, nodes, _ = select_node_dumps([path], site='min1')
            self.assertEqual(nodes, ['MIN1_B01'])
            self.assertEqual(len(records), 1)
        self.assertFalse(site_matches('MIN10_B01', 'MIN1'))

    def test_batch_payload_is_scoped_and_live_snapshot_is_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_dump(folder, 'CL1_batch_cmdump.zip', [('MIN1_B01', 2), ('MIN2_B01', 4)], 100, xml=True)
            self.assertEqual(select_node_dumps([path], expected_nodes=['MIN1_B01'], existing_nodes=['MIN1_B01'])[:2], ({}, []))
            _, nodes, _ = select_node_dumps([path], expected_nodes=['MIN1_B01'])
            self.assertEqual(nodes, ['MIN1_B01'])

    def test_same_timestamp_choice_is_deterministic_and_duplicates_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.write_dump(folder, 'a.log', [('MIN1_B01', 2)], 100)
            b = self.write_dump(folder, 'b.log', [('MIN1_B01', 4)], 100)
            self.assertEqual(select_node_dumps([a, b, a]), select_node_dumps([b, a]))


if __name__ == '__main__':
    unittest.main()
