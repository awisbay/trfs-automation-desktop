"""Exact GeranCell FDN from the non-table cmedit get, used by generated sets."""
import re
import unittest

from audit import audit_core
from audit.cmedit_source import FDN_MO, parse_cmedit_log, parse_fdn_lines

T8 = ("SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T8,SubNetwork=BSC,MeContext=MINVBS06,"
      "ManagedElement=MINVBS06,BscFunction=1,BscM=1,GeranCellM=1,GeranCell=%s")
TEMPLATE = ("SubNetwork=ONRM_ROOT_MO_R,SubNetwork=T7,SubNetwork=BSC,MeContext={bsc},"
            "ManagedElement={bsc},BscFunction=1,BscM=1,GeranCellM=1")
SITE_RX = re.compile(r"^M849[89]\D", re.I)
OUT = ("FDN : " + T8 % "M8498S1" + "\ngeranCellId : M8498S1\n\n"
       "FDN : " + T8 % "M8498S2" + "\ngeranCellId : M8498S2\n\n"
       "FDN : " + T8 % "M84981S1" + "\n\n"                 # foreign site
       "FDN : " + T8 % "M8498S1" + ",PhysicalData=1\n\n"     # child MO: ignored
       "3 instance(s)\n")


class ParseFdnTests(unittest.TestCase):
    def test_keeps_site_cells_with_bsc(self):
        recs = parse_fdn_lines(OUT, SITE_RX)
        self.assertEqual(sorted(recs), ["GeranCell=M8498S1", "GeranCell=M8498S2"])
        self.assertEqual(recs["GeranCell=M8498S1"]["__fdn__"], T8 % "M8498S1")
        self.assertEqual(recs["GeranCell=M8498S1"]["__bsc__"], "MINVBS06")

    def test_uploaded_log_block(self):
        log = ("##### NODECRAFT-CMEDIT MO=%s CMD=cmedit get *BS* "
               "GeranCell.(gerancellid==M8498*) #####\n%s" % (FDN_MO, OUT))
        recs = parse_cmedit_log(log, [], site_id="MIN849")
        self.assertEqual(audit_core.build_gsm_fdn_by_cell(recs),
                         {"M8498S1": T8 % "M8498S1", "M8498S2": T8 % "M8498S2"})


class BuildFdnTests(unittest.TestCase):
    def fdn(self, mo, param, by_cell):
        return audit_core._build_fdn(
            "MIN849_X", mo, param, "T8", TEMPLATE,
            gsm_child_map={"latitude": "PhysicalData"},
            bsc_by_cell={"M8498S1": "MINVBS06"}, default_bsc="<BSC>",
            gsm_fdn_by_cell=by_cell)

    def test_exact_fdn_wins_over_template(self):
        by = {"M8498S1": T8 % "M8498S1"}
        self.assertEqual(self.fdn("GeranCell=M8498S1", "userLabel", by),
                         T8 % "M8498S1")
        self.assertEqual(self.fdn("GeranCell=M8498S1", "latitude", by),
                         T8 % "M8498S1" + ",PhysicalData=1")
        self.assertEqual(self.fdn("GeranCell=M8498S1,ChannelGroup=0", "hsn", by),
                         T8 % "M8498S1" + ",ChannelGroup=0")

    def test_template_fallback_without_exact_fdn(self):
        self.assertEqual(
            self.fdn("GeranCell=M8498S1", "userLabel", {}),
            TEMPLATE.replace("{bsc}", "MINVBS06") + ",GeranCell=M8498S1")


if __name__ == "__main__":
    unittest.main()
