import os
import tempfile
import unittest

from gui.audit_page import AuditPage


class AuditDumpHandoffTests(unittest.TestCase):
    def test_split_dump_paths_normalizes_browse_and_copied_values(self):
        with tempfile.TemporaryDirectory() as folder:
            first = os.path.join(folder, "node one_modump.zip")
            second = os.path.join(folder, "node_two_modump.log")
            value = f'  "{first}" | \'{second}\'  '

            self.assertEqual(
                AuditPage._split_dump_paths(value),
                [os.path.abspath(first), os.path.abspath(second)],
            )

    def test_split_dump_paths_ignores_empty_segments(self):
        self.assertEqual(AuditPage._split_dump_paths("  | |  "), [])

    def test_cmdump_adds_sibling_modump(self):
        with tempfile.TemporaryDirectory() as folder:
            cmdump = os.path.join(folder, "NODE_cmdump.zip")
            modump = os.path.join(folder, "NODE_modump.zip")
            open(cmdump, "wb").close()
            open(modump, "wb").close()
            self.assertEqual(
                AuditPage._with_dump_companions([cmdump]),
                [cmdump, modump],
            )


if __name__ == "__main__":
    unittest.main()
