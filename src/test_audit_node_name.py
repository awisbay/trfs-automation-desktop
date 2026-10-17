"""networkManagedElementId vs eNodeBName / gNodeBName / gsmNodeName."""
import unittest

from audit.audit_core import AuditItem
from audit.node_name_audit import audit_node_name

NODE = "MIN131_ALEJALB02"


def dump(nme=NODE, lte=True, nr=False, gsm=False, enb="404524", gnb="902430",
         node=NODE):
    me = "SubNetwork=T7,MeContext=%s,ManagedElement=%s" % (node, node)
    recs = {me: {"networkManagedElementId": nme} if nme is not None else {}}
    if lte:
        recs[me + ",ENodeBFunction=1"] = {"eNBId": enb}
        recs[me + ",ENodeBFunction=1,EUtranCellFDD=ALEJALL-1"] = {}
    if nr:
        recs[me + ",GNBDUFunction=1"] = {"gNBId": gnb}
        recs[me + ",GNBDUFunction=1,NRCellDU=ALEJALN-401"] = {}
    if gsm:
        recs[me + ",BtsFunction=1,GsmSector=1"] = {}
    return recs


def item(tech, param, expected, node):
    return AuditItem(category="node", tech=tech, mo_local="x", parameter=param,
                     expected=expected, key=node, source="CDD", node=node)


def lte(name=NODE, enb="404524"):
    return item("lte_nr", "eNBId", enb, name)


def nr(name=NODE, gnb="902430"):
    return item("nr", "gNBId", gnb, name)


def gsm(name=NODE):
    return item("gsm", "userLabel", "ALEJALXFWLY", name)


class NodeNameTests(unittest.TestCase):
    def one(self, recs, items):
        rows = audit_node_name(recs, items)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_gsm_and_lte_all_equal(self):
        r = self.one(dump(gsm=True), [lte(), gsm()])
        self.assertEqual(r.status, "Match")
        self.assertEqual(r.parameter, "networkManagedElementId")
        self.assertEqual(r.expected, "eNodeBName=%s | gsmNodeName=%s" % (NODE, NODE))
        self.assertEqual(r.actual, NODE)

    def test_gsm_name_differs(self):
        r = self.one(dump(gsm=True), [lte(), gsm("MIN131_ALEJALB01")])
        self.assertEqual(r.status, "Mismatch")
        self.assertIn("gsmNodeName (GSM CDD) = MIN131_ALEJALB01", r.remark)
        self.assertNotIn("eNodeBName", r.remark.split("differs:")[1])

    def test_lte_row_found_by_enodebid_even_with_other_name(self):
        # the CDD row carries this node's ENodeBID but another name
        r = self.one(dump(), [lte("MIN131_ALEJALB03")])
        self.assertEqual(r.status, "Mismatch")
        self.assertIn("eNodeBName (LTE CDD, ENodeBID 404524) = MIN131_ALEJALB03",
                      r.remark)

    def test_network_managed_element_id_differs(self):
        r = self.one(dump(nme="MIN131_OLDNAME", gsm=True), [lte(), gsm()])
        self.assertEqual(r.status, "Mismatch")
        self.assertEqual(r.actual, "MIN131_OLDNAME")
        self.assertIn("eNodeBName", r.remark)
        self.assertIn("gsmNodeName", r.remark)

    def test_nr_checked_when_node_has_nr(self):
        r = self.one(dump(lte=False, nr=True), [nr("MIN131_ALEJALB09")])
        self.assertEqual(r.status, "Mismatch")
        self.assertIn("gNodeBName (NR CDD, gNodeBID 902430)", r.remark)

    def test_only_rats_on_the_node_are_checked(self):
        # GSM CDD has a different name, but this node carries no GSM
        r = self.one(dump(gsm=False), [lte(), gsm("MIN131_ALEJALB01")])
        self.assertEqual(r.status, "Match")
        self.assertNotIn("gsmNodeName", r.expected)

    def test_missing_attribute_is_not_found(self):
        r = self.one(dump(nme=None), [lte()])
        self.assertEqual(r.status, "NotFound")
        self.assertIn("networkManagedElementId not found", r.remark)

    def test_no_cdd_row(self):
        r = self.one(dump(gsm=True), [])
        self.assertEqual(r.status, "NotFound")
        self.assertIn("no LTE CDD row for ENodeBID 404524", r.remark)
        self.assertIn("no GSM CDD row for site MIN131", r.remark)

    def test_two_gsm_nodes_at_site_picks_own_name(self):
        r = self.one(dump(lte=False, gsm=True),
                     [gsm(NODE), gsm("MIN131_ALEJALB01")])
        self.assertEqual(r.status, "Match")


if __name__ == "__main__":
    unittest.main()
