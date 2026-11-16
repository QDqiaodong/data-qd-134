"""End-to-end CLI tests for venue-clearing: real subprocesses, the documented
fixtures, exit codes, atomic publication, legacy isolation and the strict
document contract."""
import json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'fixtures'/'clearing'

def cli(*args):
    return subprocess.run([sys.executable,'-m','venue_clearing.cli',*map(str,args)],
                          capture_output=True,text=True,
                          env={**os.environ,'PYTHONPATH':str(ROOT/'src')})

def write(path,body):
    Path(path).write_text(body if isinstance(body,str) else json.dumps(body))
    return path

def snapshot(db):
    conn=sqlite3.connect(db)
    try:
        return {name:conn.execute('SELECT * FROM %s ORDER BY 1'%name).fetchall()
                for name in ('meta','payments','credits','payment_alias','credit_reference',
                             'edges','components','comp_vertex','inference','bindings','decisions',
                             'imports')}
    finally:conn.close()

EXPECTED_BEFORE_PIN={
    'generation':2,'maximum_pairs':2,
    'payments':[
        {'id':'P-100','status':'forced','possible_peers':['C-1'],'can_be_unmatched':False},
        {'id':'P-200','status':'forced','possible_peers':['C-2'],'can_be_unmatched':False},
        {'id':'P-300','status':'unmatched','possible_peers':[],'can_be_unmatched':True}],
    'credits':[
        {'id':'C-1','status':'forced','possible_peers':['P-100'],'can_be_unmatched':False},
        {'id':'C-2','status':'forced','possible_peers':['P-200'],'can_be_unmatched':False},
        {'id':'C-3','status':'unmatched','possible_peers':[],'can_be_unmatched':True},
        {'id':'C-4','status':'unmatched','possible_peers':[],'can_be_unmatched':True},
        {'id':'C-5','status':'unmatched','possible_peers':[],'can_be_unmatched':True},
        {'id':'C-9','status':'unmatched','possible_peers':[],'can_be_unmatched':True}]}

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=self.tmp/'clearing.db'
    def tearDown(self):self.dir.cleanup()
    def ok(self,*args):
        result=cli(*args);self.assertEqual(result.returncode,0,result.stderr);return result
    def report(self,name='report.json'):
        out=self.tmp/name
        result=cli('report','--db',self.db,'--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(out.read_text()),out

    def test_full_lifecycle_matches_documented_fixture(self):
        self.ok('init','--db',self.db)
        result=self.ok('import','--db',self.db,'--source',FIXTURES/'payments.json')
        self.assertEqual(json.loads(result.stdout)['payments'],3)
        result=self.ok('import','--db',self.db,'--source',FIXTURES/'credits.json')
        self.assertEqual(json.loads(result.stdout)['edges'],3)
        document,_=self.report()
        self.assertEqual(document,EXPECTED_BEFORE_PIN)
        self.ok('decide','--db',self.db,'--source',FIXTURES/'pin.json')
        document,_=self.report('after-pin.json')
        by_id={row['id']:row for row in document['payments']+document['credits']}
        self.assertEqual(by_id['P-100']['status'],'pinned')
        self.assertEqual(by_id['C-1']['status'],'pinned')
        self.assertEqual(document['maximum_pairs'],2)
        self.assertEqual(document['generation'],3)
        self.ok('decide','--db',self.db,'--source',FIXTURES/'unpin.json')
        document,_=self.report('after-unpin.json')
        self.assertEqual(document['generation'],4)
        self.assertEqual(document['maximum_pairs'],2)
        self.assertEqual({k:v for k,v in document.items() if k!='generation'},
                         {k:v for k,v in EXPECTED_BEFORE_PIN.items() if k!='generation'})

    def test_init_refuses_nonempty_file_and_may_adopt_empty_one(self):
        self.ok('init','--db',self.db)
        self.assertEqual(cli('init','--db',self.db).returncode,2)
        other=self.tmp/'other.db';other.write_text('not a database')
        self.assertEqual(cli('init','--db',other).returncode,2)
        self.assertEqual(other.read_text(),'not a database')
        empty=self.tmp/'empty.db';empty.touch()
        self.ok('init','--db',empty)

    def test_uninitialized_database(self):
        missing=self.tmp/'nope.db'
        self.assertEqual(cli('import','--db',missing,
                             '--source',FIXTURES/'payments.json').returncode,2)
        self.assertEqual(cli('decide','--db',missing,
                             '--source',FIXTURES/'pin.json').returncode,2)
        out=self.tmp/'report.json';out.write_text('prior')
        self.assertEqual(cli('report','--db',missing,'--output',out).returncode,2)
        self.assertEqual(out.read_text(),'prior')

    def test_report_is_atomic_read_only_and_stdio_mirror(self):
        self.ok('init','--db',self.db)
        self.ok('import','--db',self.db,'--source',FIXTURES/'payments.json')
        self.ok('import','--db',self.db,'--source',FIXTURES/'credits.json')
        first,out=self.report()
        bytes_first=out.read_bytes()
        before=snapshot(self.db)
        second,_=self.report()
        self.assertEqual(out.read_bytes(),bytes_first)
        self.assertEqual(second,first)
        self.assertEqual(snapshot(self.db),before)
        target=self.tmp/'target.json';target.write_text('PRIOR')
        link=self.tmp/'link.json';link.symlink_to(target)
        self.assertEqual(cli('report','--db',self.db,'--output',link).returncode,2)
        self.assertEqual(target.read_text(),'PRIOR')
        self.assertEqual(cli('report','--db',self.db,'--output',self.db).returncode,2)
        self.assertEqual(snapshot(self.db),before)

    def test_malformed_documents_exit_2_and_change_nothing(self):
        self.ok('init','--db',self.db)
        self.ok('import','--db',self.db,'--source',FIXTURES/'payments.json')
        before=snapshot(self.db)
        good_payment={'payment_id':'X','venue':'hall','currency':'USD',
                      'amount_minor':1,'aliases':['T']}
        good_credit={'credit_id':'Y','venue':'hall','currency':'USD',
                     'amount_minor':1,'references':['T']}
        def imp(**patch):
            record=dict(good_payment,**patch)
            return {'payments':[record],'credits':[]}
        cases=[
            ('{"payments":[],"credits":[],"payments":[]}',None),
            ('{"payments":[]}',None),
            ('{"payments":[],"credits":[],"extra":1}',None),
            (imp(currency='usd'),None),
            (imp(currency='US'),None),
            (imp(currency='U1D'),None),
            (imp(currency=123),None),
            (imp(amount_minor=True),None),
            (imp(amount_minor=0),None),
            (imp(amount_minor=10**12+1),None),
            (imp(amount_minor=1.0),None),
            (imp(amount_minor='1'),None),
            (imp(aliases=[]),None),
            (imp(aliases=['T']*2),None),
            (imp(aliases=['T']*101),None),
            (imp(aliases=[' ']),None),
            (imp(aliases='T'),None),
            (imp(venue=''),None),
            (imp(payment_id='X'*129),None),
            ({'payments':[dict(good_payment,alt=1)],'credits':[]},None),
            ({'payments':[good_payment],'credits':[dict(good_credit,reference=['T'])]},None),
            ({'payments':[dict(good_payment,aliases=['T','T'])],'credits':[]},None),
            ({'payments':[dict(good_payment,payment_id='X%d'%i) for i in range(1001)],
              'credits':[dict(good_credit,credit_id='Y%d'%i) for i in range(1001)]},None),
            ({'payments':[good_payment],'credits':[]}, 'unknown-key-see-below'),
        ]
        for index,(body,flag) in enumerate(cases):
            if flag=='unknown-key-see-below':
                text='{"payments":[{"payment_id":"X","venue":"hall","currency":"USD",'\
                     '"amount_minor":1,"aliases":["T"],"zz":1}],"credits":[]}'
            else:
                text=body if isinstance(body,str) else json.dumps(body)
            path=write(self.tmp/('bad%d.json'%index),text)
            result=cli('import','--db',self.db,'--source',path)
            self.assertEqual(result.returncode,2,(index,text,result.stdout,result.stderr))
            self.assertEqual(snapshot(self.db),before)
        # 8 MB source limit.
        body=json.dumps({'payments':[good_payment],'credits':[]})
        oversized=self.tmp/'oversized.json'
        oversized.write_text(body+' '*(8_000_001-len(body.encode())))
        self.assertEqual(cli('import','--db',self.db,'--source',oversized).returncode,2)
        # Non-finite numbers are rejected before parsing.
        path=write(self.tmp/'nan.json','{"payments":[],"credits":[],"x":NaN}')
        self.assertEqual(cli('import','--db',self.db,'--source',path).returncode,2)
        self.assertEqual(snapshot(self.db),before)

    def test_usage_errors(self):
        self.assertEqual(cli().returncode,2)
        self.assertEqual(cli('init').returncode,2)
        self.assertEqual(cli('import','--db',self.db).returncode,2)
        self.assertEqual(cli('decide','--source',self.tmp/'x.json').returncode,2)
        self.assertEqual(cli('report','--db',self.db).returncode,2)
        self.assertEqual(cli('export','--db',self.db).returncode,2)

    def test_decision_document_validation(self):
        self.ok('init','--db',self.db)
        self.ok('import','--db',self.db,'--source',FIXTURES/'payments.json')
        self.ok('import','--db',self.db,'--source',FIXTURES/'credits.json')
        before=snapshot(self.db)
        cases=[
            {'decisions':[]},                                     # empty batch is legal
            {'decisions':[{'decision_id':'d','op':'pin','payment_id':'P-100'}]},
            {'decisions':[{'decision_id':'d','op':'unpin','payment_id':'P-100','credit_id':'C-1'}]},
            {'decisions':[{'decision_id':'d','op':'pay','payment_id':'P-100'}]},
            {'decisions':[{'decision_id':'d','op':'pin','payment_id':'P-100','credit_id':'C-3'}]},
            {'decisions':[{'decision_id':'d','op':'unpin','payment_id':'P-404'}]},
            {'decisions':[{'decision_id':'d','op':'unpin','payment_id':'P-100'}]*2001},
        ]
        for index,body in enumerate(cases):
            path=write(self.tmp/('d%d.json'%index),body)
            result=cli('decide','--db',self.db,'--source',path)
            if index==0:
                self.assertEqual(result.returncode,0)
            else:
                self.assertEqual(result.returncode,2,(body,result.stdout))
        self.assertEqual(snapshot(self.db),before)

    def test_empty_import_and_empty_database_report(self):
        self.ok('init','--db',self.db)
        path=write(self.tmp/'empty.json',{'payments':[],'credits':[]})
        result=self.ok('import','--db',self.db,'--source',path)
        self.assertEqual(json.loads(result.stdout)['generation'],0)
        document,_=self.report()
        self.assertEqual(document,{'generation':0,'maximum_pairs':0,'payments':[],'credits':[]})

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
            recall_db=Path(td)/'recall.db'
            base=ROOT/'fixtures'/'recall'
            for args in (('init','--db',recall_db,'--source',base/'init.json'),
                         ('ingest','--db',recall_db,'--source',base/'operations.json'),
                         ('recall','--db',recall_db,'--source',base/'recall.json')):
                result=subprocess.run([sys.executable,'-m','venue_recall.cli',*map(str,args)],
                                      capture_output=True,text=True,env=env)
                self.assertEqual(result.returncode,0,result.stderr)
            report=Path(td)/'recall.json'
            result=subprocess.run([sys.executable,'-m','venue_recall.cli','report',
                                   '--db',str(recall_db),'--output',str(report)],
                                  capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(json.loads(report.read_text())['stock']),3)

    def test_clearing_does_not_touch_a_legacy_database(self):
        with tempfile.TemporaryDirectory() as td:
            recall_db=Path(td)/'recall.db'
            env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
            base=ROOT/'fixtures'/'recall'
            subprocess.run([sys.executable,'-m','venue_recall.cli','init','--db',str(recall_db),
                            '--source',str(base/'init.json')],check=True,capture_output=True,env=env)
            before=sqlite3.connect(recall_db).execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            clearing_db=Path(td)/'clearing.db'
            result=cli('init','--db',clearing_db)
            self.assertEqual(result.returncode,0,result.stderr)
            after=sqlite3.connect(recall_db).execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
