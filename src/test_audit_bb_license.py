import unittest

from audit.bb_license_audit import audit_bb_license, licence_hw_ids

def hup(installed):
    """A hupInfo struct string shaped like the one the modump parser produces."""
    return ("{gsmAllocated=N/A, licenseKeyId=CXC4012472, "
            "licenseKeyName=HWAC BB ABW UM 60 MHz, "
            f"totInstalled={installed}, totFree=N/A}}")


def records(installed_licence, product_name, node="MIN1_SITEB01", with_cap=True):
    out = {}
    if with_cap:
        out[f"ManagedElement={node},NodeSupport=1,CapacityUsage=1"] = {
            "capacityUsageId": "1",
            "hupInfo": hup(installed_licence),
        }
    out[f"ManagedElement={node},Equipment=1,FieldReplaceableUnit=BB-1"] = {
        "productData": "{productionDate=20260817, productName=" + product_name +
                       ", productNumber=KDU1370071/11}",
    }
    return out


class LicenceHwIdTests(unittest.TestCase):
    def test_reads_ip_parts_only(self):
        self.assertEqual(licence_hw_ids("totInstalled=IP:BB6621:1:1;"), ["BB6621"])
        # EP parts are pool entries without a hardware id
        self.assertEqual(licence_hw_ids("totInstalled=IP:RANP6672:1:4;EP::2:6;"), ["RANP6672"])
        self.assertEqual(licence_hw_ids("totInstalled=EP::1:10;"), [])
        self.assertEqual(licence_hw_ids("totInstalled=0"), [])

    def test_several_licences_deduplicated_in_order(self):
        hup = "totInstalled=IP:RANP6672:1:10;, totInstalled=IP:RANP6672:1:4;, totInstalled=IP:BB6621:1:1;"
        self.assertEqual(licence_hw_ids(hup), ["RANP6672", "BB6621"])


    def test_unallocated_keys_ignored(self):
        # MIN5290: the ABW key (BB6631) is allocated; the CBW/Layer keys name
        # RANP6655 but are only installed (totAllocated=N/A) - not in use.
        hup = ("{licenseKeyId=CXC4012472, lteAllocated=IP:BB6631:1:1;EP::1:9;, "
               "totAllocated=IP:BB6631:1:2;EP::1:9;, totFree=EP::1:1;, "
               "totInstalled=IP:BB6631:1:2;EP::1:10;} | "
               "{licenseKeyId=CXC4012473, lteAllocated=N/A, totAllocated=N/A, "
               "totFree=N/A, totInstalled=IP:RANP6655:1:5;} | "
               "{licenseKeyId=CXC4012474, totAllocated=N/A, totFree=N/A, "
               "totInstalled=IP:RANP6655:1:2;}")
        self.assertEqual(licence_hw_ids(hup), ["BB6631"])

    def test_no_allocated_key_falls_back_to_installed(self):
        hup = ("{totAllocated=N/A, totInstalled=IP:RANP6655:1:5;} | "
               "{totAllocated=0, totInstalled=IP:BB6631:1:1;}")
        self.assertEqual(licence_hw_ids(hup), ["RANP6655", "BB6631"])


class BbLicenceAuditTests(unittest.TestCase):
    def test_match_compares_on_product_number(self):
        rows = audit_bb_license(records("IP:RANP6672:1:10;", "RAN Processor 6672"))
        self.assertEqual([(r.expected, r.actual, r.status) for r in rows],
                         [("RAN Processor 6672", "RANP6672", "Match")])

    def test_mismatch_keeps_fru_as_expected_and_licence_as_actual(self):
        rows = audit_bb_license(records("IP:BB6621:1:1;", "Baseband 6631"))
        row = rows[0]
        self.assertEqual((row.expected, row.actual, row.status),
                         ("Baseband 6631", "BB6621", "Mismatch"))
        self.assertEqual(row.remark,
                         "LKF value is BB6621, actual BB installed is Baseband 6631.")

    def test_no_licence_installed_is_unresolved_not_mismatch(self):
        rows = audit_bb_license(records("0", "Baseband 6621"))
        self.assertEqual(rows[0].status, "NotFound")
        self.assertIn("possible LKF not installed or not detect. please verify",
                      rows[0].remark)
        self.assertIn("Actual BB installed is Baseband 6621", rows[0].remark)

    def test_cmdump_without_licence_attributes_says_so(self):
        rows = audit_bb_license(records("", "Baseband 6621", with_cap=False))
        self.assertEqual(rows[0].status, "NotFound")
        self.assertIn("modump", rows[0].remark)
        self.assertNotIn("LKF not installed", rows[0].remark)

    def test_missing_baseband_fru(self):
        recs = {"ManagedElement=MIN1_SITEB01,NodeSupport=1,CapacityUsage=1":
                {"hupInfo": hup("IP:BB6621:1:1;")}}
        rows = audit_bb_license(recs)
        self.assertEqual(rows[0].status, "MO_NotFound")

    def test_other_nodes_are_not_mixed_up(self):
        recs = records("IP:BB6621:1:1;", "Baseband 6621", node="MIN1_SITEB01")
        recs.update(records("IP:RANP6655:1:1;", "Baseband 6631", node="MIN1_SITEB02"))
        rows = audit_bb_license(recs, nodes=["MIN1_SITEB01", "MIN1_SITEB02"])
        self.assertEqual([(r.node, r.status) for r in rows],
                         [("MIN1_SITEB01", "Match"), ("MIN1_SITEB02", "Mismatch")])

    def test_node_without_baseband_is_skipped(self):
        recs = {"ManagedElement=BSC1,BtsFunction=1": {}}
        self.assertEqual(audit_bb_license(recs), [])


if __name__ == "__main__":
    unittest.main()
