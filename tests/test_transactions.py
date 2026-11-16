"""Single-transaction visibility, snapshot export and failure recovery."""
import json,os,sqlite3,tempfile,threading,unittest
from venue_occupancy import dataset,store

BASE={'horizon':{'start':0,'end':1000},
      'venues':[{'id':'v%d'%i,'capacity':3} for i in range(6)],
      'bookings':[{'id':'b%d'%i,'venue':'v%d'%(i%6),'start':i*4,'end':i*4+7,'quantity':1} for i in range(40)]}

def snapshot(db):
    conn=sqlite3.connect(db)
    try:
        tables={}
        for table in ('meta','facts','effective','segments','venue_levels','venue_metrics'):
            if table=='meta':rows=conn.execute('SELECT key,value FROM meta ORDER BY key').fetchall()
            elif table=='facts':rows=conn.execute('SELECT * FROM facts ORDER BY id,rev').fetchall()
            elif table=='effective':rows=conn.execute('SELECT * FROM effective ORDER BY id').fetchall()
            elif table=='segments':rows=conn.execute('SELECT * FROM segments ORDER BY venue,start').fetchall()
            elif table=='venue_levels':rows=conn.execute('SELECT * FROM venue_levels ORDER BY venue,occupancy').fetchall()
            else:rows=conn.execute('SELECT * FROM venue_metrics ORDER BY venue').fetchall()
            tables[table]=rows
        return tables
    finally:conn.close()

class Transactions(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.db=self.dir.name+'/d.db';dataset.init(self.db,BASE)
    def tearDown(self):self.dir.cleanup()
    def test_failure_after_materialize_rolls_everything_back(self):
        before=snapshot(self.db)
        batch={'revisions':[{'id':'b1','rev':2,'op':'delete'},
                            {'id':'b2','rev':4,'op':'upsert','venue':'v5','start':20,'end':90,'quantity':3},
                            {'id':'new','rev':1,'op':'upsert','venue':'v0','start':0,'end':5,'quantity':2}]}
        with self.assertRaises(ValueError):dataset.apply(self.db,batch,fail_after_materialize=True)
        self.assertEqual(snapshot(self.db),before)
        self.assertEqual(int(store.connect(self.db).execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]),0)
        # Retrying the same batch once now yields exactly the no-fault result.
        dataset.apply(self.db,batch)
        retried=snapshot(self.db)
        clean=self.dir.name+'/clean.db';dataset.init(clean,BASE);dataset.apply(clean,batch)
        self.assertEqual(retried,snapshot(clean))
        for name in ('retry','clean'):
            out=self.dir.name+'/'+name+'.json'
            dataset.export(self.db if name=='retry' else clean,out)
            with open(out) as stream:globals().setdefault(name,json.load(stream))
        self.assertEqual(globals()['retry'],globals()['clean'])
        self.assertEqual(globals()['retry']['generation'],1)
    def test_export_snapshot_survives_concurrent_commits(self):
        observed=[];stop=threading.Event();errors=[]
        def observer():
            while not stop.is_set():
                out=self.dir.name+'/obs-%d.json'%threading.get_ident()
                try:dataset.export(self.db,out)
                except Exception as error:errors.append(repr(error));continue
                with open(out) as stream:observed.append(json.load(stream))
                os.unlink(out)
        thread=threading.Thread(target=observer);thread.start()
        known={}
        out=self.dir.name+'/gen.json';dataset.export(self.db,out)
        with open(out) as stream:known[0]=json.dumps(json.load(stream),sort_keys=True)
        for generation in range(1,16):
            dataset.apply(self.db,{'revisions':[{'id':'b%d'%(generation*3%40),'rev':generation,'op':'upsert',
                                                 'venue':'v%d'%(generation%6),'start':generation*50,
                                                 'end':generation*50+30,'quantity':generation%3+1}]})
            out=self.dir.name+'/gen.json';dataset.export(self.db,out)
            with open(out) as stream:known[generation]=json.dumps(json.load(stream),sort_keys=True)
        stop.set();thread.join()
        self.assertEqual(errors,[])
        self.assertGreater(len(observed),5)
        valid=set(known.values())
        for report in observed:
            self.assertIn(json.dumps(report,sort_keys=True),valid)
    def test_facts_and_derivatives_commit_together(self):
        conn=store.connect(self.db)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO facts(id,rev,op,venue,start,end,quantity) VALUES('ghost',1,1,'v0',0,5,1)")
            conn.execute('ROLLBACK')
        finally:conn.close()
        conn=sqlite3.connect(self.db)
        self.assertIsNone(conn.execute("SELECT 1 FROM facts WHERE id='ghost'").fetchone())
