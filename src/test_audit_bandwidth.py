import unittest
from audit.bandwidth_audit import audit_bandwidth_license, RULES


class BandwidthTests(unittest.TestCase):
    def fixture(self):
        r = {
            'ManagedElement=BB1,EUtranCellFDD=F': {'dlChannelBandwidth':'20000', 'ulChannelBandwidth':'20000', 'sectorCarrierRef':'SectorCarrier=F'},
            'ManagedElement=BB1,SectorCarrier=F': {'sectorFunctionRef':'SectorEquipmentFunction=F'},
            'ManagedElement=BB1,SectorEquipmentFunction=F': {'rfBranchRef':'RfBranch=1'},
            'ManagedElement=BB1,RfBranch=1': {'rfPortRef':'FieldReplaceableUnit=RADIO,RfPort=A'},
            'ManagedElement=BB1,FieldReplaceableUnit=RADIO': {'productData':'{productName=Radio 4499}'},
            'ManagedElement=BB1,EUtranCellTDD=T': {'channelBandwidth':'20000','sectorCarrierRef':'SectorCarrier=T'},
            'ManagedElement=BB1,SectorCarrier=T': {'sectorFunctionRef':'SectorEquipmentFunction=T'},
            'ManagedElement=BB1,SectorEquipmentFunction=T': {'rfBranchRef':'FieldReplaceableUnit=AIR,Transceiver=1'},
            'ManagedElement=BB1,FieldReplaceableUnit=AIR,Transceiver=1': {'id':'1'},
            'ManagedElement=BB1,FieldReplaceableUnit=AIR': {'productName':'AIR 6419'},
            'ManagedElement=BB1,NRCellDU=N': {'bandList':'41;90', 'nRSectorCarrierRef':'NRSectorCarrier=N'},
            'ManagedElement=BB1,NRSectorCarrier=N': {'bSChannelBwDL':'40', 'bSChannelBwUL':'40','sectorEquipmentFunctionRef':'SectorEquipmentFunction=T'},
        }
        for k in RULES:
            r['ManagedElement=BB1,Lm=1,CapacityState=CXC401'+k]={'grantedCapacityLevel':'100'}
        return r

    def test_fdd_tdd_aas_and_non_aas(self):
        r = self.fixture()
        rows = {x.mo[-4:]:x for x in audit_bandwidth_license(r)}
        for k, expected in [('1622','6'),('2367','4'),('2203','2'),('2290','4'),('2283','4'),('2322','0')]:
            self.assertEqual((rows[k].expected,rows[k].status),(expected,'Match'))
        del r['ManagedElement=BB1,FieldReplaceableUnit=AIR']
        rows = {x.mo[-4:]:x for x in audit_bandwidth_license(r)}
        self.assertEqual(rows['2203'].status,'NotFound')
        self.assertEqual(rows['1622'].status,'Match')

    def test_cmdump_and_modump_same_results(self):
        import tempfile
        from pathlib import Path
        from test_audit_termpoint import cmdump
        from audit.dump_parser import parse_dump
        records = self.fixture()
        expected = [(r.mo, r.expected, r.actual, r.status) for r in audit_bandwidth_license(records)]
        dcg = '\n'.join('MO  '+dn+'\n'+'\n'.join(f'{k}  {v}' for k,v in a.items())
                        for dn,a in records.items()).encode()
        with tempfile.TemporaryDirectory() as folder:
            for suffix,payload in [('xml',cmdump(records)),('log',dcg)]:
                path=Path(folder)/('dump.'+suffix)
                path.write_bytes(payload)
                self.assertEqual([(r.mo,r.expected,r.actual,r.status)
                                  for r in audit_bandwidth_license(parse_dump(str(path)))],expected)

    def test_missing_nr_bandwidth_and_insufficient_capacity(self):
        r = self.fixture()
        r['ManagedElement=BB1,Lm=1,CapacityState=CXC4011622']['grantedCapacityLevel']='5'
        del r['ManagedElement=BB1,NRSectorCarrier=N']['bSChannelBwDL']
        rows={x.mo[-4:]:x for x in audit_bandwidth_license(r)}
        self.assertEqual(rows['1622'].status,'Mismatch')
        self.assertEqual(rows['2290'].status,'NotFound')

    def test_mixed_nr_tdd_sample_3785(self):
        records = self.fixture()
        for i in (1, 3):
            records[f'ManagedElement=BB1,NRCellDU=N{i}'] = {
                'bandList':'41', 'nRSectorCarrierRef':f'NRSectorCarrier=N{i}'}
            records[f'ManagedElement=BB1,NRSectorCarrier=N{i}'] = {
                'bSChannelBwDL':'40','bSChannelBwUL':'40',
                'sectorEquipmentFunctionRef':'SectorEquipmentFunction=F'}
        for key, grant in [('2322','16'),('2283','4'),('2290','12')]:
            records['ManagedElement=BB1,Lm=1,CapacityState=CXC401'+key]['grantedCapacityLevel']=grant
        rows={r.mo[-4:]:r for r in audit_bandwidth_license(records)}
        for key, expected in [('2322','16'),('2283','4'),('2290','12')]:
            self.assertEqual((rows[key].expected,rows[key].status),(expected,'Match'))
        records['ManagedElement=BB1,Lm=1,CapacityState=CXC4012322']['grantedCapacityLevel']='15'
        rows={r.mo[-4:]:r for r in audit_bandwidth_license(records)}
        self.assertEqual(rows['2322'].status,'Mismatch')
        self.assertEqual(rows['2283'].status,'Match')

    def test_no_correction_generated(self):
        from audit.audit_core import _NON_SETTABLE_CATEGORIES
        self.assertIn('bandwidth-license', _NON_SETTABLE_CATEGORIES)
