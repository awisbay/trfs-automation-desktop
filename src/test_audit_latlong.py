"""GeranCell DMS coordinates (CDD form vs the BSC's PhysicalData form) and the
truncated-dump rejection message."""
import os
import tempfile
import unittest
import zipfile

from audit.audit_core import _format_set_value, normalize_latlong
from audit.dump_selection import zip_problem


class DmsTests(unittest.TestCase):
    def test_bsc_format_two_decimal_seconds(self):
        # node values as the BSC reports them: E125-06-46.08, N06-04-15.97
        self.assertEqual(_format_set_value("125°6'46.08\"E", "latlong"), "E125-06-46.08")
        self.assertEqual(_format_set_value("6°4'15.97\"N", "latlong"), "N06-04-15.97")
        self.assertEqual(_format_set_value("124°3'3.2\"E", "latlong"), "E124-03-03.20")
        self.assertEqual(_format_set_value("124°3'3\"E", "latlong"), "E124-03-03.00")

    def test_seconds_with_bare_trailing_dot(self):
        # MIN849 GSM CDD: 123°45'18."E (18 s) — the BSC holds E123-45-18.00
        self.assertEqual(_format_set_value("123°45'18.\"E", "latlong"), "E123-45-18.00")
        self.assertEqual(normalize_latlong("123°45'18.\"E"),
                         normalize_latlong("E123-45-18.00"))

    def test_rounding_carries_into_the_minute(self):
        self.assertEqual(_format_set_value("124°59'59.999\"E", "latlong"), "E125-00-00.00")

    def test_south_west_and_compare(self):
        self.assertEqual(normalize_latlong("N08-05-11.93"),
                         normalize_latlong("8°5'11.93\"N"))
        self.assertTrue(normalize_latlong("S01-00-00.00").startswith("-"))


class TruncatedZipTests(unittest.TestCase):
    def test_complete_zip_has_no_problem(self):
        path = os.path.join(tempfile.mkdtemp(), "ok_modump.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("a.txt", "x" * 100)
        self.assertEqual(zip_problem(path), "")

    def test_cut_off_zip_names_the_truncated_member(self):
        path = os.path.join(tempfile.mkdtemp(), "cut_modump.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("model.xml.gz", b"m" * 200)
            z.writestr("node_dcg_k.log.gz", b"d" * 1000)
        data = open(path, "rb").read()
        cut = data.find(b"node_dcg_k.log.gz") + len("node_dcg_k.log.gz") + 400
        open(path, "wb").write(data[:cut])          # download cut mid-member
        why = zip_problem(path)
        self.assertIn("zip is incomplete", why)
        self.assertIn("node_dcg_k.log.gz cut at", why)
        self.assertIn("complete members: model.xml.gz", why)

    def test_non_zip_is_not_checked(self):
        self.assertEqual(zip_problem("dump.xml"), "")


if __name__ == "__main__":
    unittest.main()
