"""End-to-end CLI: legacy one-shot invocation plus init/apply/export."""
import hashlib,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def cli(*args,cwd=ROOT):
    return subprocess.run([sys.executable,'-m','venue_occupancy.cli',*map(str,args)],
                          capture_output=True,text=True,cwd=cwd,
                          env={**os.environ,'PYTHONPATH':str(ROOT/'src')})

def write(path,body):
    Path(path).write_text(json.dumps(body) if not isinstance(body,str) else body)
    return path

BASE={'horizon':{'start':0,'end':20},
      'venues':[{'id':'hall','capacity':3},{'id':'annex','capacity':1}],
      'bookings':[{'id':'A','venue':'hall','start':0,'end':10,'quantity':2}]}

class Legacy(unittest.TestCase):
    def test_legacy_invocation_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'report.json';source=ROOT/'fixtures/input.json'
            result=cli('--source',source,'--output',out)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(out.read_text())
            self.assertEqual(set(report),{'schema_version','source_sha256','result'})
            self.assertEqual(report['source_sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report,json.loads(result.stdout))
            before=out.read_bytes()
            report['result']['venues'][0]['peak']=999
            out.write_text(json.dumps(report))
            self.assertEqual(cli('--source',source,'--output',out).returncode,0)
            self.assertEqual(json.loads(out.read_text())['result']['venues'][0]['peak'],4)
    def test_legacy_requires_both_flags(self):
        self.assertEqual(cli('--source',ROOT/'fixtures/input.json').returncode,2)
        self.assertEqual(cli('--db','x.db','--source',ROOT/'fixtures/input.json').returncode,2)

class Dataset(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=self.tmp/'data.db'
        self.init=write(self.tmp/'init.json',BASE)
    def tearDown(self):self.dir.cleanup()
    def ok(self,*args):
        result=cli(*args);self.assertEqual(result.returncode,0,result.stderr);return result
    def apply(self,body):
        return cli('apply','--db',self.db,'--source',write(self.tmp/'batch.json',body))
    def export(self,name='out.json'):
        out=self.tmp/name;self.ok('export','--db',self.db,'--output',out)
        return json.loads(out.read_text())
    def test_lifecycle_survives_process_restart(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.assertEqual(self.export()['generation'],0)
        # Every command runs in a fresh process, i.e. nothing is kept in memory.
        self.assertEqual(self.apply({'revisions':[{'id':'A','rev':9,'op':'delete'}]}).returncode,0)
        self.assertEqual(self.apply({'revisions':[{'id':'A','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':1}]}).returncode,0)
        report=self.export('withdrawn.json')
        self.assertEqual(next(v for v in report['result']['venues'] if v['venue']=='hall')['peak'],0)
        self.assertEqual(report['generation'],2)
        self.assertEqual(self.apply({'revisions':[{'id':'A','rev':10,'op':'upsert','venue':'annex','start':10,'end':20,'quantity':1}]}).returncode,0)
        report=self.export('restored.json')
        self.assertEqual(next(v for v in report['result']['venues'] if v['venue']=='annex')['peak'],1)
        self.assertEqual(report['generation'],3)
        # The original static model agrees with the final dataset.
        static=self.tmp/'static.json'
        self.ok('--source',write(self.tmp/'equivalent.json',{
            'horizon':BASE['horizon'],'venues':BASE['venues'],
            'bookings':[{'id':'A','venue':'annex','start':10,'end':20,'quantity':1}]}),'--output',static)
        self.assertEqual(report['result'],json.loads(static.read_text())['result'])
    def test_conflict_exits_2_and_leaves_dataset_and_report(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.assertEqual(self.apply({'revisions':[{'id':'A','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':1}]}).returncode,0)
        before=self.export('before.json')
        out=self.tmp/'report.json';out.write_text('prior-report')
        result=self.apply({'revisions':[{'id':'new','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1},
                                        {'id':'A','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':9}]})
        self.assertEqual(result.returncode,2)
        self.assertIn('conflict',result.stderr.lower())
        self.assertEqual(self.export('after.json'),before)
        empty=write(self.tmp/'empty.json',{'revisions':[]})
        result=cli('apply','--db',self.db,'--source',empty)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.export('empty.json'),before)
        self.assertEqual(out.read_text(),'prior-report')
    def test_export_failure_keeps_old_file(self):
        self.ok('init','--db',self.db,'--source',self.init)
        out=self.tmp/'report.json';out.write_text('prior')
        out_symlink=self.tmp/'link.json';out_symlink.symlink_to(out)
        self.assertEqual(cli('export','--db',self.db,'--output',out_symlink).returncode,2)
        self.assertEqual(out.read_text(),'prior')
        missing=self.tmp/'nope.db'
        self.assertEqual(cli('export','--db',missing,'--output',out).returncode,2)
        self.assertEqual(out.read_text(),'prior')
        bad=self.tmp/'bad.json';bad.write_text('{"revisions":[{"id":"x","rev":1,"op":"upsert","venue":"ghost","start":0,"end":1,"quantity":1}]}')
        self.ok('init','--db',missing,'--source',self.init)
        before=self.export('keep.json')
        self.assertEqual(cli('apply','--db',missing,'--source',bad).returncode,2)
        self.assertEqual(self.export('keep2.json'),before)
    def test_usage_errors(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.assertEqual(cli('init','--db',self.db,'--source',self.init).returncode,2)   # already initialized
        self.assertEqual(cli('apply','--db',self.db).returncode,2)
        self.assertEqual(cli('export','--db',self.db).returncode,2)
        fresh=self.tmp/'fresh.db';out=self.tmp/'x.json'
        self.assertEqual(cli('apply','--db',fresh,'--source',self.init).returncode,2)
        self.assertEqual(cli('export','--db',fresh,'--output',out).returncode,2)
        self.assertFalse(out.exists())
    def test_oversized_or_malformed_batch_is_rejected_before_writing(self):
        self.ok('init','--db',self.db,'--source',self.init)
        before=self.export('before.json')
        cases=[
            {'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1}]*20001},
            {'revisions':[{'id':'x','rev':1,'op':'move','venue':'hall'}]},
            {'revisions':[{'id':'x','rev':0,'op':'delete'}]},
            {'revisions':[{'id':'x','rev':1,'op':'delete','venue':'hall'}]},
            {'revisions':'nope'},
        ]
        for index,body in enumerate(cases):
            path=write(self.tmp/('bad%d.json'%index),body)
            self.assertEqual(cli('apply','--db',self.db,'--source',path).returncode,2,body)
        self.assertEqual(self.export('after.json'),before)
        empty=write(self.tmp/'empty.json',{'revisions':[]})
        result=cli('apply','--db',self.db,'--source',empty)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.export('empty.json'),before)
