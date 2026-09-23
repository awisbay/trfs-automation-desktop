import os
import tempfile
import unittest
import zipfile

from audit.audit_core import generate_moshell_scripts
from audit.systemconstant_audit import audit_systemconstant


class SystemConstantAuditTests(unittest.TestCase):
    def test_lte_nr_values_and_missing_nr(self):
        node = "MIN2748_B01"
        records = {
            f"ManagedElement={node},ENodeBFunction=1,EUtranCellFDD=L1": {},
            f"ManagedElement={node},GNBDUFunction=1,NRCellDU=N1": {},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "modump.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("logs/rnclog.txt", "\n".join([
                    "/cm/sysconread all", "0001: [default] 4631:1",
                    "0001: [LTE]", f"0001: [ManagedElement={node},Equipment=1]",
                    "/cm/sysconread all", "0001: [default] 4631:0",
                    "0001: [NR]", f"0001: [ManagedElement={node},Equipment=1]",
                ]))
            rows = audit_systemconstant(records, [node], [{"node": node, "path": path}])
            self.assertEqual([(r.mo, r.actual, r.status) for r in rows], [
                ("[LTE/NR]", "LTE 4631:1, NR 4631:0", "Mismatch")])
            scripts = generate_moshell_scripts(rows, folder, "MIN2748", "audit.xlsx")
            self.assertEqual(len(scripts), 1)
            with open(scripts[0], encoding="utf-8") as script:
                self.assertEqual(script.read().count("scw 4631:1"), 1)

            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("other.txt", "no rnclog")
            rows = audit_systemconstant(records, [node], [{"node": node, "path": path}])
            self.assertEqual([r.status for r in rows], ["Mismatch"])

            cmdump = os.path.join(folder, f"{node}_cmdump.zip")
            with zipfile.ZipFile(cmdump, "w") as archive:
                archive.writestr("export.xml", "<configData />")
            rows = audit_systemconstant(records, [node], [{"node": node, "path": cmdump}])
            self.assertEqual([r.status for r in rows], ["Match"])
            self.assertEqual([r.expected for r in rows], ["N/A"])
            self.assertEqual([r.remark for r in rows], ["N/A use cmdump"])

            gsm_records = {f"ManagedElement={node},BtsFunction=1": {}}
            rows = audit_systemconstant(gsm_records, [node], [])
            self.assertEqual([(r.expected, r.actual, r.status, r.remark) for r in rows],
                             [("N/A", "N/A", "Match", "Not Required - GSM-only node")])


if __name__ == "__main__":
    unittest.main()
