"""End-to-end CLI tests, atomic publication and legacy-command isolation."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def cli(*args):
    return subprocess.run([sys.executable,'-m','venue_energy.cli',*map(str,args)],
                          capture_output=True,text=True,
                          env={**os.environ,'PYTHONPATH':str(ROOT/'src')})

def write(path,body):
    Path(path).write_text(json.dumps(body) if not isinstance(body,str) else body)

EPOCH_MIN=946684800
EPOCH_MAX_EXCLUSIVE=4102444800

class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        write(self.tmp/'meters.json',{'meters':[
            {'meter_id':'m','venue':'v','modulus_wh':10000,'max_watts':1000}]})
        write(self.tmp/'tariffs.json',{'tariffs':[
            {'venue':'v','start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
             'cost_micro_per_kwh':100000,'carbon_mg_per_kwh':400000}]})
        (self.tmp/'readings.csv').write_text(
            'meter_id,read_id,observed_at,register_wh\n'
            'm,r2,%d,100\nm,r1,%d,0\n'%(EPOCH_MIN+3600,EPOCH_MIN))

    def tearDown(self):self.dir.cleanup()

    def test_audit_writes_sorted_report_and_stdout_matches_file(self):
        out=self.tmp/'report.json'
        result=cli('audit','--meters',self.tmp/'meters.json','--readings',self.tmp/'readings.csv',
                   '--tariffs',self.tmp/'tariffs.json','--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(out.read_text())
        self.assertEqual(json.loads(result.stdout),report)
        self.assertEqual(report['schema_version'],1)
        self.assertEqual(set(report['input_sha256']),{'meters.json','readings.csv','tariffs.json'})
        self.assertEqual(report['months'][0]['wh'],100)

    def test_failure_keeps_previous_report(self):
        out=self.tmp/'report.json';out.write_text('PREVIOUS')
        bad=self.tmp/'bad.csv';bad.write_text('meter_id,read_id,observed_at\n')
        result=cli('audit','--meters',self.tmp/'meters.json','--readings',bad,
                   '--tariffs',self.tmp/'tariffs.json','--output',out)
        self.assertEqual(result.returncode,2)
        self.assertEqual(out.read_text(),'PREVIOUS')

    def test_output_equal_to_input_rejected(self):
        result=cli('audit','--meters',self.tmp/'meters.json','--readings',self.tmp/'readings.csv',
                   '--tariffs',self.tmp/'tariffs.json','--output',self.tmp/'meters.json')
        self.assertEqual(result.returncode,2)

    def test_symlink_output_rejected(self):
        target=self.tmp/'target.json';target.write_text('PREVIOUS')
        link=self.tmp/'link.json';link.symlink_to(target)
        result=cli('audit','--meters',self.tmp/'meters.json','--readings',self.tmp/'readings.csv',
                   '--tariffs',self.tmp/'tariffs.json','--output',link)
        self.assertEqual(result.returncode,2)
        self.assertEqual(target.read_text(),'PREVIOUS')

    def test_missing_subcommand_exits_2(self):
        self.assertEqual(cli().returncode,2)

    def test_legacy_venue_occupancy_command_unchanged(self):
        result=subprocess.run(
            [sys.executable,'-m','venue_occupancy.cli',
             '--source',ROOT/'fixtures'/'input.json','--output',self.tmp/'legacy.json'],
            capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(ROOT/'src')})
        self.assertEqual(result.returncode,0,result.stderr)
        legacy=json.loads((self.tmp/'legacy.json').read_text())
        self.assertEqual(set(legacy),{'schema_version','source_sha256','result'})
        self.assertIn('venues',legacy['result'])

class CrossBoundaryTests(unittest.TestCase):
    """Remainder allocation across tariff and month boundaries via the CLI."""

    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        import calendar
        self.t0=calendar.timegm((2000,1,31,23,30,0,0,0,0))   # 30 min before month end
        self.t1=self.t0+7200                                   # 90 min into February

    def tearDown(self):self.dir.cleanup()

    def test_remainders_split_across_month_and_tariff(self):
        cut=self.t0+3600                                       # tariff change at 00:30
        write(self.tmp/'meters.json',{'meters':[
            {'meter_id':'m','venue':'v','modulus_wh':100000,'max_watts':1000}]})
        write(self.tmp/'tariffs.json',{'tariffs':[
            {'venue':'v','start':946684800,'end':cut,'cost_micro_per_kwh':100,'carbon_mg_per_kwh':10},
            {'venue':'v','start':cut,'end':4102444800,'cost_micro_per_kwh':300,'carbon_mg_per_kwh':30}]})
        # 10 Wh over 7200 s: segments 1800,1800,3600 -> floors 2,2,5 rem 1 -> first segment.
        (self.tmp/'readings.csv').write_text(
            'meter_id,read_id,observed_at,register_wh\n'
            'm,r1,%d,0\nm,r2,%d,10\n'%(self.t0,self.t1))
        out=self.tmp/'report.json'
        result=cli('audit','--meters',self.tmp/'meters.json','--readings',self.tmp/'readings.csv',
                   '--tariffs',self.tmp/'tariffs.json','--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(out.read_text())
        rows={(r['year'],r['month']):r for r in report['months']}
        jan,feb=rows[(2000,'01')],rows[(2000,'02')]
        self.assertEqual(jan['wh']+feb['wh'],10)
        self.assertEqual(jan['wh'],3)          # 1800s piece: 2 floor + earliest remainder
        self.assertEqual(feb['wh'],7)          # 1800s -> 2, 3600s -> 5
        self.assertTrue(jan['complete'] and feb['complete'])
        # Exact products: Jan 3 Wh over 1800 s at rate 100 micro/kWh.
        self.assertEqual(jan['cost_micro'],(2*3*100*1800+3_600_000)//(2*3_600_000))

class FixtureTests(unittest.TestCase):
    """The committed hand-made fixture under fixtures/energy."""

    def test_committed_fiature_settles_as_documented(self):
        base=ROOT/'fixtures'/'energy'
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'report.json'
            result=cli('audit','--meters',base/'meters.json','--readings',base/'readings.csv',
                       '--tariffs',base/'tariffs.json','--output',out)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(out.read_text())
            rows={(r['venue'],r['year'],r['month']):r for r in report['months']}
            self.assertEqual(report['reading_rows'],9)
            hall=rows[('hall',2000,'01')]
            self.assertEqual(hall['wh'],3100)              # 2000+1000 settled, 100 solar
            self.assertFalse(hall['complete'])             # long solar interval ambiguous
            annex=rows[('annex',2000,'01')]
            self.assertEqual(annex['wh'],30)
            self.assertTrue(annex['complete'])

if __name__=='__main__':unittest.main()
