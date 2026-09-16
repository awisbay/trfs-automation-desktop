import tempfile
import unittest
from pathlib import Path

from audit.dump_parser import parse_dump
from audit.power_audit import audit_power_license
from test_audit_termpoint import cmdump


class PowerAuditTests(unittest.TestCase):
    def test_user_example_both_formats(self):
        records = {}
        values = [80000] * 15 + [100000] * 3 + [71000] * 6
        values += [100000] * 3 + [142000] * 3
        for i, value in enumerate(values):
            cls = "SectorCarrier" if i < 24 else "NRSectorCarrier"
            records[f"ManagedElement=BB1,{cls}={i}"] = {"configuredMaxTxPower": str(value)}
        records["ManagedElement=BB1,SystemFunctions=1,Lm=1,CapacityState=CXC4012338"] = {"grantedCapacityLevel": "132"}
        dcg = "\n".join("MO  " + dn + "\n" + "\n".join(f"{k}  {v}" for k, v in a.items())
                        for dn, a in records.items()).encode()
        with tempfile.TemporaryDirectory() as folder:
            for kind, payload in (("xml", cmdump(records)), ("log", dcg)):
                with self.subTest(kind=kind):
                    path = Path(folder) / ("dump." + kind)
                    path.write_bytes(payload)
                    rows, evidence = audit_power_license(parse_dump(str(path)))
                    self.assertEqual((rows[0].expected, rows[0].actual, rows[0].status),
                                     ("132.6", "132", "Mismatch"))
                    self.assertEqual(len(evidence), 30)

    def test_gsm_and_node_isolation_exact_precision(self):
        records = {
            "ManagedElement=BB1,GsmSector=S1,Trx=0": {"configuredMaxTxPower": "20000"},
            "ManagedElement=BB1,Lm=1,CapacityState=CXC4012338": {"grantedCapacityLevel": "1"},
            "ManagedElement=BB2,SectorCarrier=S1": {"configuredMaxTxPower": "20001"},
            "ManagedElement=BB2,Lm=1,CapacityState=CXC4012338": {"grantedCapacityLevel": "1"},
        }
        rows, _ = audit_power_license(records)
        self.assertEqual([(r.expected, r.status) for r in rows], [("1", "Match"), ("1.00005", "Mismatch")])

    def test_incomplete_data_never_passes(self):
        for value in ("", "-1", "NaN", "Infinity", "not-a-number"):
            rows, _ = audit_power_license({
                "ManagedElement=BB1,SectorCarrier=1": {"configuredMaxTxPower": value},
                "ManagedElement=BB1,Lm=1,CapacityState=CXC4012338": {"grantedCapacityLevel": "10"}})
            self.assertEqual(rows[0].status, "NotFound")
        rows, _ = audit_power_license({}, nodes=["MISSING"])
        self.assertEqual(rows[0].status, "NotFound")

    def test_excel_and_no_license_correction(self):
        from audit.audit_core import write_excel, generate_moshell_scripts, generate_cmedit_scripts
        from openpyxl import load_workbook
        rows, evidence = audit_power_license({
            "ManagedElement=BB1,SectorCarrier=1": {"configuredMaxTxPower": "426000"},
            "ManagedElement=BB1,Lm=1,CapacityState=CXC4012338": {"grantedCapacityLevel": "21"}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.xlsx"
            write_excel(rows, str(path), {}, power_results=rows, power_evidence=evidence)
            book = load_workbook(path)
            self.assertEqual(book["Summary"].title, "Summary")
            self.assertEqual(book["Power License"]["D2"].value, -0.3)
            self.assertEqual(book["Power MO Detail"].max_row, 2)
            book.close()
            self.assertEqual(generate_moshell_scripts(rows, folder, "SITE", str(path)), [])
            self.assertEqual(generate_cmedit_scripts(rows, folder, "SITE", str(path)), [])


if __name__ == "__main__":
    unittest.main()
