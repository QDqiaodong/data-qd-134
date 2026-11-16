"""End-to-end CLI tests for venue-recall: lifecycle, exit codes, atomic
publication, and isolation of the legacy venue-occupancy / venue-energy
commands."""
import json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def cli(*args):
    return subprocess.run([sys.executable,'-m','venue_recall.cli',*map(str,args)],
                          capture_output=True,text=True,
                          env={**os.environ,'PYTHONPATH':str(ROOT/'src')})

def write(path,body):
    Path(path).write_text(body if isinstance(body,str) else json.dumps(body))
    return path

def snapshot(db):
    conn=sqlite3.connect(db)
    try:
        tables={}
        for table in ('meta','venues','ops','lots','consumption','deliveries','composition','recalls'):
            order='1' if table in ('meta','venues','recalls') else '1,2'
            tables[table]=conn.execute('SELECT * FROM %s ORDER BY %s'%(table,order)).fetchall()
        return tables
    finally:conn.close()

INIT={'venues':['hall','annex']}
OPS={'operations':[
    {'op_id':'r1','type':'receive','lot_id':'flour-a','grams':1000},
    {'op_id':'r2','type':'receive','lot_id':'flour-b','grams':600},
    {'op_id':'b1','type':'blend','inputs':[{'lot_id':'flour-a','grams':400},{'lot_id':'flour-b','grams':200}],
     'outputs':[{'lot_id':'dough-x','grams':600}]},
    {'op_id':'s1','type':'serve','lot_id':'dough-x','grams':150,'venue':'hall'},
    {'op_id':'b2','type':'blend','inputs':[{'lot_id':'dough-x','grams':300},{'lot_id':'flour-a','grams':100}],
     'outputs':[{'lot_id':'dough-y','grams':400}]},
    {'op_id':'s2','type':'serve','lot_id':'dough-y','grams':80,'venue':'annex'}]}
