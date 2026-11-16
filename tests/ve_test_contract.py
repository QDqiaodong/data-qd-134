"""Contract rejection tests: malformed inputs, gaps and overlaps."""
import json,tempfile,unittest
from pathlib import Path
from venue_energy import engine,contract

EPOCH_MIN=946684800
EPOCH_MAX_EXCLUSIVE=4102444800

class Case(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.meters=[{'meter_id':'m','venue':'v','modulus_wh':1000,'max_watts':100}]
        self.tariffs=[{'venue':'v','start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
                       'cost_micro_per_kwh':10,'carbon_mg_per_kwh':20}]
        self.rows=[('m','r1',EPOCH_MIN,0),('m','r2',EPOCH_MIN+3600,50)]

    def tearDown(self):self.dir.cleanup()

    def write(self,meters=None,tariffs=None,rows=None):
        meters=self.meters if meters is None else meters
        tariffs=self.tariffs if tariffs is None else tariffs
        rows=self.rows if rows is None else rows
        (self.tmp/'meters.json').write_text(json.dumps({'meters':meters}))
        (self.tmp/'tariffs.json').write_text(json.dumps({'tariffs':tariffs}))
        with open(self.tmp/'readings.csv','w',newline='') as f:
            f.write('meter_id,read_id,observed_at,register_wh\n')
            for row in rows:f.write(','.join(map(str,row))+'\n')

    def run_audit(self):
        return engine.audit(self.tmp/'meters.json',self.tmp/'readings.csv',
                            self.tmp/'tariffs.json',workdir=self.tmp)

    def test_happy_path(self):
        self.write();report=self.run_audit()
        self.assertEqual(len(report['months']),1)
        self.assertEqual(report['months'][0]['complete'],True)

    def test_meters_document_must_be_strict(self):
        self.write()
        (self.tmp/'meters.json').write_text(json.dumps({'meters':[],'extra':1}))
        with self.assertRaises(ValueError):self.run_audit()
        (self.tmp/'meters.json').write_text(json.dumps({'meters':[
            {'meter_id':'m','venue':'v','modulus_wh':0,'max_watts':100}]}))
        with self.assertRaises(ValueError):self.run_audit()

    def test_duplicate_meter_id_rejected(self):
        self.write(meters=self.meters*2)
        with self.assertRaises(ValueError):self.run_audit()

    def test_register_outside_modulus_rejected(self):
        self.write(rows=[('m','r1',EPOCH_MIN,1000)])
        with self.assertRaises(ValueError):self.run_audit()

    def test_unknown_meter_in_reading_rejected(self):
        self.write(rows=[('ghost','r1',EPOCH_MIN,1)])
        with self.assertRaises(ValueError):self.run_audit()

    def test_timestamp_out_of_range_rejected(self):
        self.write(rows=[('m','r1',EPOCH_MAX_EXCLUSIVE,1)])
        with self.assertRaises(ValueError):self.run_audit()
        self.write(rows=[('m','r1',EPOCH_MIN-1,1)])
        with self.assertRaises(ValueError):self.run_audit()

    def test_empty_tariffs_for_venue_rejected(self):
        self.write(tariffs=[])
        with self.assertRaises(ValueError):self.run_audit()

    def test_overlapping_tariffs_rejected(self):
        self.write(tariffs=[
            {'venue':'v','start':EPOCH_MIN,'end':EPOCH_MIN+7200,
             'cost_micro_per_kwh':1,'carbon_mg_per_kwh':1},
            {'venue':'v','start':EPOCH_MIN+3600,'end':EPOCH_MAX_EXCLUSIVE,
             'cost_micro_per_kwh':2,'carbon_mg_per_kwh':2}])
        with self.assertRaises(ValueError):self.run_audit()

    def test_tariff_gap_over_interval_rejected(self):
        gap=EPOCH_MIN+3600
        self.write(
            rows=[('m','r1',EPOCH_MIN,0),('m','r2',EPOCH_MIN+5400,10)],
            tariffs=[
            {'venue':'v','start':EPOCH_MIN,'end':gap,
             'cost_micro_per_kwh':1,'carbon_mg_per_kwh':1},
            {'venue':'v','start':gap+1800,'end':EPOCH_MAX_EXCLUSIVE,
             'cost_micro_per_kwh':2,'carbon_mg_per_kwh':2}])
        with self.assertRaises(ValueError):self.run_audit()

    def test_touching_half_open_tariffs_are_accepted(self):
        cut=EPOCH_MIN+3600
        self.write(tariffs=[
            {'venue':'v','start':EPOCH_MIN,'end':cut,
             'cost_micro_per_kwh':1,'carbon_mg_per_kwh':1},
            {'venue':'v','start':cut,'end':EPOCH_MAX_EXCLUSIVE,
             'cost_micro_per_kwh':2,'carbon_mg_per_kwh':2}])
        report=self.run_audit()
        self.assertEqual(report['months'][0]['complete'],True)

    def test_negative_rates_rejected(self):
        self.write(tariffs=[{'venue':'v','start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
                             'cost_micro_per_kwh':-1,'carbon_mg_per_kwh':20}])
        with self.assertRaises(ValueError):self.run_audit()

    def test_duplicate_read_id_ignored_conflict_rejected(self):
        self.write(rows=[('m','r1',EPOCH_MIN,0),('m','r1',EPOCH_MIN,0),
                         ('m','r2',EPOCH_MIN+3600,50)])
        report=self.run_audit()
        self.assertEqual(report['reading_rows'],3)
        self.write(rows=[('m','r1',EPOCH_MIN,0),('m','r1',EPOCH_MIN,9)])
        with self.assertRaises(ValueError):self.run_audit()

    def test_same_timestamp_different_register_rejected(self):
        self.write(rows=[('m','r1',EPOCH_MIN,0),('m','r2',EPOCH_MIN,9)])
        with self.assertRaises(ValueError):self.run_audit()

    def test_csv_header_and_shape_enforced(self):
        self.write()
        (self.tmp/'readings.csv').write_text('meter_id,read_id,observed_at\n')
        with self.assertRaises(ValueError):self.run_audit()
        (self.tmp/'readings.csv').write_text(
            'meter_id,read_id,observed_at,register_wh\nm,r1,notanint,1\n')
        with self.assertRaises(ValueError):self.run_audit()

    def test_duplicate_json_key_rejected(self):
        self.write()
        (self.tmp/'meters.json').write_text(
            '{"meters":[],"meters":[]}')
        with self.assertRaises(ValueError):self.run_audit()

    def test_empty_readings_yield_no_months(self):
        self.write(rows=[])
        report=self.run_audit()
        self.assertEqual(report['months'],[])
        self.assertEqual(report['reading_rows'],0)

    def test_zero_watt_meter_only_settles_zero_energy(self):
        meters=[{'meter_id':'z','venue':'v','modulus_wh':100,'max_watts':0}]
        self.write(meters=meters,rows=[('z','r1',EPOCH_MIN,5),('z','r2',EPOCH_MIN+3600,5)])
        report=self.run_audit()
        self.assertEqual(report['months'][0]['wh'],0)
        self.assertTrue(report['months'][0]['complete'])
        # Any positive consumption is unfeasible for a zero-watt meter.
        self.write(meters=meters,rows=[('z','r1',EPOCH_MIN,5),('z','r2',EPOCH_MIN+3600,6)])
        report=self.run_audit()
        self.assertEqual(report['months'][0]['wh'],0)
        self.assertFalse(report['months'][0]['complete'])

    def test_blank_line_and_bom_rejected(self):
        self.write()
        (self.tmp/'readings.csv').write_text(
            'meter_id,read_id,observed_at,register_wh\n\nm,r1,%d,1\n'%EPOCH_MIN)
        with self.assertRaises(ValueError):self.run_audit()
        (self.tmp/'readings.csv').write_bytes(
            b'\xef\xbb\xbfmeter_id,read_id,observed_at,register_wh\nm,r1,'
            +str(EPOCH_MIN).encode()+b',1\n')
        with self.assertRaises(ValueError):self.run_audit()

if __name__=='__main__':unittest.main()
