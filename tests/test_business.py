
import copy,json,os,unittest,tempfile,subprocess,sys,hashlib
from pathlib import Path
from venue_occupancy.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_top_field(self):
        d=sample();d['unexpected']=1;self.bad(d)

    def test_capacity(self):
        r=analyze(sample());h=r['venues'][0];self.assertEqual((h['peak'],h['over_capacity_ticks'],h['excess_unit_ticks'],h['booked_unit_ticks']),(4,5,5,55));self.assertEqual(r['venues'][1]['segments'],[{'start':0,'end':20,'occupancy':0,'excess':0}])
    def test_order(self):
        d=sample();d['bookings'].reverse();d['venues'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_touching(self):
        d=sample();d['bookings']=[{'id':'a','venue':'hall','start':0,'end':10,'quantity':3},{'id':'b','venue':'hall','start':10,'end':20,'quantity':3}];r=analyze(d)['venues'][0];self.assertEqual(r['over_capacity_ticks'],0);self.assertEqual(len(r['segments']),1)
    def test_outside(self):
        d=sample();d['bookings']=[{'id':'x','venue':'hall','start':20,'end':30,'quantity':10}];self.assertEqual(analyze(d)['venues'][0]['peak'],0)
    def test_zero_capacity(self):
        d=sample();d['venues'][0]['capacity']=0;r=analyze(d)['venues'][0];self.assertEqual(r['excess_unit_ticks'],55);self.assertEqual(r['over_capacity_ticks'],20)
    def test_invalid_interval(self):
        d=sample();d['bookings'][0]['end']=0;self.bad(d)
    def test_unknown_venue(self):
        d=sample();d['bookings'][0]['venue']='x';self.bad(d)
    def test_duplicate(self):
        d=sample();d['bookings'].append(d['bookings'][0]);self.bad(d)

class Command(unittest.TestCase):
    def call(self,source,output):
        environment=dict(os.environ)
        source_root=str(ROOT/'src')
        environment['PYTHONPATH']=source_root+os.pathsep+environment.get('PYTHONPATH','')
        return subprocess.run([sys.executable,'-m','venue_occupancy.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True,env=environment)
    def test_installed_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();report=json.loads(first);self.assertEqual(report['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"x":1,"x":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