RECALL={'roots':['flour-b']}
EXPECTED={
    'recalled_roots':['flour-b'],
    'deliveries':[
        {'op_id':'s1','venue':'hall','lot_id':'dough-x','grams':150,
         'affected_grams':{'numerator':'50','denominator':'1'}},
        {'op_id':'s2','venue':'annex','lot_id':'dough-y','grams':80,
         'affected_grams':{'numerator':'20','denominator':'1'}}],
    'stock':[
        {'lot_id':'dough-x','remaining_grams':150,
         'affected_grams':{'numerator':'50','denominator':'1'},'quarantined':True},
        {'lot_id':'dough-y','remaining_grams':320,
         'affected_grams':{'numerator':'80','denominator':'1'},'quarantined':True},
        {'lot_id':'flour-b','remaining_grams':400,
         'affected_grams':{'numerator':'400','denominator':'1'},'quarantined':True}]}

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=self.tmp/'recall.db'
        self.init=write(self.tmp/'init.json',INIT)
        self.ops=write(self.tmp/'ops.json',OPS)
        self.recall=write(self.tmp/'recall.json',RECALL)
    def tearDown(self):self.dir.cleanup()
    def ok(self,*args):
        result=cli(*args);self.assertEqual(result.returncode,0,result.stderr);return result
    def ingest(self,body,name='ops.json'):
        return cli('ingest','--db',self.db,'--source',write(self.tmp/name,body))
    def report(self,name='report.json'):
        out=self.tmp/name
        result=cli('report','--db',self.db,'--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(out.read_text()),result

    def test_full_lifecycle_matches_documented_fixture(self):
        # Every command runs in a fresh process: nothing is kept in memory.
        self.ok('init','--db',self.db,'--source',self.init)
        result=self.ok('ingest','--db',self.db,'--source',self.ops)
        self.assertEqual(json.loads(result.stdout),{'applied':6,'command':'ingest','replayed':0})
        result=self.ok('recall','--db',self.db,'--source',self.recall)
        self.assertEqual(json.loads(result.stdout),{'command':'recall','recalled_roots':1})
        document,result=self.report()
        self.assertEqual(document,EXPECTED)
        self.assertEqual(json.loads(result.stdout),document)   # stdout mirrors the file
        # flour-b mass conservation: stock 50+80+400 plus delivered 50+20 == 600 received.
        def grams(rows):return sum(int(r['affected_grams']['numerator']) for r in rows)
        self.assertEqual(grams(document['stock'])+grams(document['deliveries']),600)

    def test_init_refuses_nonempty_file_and_rerun(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.assertEqual(cli('init','--db',self.db,'--source',self.init).returncode,2)
        other=self.tmp/'other.db';other.write_text('not a database')
        self.assertEqual(cli('init','--db',other,'--source',self.init).returncode,2)
        self.assertEqual(other.read_text(),'not a database')
        empty=self.tmp/'empty.db';empty.touch()                 # an empty file may be adopted
        self.ok('init','--db',empty,'--source',self.init)

    def test_malformed_inputs_exit_2_and_change_nothing(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.ok('ingest','--db',self.db,'--source',self.ops)
        before=snapshot(self.db)
        cases=[
            ('ingest','{"operations":[{"op_id":"x","type":"receive","lot_id":"l","grams":1}],"operations":[]}'),
            ('init',{'venues':[]}),                                           # 1..100 venues
            ('init',{'venues':['hall']*2}),                                   # duplicate venue
            ('init',{'venues':['hall'],'extra':1}),                           # unknown field
            ('init',{'venues':['  ']}),                                       # blank identifier
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l','grams':True}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l','grams':0}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l','grams':10**12+1}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l','grams':1.5}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l'}]}),     # missing field
            ('ingest',{'operations':[{'op_id':'x','type':'receive','lot_id':'l','grams':1,'v':2}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'melt','lot_id':'l','grams':1}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'blend','inputs':[],
                                      'outputs':[{'lot_id':'o','grams':1}]}]}),
            ('ingest',{'operations':[{'op_id':'x','type':'blend',
                                      'inputs':[{'lot_id':'flour-a','grams':1}]*101,
                                      'outputs':[{'lot_id':'o','grams':101}]}]}),        # >100 items
            ('ingest',{'operations':[{'op_id':'x','type':'blend',
                                      'inputs':[{'lot_id':'flour-a','grams':1},{'lot_id':'flour-a','grams':1}],
                                      'outputs':[{'lot_id':'o','grams':2}]}]}),          # duplicate input lot
            ('ingest',{'operations':[{'op_id':'x','type':'serve','lot_id':'flour-a','grams':1,'venue':'ghost'}]}),
            ('ingest',{'operations':[{'op_id':'r1','type':'receive','lot_id':'other','grams':5}]}),  # replay conflict
            ('ingest',{'operations':'nope'}),
            ('recall',{'roots':['flour-a','flour-a'],'extra':1}),
            ('recall',{'roots':['never-seen']}),                              # unknown recall root
            ('recall',{'roots':['dough-x']}),                                 # recall root must be raw
        ]
        for index,(command,body) in enumerate(cases):
            path=write(self.tmp/('bad%d.json'%index),body)
            result=cli(command,'--db',self.db,'--source',path)
            self.assertEqual(result.returncode,2,(body,result.stdout))
        self.assertEqual(snapshot(self.db),before)
        body='{"operations":['+','.join(
            '{"op_id":"o%06d","type":"receive","lot_id":"lot%06d","grams":1}'%(i,i)
            for i in range(20000))+']}'
        oversized=self.tmp/'oversized.json'
        oversized.write_text(body+' '*(8_000_001-len(body.encode())))
        self.assertGreater(oversized.stat().st_size,8_000_000)
        self.assertEqual(cli('ingest','--db',self.db,'--source',oversized).returncode,2)
        self.assertEqual(snapshot(self.db),before)
        too_many=write(self.tmp/'too_many.json',
                       {'operations':[{'op_id':'o%d'%i,'type':'receive','lot_id':'l%d'%i,'grams':1}
                                      for i in range(20001)]})
        self.assertEqual(cli('ingest','--db',self.db,'--source',too_many).returncode,2)
        self.assertEqual(snapshot(self.db),before)

    def test_uninitialized_database(self):
        missing=self.tmp/'nope.db'
        self.assertEqual(cli('ingest','--db',missing,'--source',self.ops).returncode,2)
        self.assertEqual(cli('recall','--db',missing,'--source',self.recall).returncode,2)
        out=self.tmp/'report.json';out.write_text('prior')
        self.assertEqual(cli('report','--db',missing,'--output',out).returncode,2)
        self.assertEqual(out.read_text(),'prior')

    def test_report_publication_is_atomic_and_read_only(self):
        self.ok('init','--db',self.db,'--source',self.init)
        self.ok('ingest','--db',self.db,'--source',self.ops)
        self.ok('recall','--db',self.db,'--source',self.recall)
        out=self.tmp/'report.json'
        document,_=self.report()
        first=out.read_bytes()
        before=snapshot(self.db)
        document2,_=self.report()                                # repeated report
        self.assertEqual(out.read_bytes(),first)                 # identical bytes
        self.assertEqual(document2,document)
        self.assertEqual(snapshot(self.db),before)               # stock untouched
        target=self.tmp/'target.json';target.write_text('PRIOR')
        link=self.tmp/'link.json';link.symlink_to(target)
        self.assertEqual(cli('report','--db',self.db,'--output',link).returncode,2)
        self.assertEqual(target.read_text(),'PRIOR')             # old file kept
        self.assertEqual(cli('report','--db',self.db,'--output',self.db).returncode,2)
        self.assertEqual(snapshot(self.db),before)

    def test_usage_errors(self):
        self.assertEqual(cli().returncode,2)
        self.assertEqual(cli('init','--db',self.db).returncode,2)
        self.assertEqual(cli('ingest','--source',self.ops).returncode,2)
        self.assertEqual(cli('recall','--db',self.db).returncode,2)
        self.assertEqual(cli('report','--db',self.db).returncode,2)
        self.assertEqual(cli('export','--db',self.db).returncode,2)

class LegacyIsolation(unittest.TestCase):
    def test_legacy_commands_and_reports_are_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
            out=Path(td)/'legacy.json'
            result=subprocess.run(
                [sys.executable,'-m','venue_occupancy.cli',
                 '--source',ROOT/'fixtures'/'input.json','--output',out],
                capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            legacy=json.loads(out.read_text())
            self.assertEqual(set(legacy),{'schema_version','source_sha256','result'})
            energy=Path(td)/'energy.json'
            base=ROOT/'fixtures'/'energy'
            result=subprocess.run(
                [sys.executable,'-m','venue_energy.cli','audit',
                 '--meters',base/'meters.json','--readings',base/'readings.csv',
                 '--tariffs',base/'tariffs.json','--output',energy],
                capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(energy.read_text())['reading_rows'],9)

if __name__=='__main__':unittest.main()
