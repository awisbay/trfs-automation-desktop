import tempfile
import unittest
from pathlib import Path

from audit.dump_parser import parse_dump
from audit.trx_license_audit import audit_trx_license, LICENSES
from test_audit_termpoint import cmdump


def sample():
    records = {f"ManagedElement=GSM1,BtsFunction=1,GsmSector=S{i // 4},Trx={i % 4}": {}
               for i in range(13)}
    for code in LICENSES:
        for cls, ident in (("CapacityState", code), ("CapacityKey", code + "_2")):
            records[f"ManagedElement=GSM1,Lm=1,{cls}={ident}"] = {"grantedCapacityLevel": "13"}
    return records


class TrxLicenseTests(unittest.TestCase):
    def test_formats_and_no_double_count(self):
        records = sample()
        payload = "\n".join("MO  " + dn + "\n" + "\n".join(f"{k}  {v}" for k, v in attrs.items())
                            for dn, attrs in records.items()).encode()
        with tempfile.TemporaryDirectory() as folder:
            for suffix, data in (("xml", cmdump(records)), ("log", payload)):
                path = Path(folder) / ("dump." + suffix)
                path.write_bytes(data)
                rows = audit_trx_license(parse_dump(str(path)))
                self.assertEqual([(r.expected, r.actual, r.status) for r in rows],
                                 [("13", "13", "Match")] * 2)

    def test_shortage_and_node_isolation(self):
        records = sample()
        records["ManagedElement=GSM1,Lm=1,CapacityState=CXC4012021"]["grantedCapacityLevel"] = "12"
        records["ManagedElement=OTHER,BtsFunction=1,GsmSector=S1,Trx=0"] = {}
        rows = audit_trx_license(records, nodes=["GSM1", "LTE1"])
        self.assertEqual([r.status for r in rows], ["Mismatch", "Match"])
        self.assertIn("shortage 1", rows[0].remark)

    def test_missing_invalid_and_key_fallback(self):
        records = sample()
        del records["ManagedElement=GSM1,Lm=1,CapacityState=CXC4012021"]
        self.assertEqual(audit_trx_license(records)[0].status, "Match")
        records["ManagedElement=GSM1,Lm=1,CapacityKey=CXC4012021_2"]["grantedCapacityLevel"] = "NaN"
        self.assertEqual(audit_trx_license(records)[0].status, "NotFound")
        del records["ManagedElement=GSM1,Lm=1,CapacityKey=CXC4012021_2"]
        self.assertEqual(audit_trx_license(records)[0].status, "NotFound")

    def test_no_license_set_commands(self):
        from audit.audit_core import generate_moshell_scripts, generate_cmedit_scripts
        records = sample()
        records["ManagedElement=GSM1,Lm=1,CapacityState=CXC4012021"]["grantedCapacityLevel"] = "1"
        with tempfile.TemporaryDirectory() as folder:
            rows = audit_trx_license(records)
            self.assertEqual(generate_moshell_scripts(rows, folder, "SITE", "audit.xlsx"), [])
            self.assertEqual(generate_cmedit_scripts(rows, folder, "SITE", "audit.xlsx"), [])
