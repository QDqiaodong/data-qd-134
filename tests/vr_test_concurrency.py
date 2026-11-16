"""Concurrency and recovery: a recall racing a serve is equivalent to one of
the two serial orders, reports always see a complete snapshot, and a failure
just before commit rolls everything back so a retry lands cleanly."""
import json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from venue_recall import dataset
ROOT=Path(__file__).resolve().parents[1]

def snapshot(db):
    conn=sqlite3.connect(db)
    try:
        tables={}
        for table in ('meta','venues','ops','lots','consumption','deliveries','composition','recalls'):
            order='1' if table in ('meta','venues','recalls') else '1,2'
            tables[table]=conn.execute('SELECT * FROM %s ORDER BY %s'%(table,order)).fetchall()
        return tables
    finally:conn.close()

SETUP=[{'op_id':'r1','type':'receive','lot_id':'raw','grams':100},
       {'op_id':'b1','type':'blend','inputs':[{'lot_id':'raw','grams':60}],
        'outputs':[{'lot_id':'mix','grams':60}]}]
SERVE={'op_id':'s1','type':'serve','lot_id':'mix','grams':10,'venue':'hall'}

def build(db):
    dataset.init(db,{'venues':['hall']})
    dataset.ingest(db,{'operations':SETUP})

class Race(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
    def tearDown(self):self.dir.cleanup()

    def serial_outcome(self,name,serve_first):
        """One of the only two legal final states of the race, computed serially."""
        db=str(self.tmp/(name+'.db'))
        build(db)
        if serve_first:
            dataset.ingest(db,{'operations':[SERVE]});dataset.recall(db,{'roots':['raw']})
        else:
            dataset.recall(db,{'roots':['raw']})
            with self.assertRaisesRegex(ValueError,'quarantined'):
                dataset.ingest(db,{'operations':[SERVE]})
        return json.dumps(snapshot(db),sort_keys=True,default=str)

    def test_recall_and_serve_race_is_serial(self):
        legal={self.serial_outcome('serve-first',True),self.serial_outcome('recall-first',False)}
        for round_i in range(20):
            db=str(self.tmp/('race-%d.db'%round_i));build(db)
            barrier=threading.Barrier(2);errors={}
            def do_recall():
                barrier.wait()
                try:dataset.recall(db,{'roots':['raw']})
                except Exception as error:errors['recall']=error
            def do_serve():
                barrier.wait()
                try:dataset.ingest(db,{'operations':[SERVE]})
                except ValueError:pass                       # quarantined: legal
                except Exception as error:errors['serve']=error
            threads=[threading.Thread(target=do_recall),threading.Thread(target=do_serve)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            self.assertEqual(errors,{})
            state=json.dumps(snapshot(db),sort_keys=True,default=str)
            self.assertIn(state,legal,'round %d observed a non-serial interleaving'%round_i)

    def test_recall_and_serve_race_between_processes(self):
        serve_first=self.serial_outcome('proc-serve-first',True)
        recall_first=self.serial_outcome('proc-recall-first',False)
        env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
        for round_i in range(4):
            tmp=self.tmp/('proc-%d'%round_i);tmp.mkdir()
            db=str(tmp/'race.db');build(db)
            serve_src=tmp/'serve.json';serve_src.write_text(json.dumps({'operations':[SERVE]}))
            recall_src=tmp/'recall.json';recall_src.write_text(json.dumps({'roots':['raw']}))
            processes=[
                subprocess.Popen([sys.executable,'-m','venue_recall.cli','recall',
                                  '--db',db,'--source',str(recall_src)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env),
                subprocess.Popen([sys.executable,'-m','venue_recall.cli','ingest',
                                  '--db',db,'--source',str(serve_src)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)]
            codes=[]
            for process in processes:
                process.communicate()
                codes.append(process.returncode)
            # The recall always succeeds; the serve lands iff it committed first.
            self.assertEqual(codes[0],0)
            self.assertIn(codes[1],(0,2))
            state=json.dumps(snapshot(db),sort_keys=True,default=str)
            if codes[1]==0:
                self.assertEqual(state,serve_first)
            else:
                self.assertEqual(state,recall_first)

class Snapshots(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'recall.db')
        dataset.init(self.db,{'venues':['hall']})
    def tearDown(self):self.dir.cleanup()

    def test_reports_during_writes_are_complete_snapshots(self):
        dataset.ingest(self.db,{'operations':[
            {'op_id':'r%d'%i,'type':'receive','lot_id':'raw-%d'%i,'grams':100} for i in range(4)]})
        known={}
        out=self.tmp/'known.json'
        document=dataset.report(self.db,str(out))
        known[json.dumps(document,sort_keys=True)]=True
        writes=[]
        for i in range(4):
            writes.append({'operations':[
                {'op_id':'b%d'%i,'type':'blend','inputs':[{'lot_id':'raw-%d'%i,'grams':40}],
                 'outputs':[{'lot_id':'mix-%d'%i,'grams':40}]},
                {'op_id':'s%d'%i,'type':'serve','lot_id':'mix-%d'%i,'grams':5,'venue':'hall'}]})
            writes.append({'roots':['raw-%d'%i]})
        observed=[];errors=[];stop=threading.Event();counter=[0]
        def observer():
            while not stop.is_set():
                counter[0]+=1
                try:document=dataset.report(self.db,str(self.tmp/('obs-%d.json'%counter[0])))
                except Exception as error:errors.append(repr(error));continue
                observed.append(json.dumps(document,sort_keys=True))
        thread=threading.Thread(target=observer);thread.start()
        for write in writes:
            if 'operations' in write:dataset.ingest(self.db,write)
            else:dataset.recall(self.db,write)
            document=dataset.report(self.db,str(out))
            known[json.dumps(document,sort_keys=True)]=True
        stop.set();thread.join()
        self.assertEqual(errors,[])
        self.assertGreater(len(observed),5)
        for document in observed:
            self.assertIn(document,known)     # only complete serial states, never a mix

class Recovery(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'recall.db')
        dataset.init(self.db,{'venues':['hall']})
    def tearDown(self):self.dir.cleanup()

    def test_failed_ingest_rolls_back_and_retry_matches_clean(self):
        dataset.ingest(self.db,{'operations':SETUP})
        batch={'operations':[
            {'op_id':'r2','type':'receive','lot_id':'raw-2','grams':50},
            {'op_id':'b2','type':'blend','inputs':[{'lot_id':'mix','grams':20},{'lot_id':'raw-2','grams':10}],
             'outputs':[{'lot_id':'mix-2','grams':30}]},
            {'op_id':'s2','type':'serve','lot_id':'mix-2','grams':7,'venue':'hall'}]}
        before=snapshot(self.db)
        with self.assertRaisesRegex(ValueError,'injected failure'):
            dataset.ingest(self.db,batch,fail_before_commit=True)
        self.assertEqual(snapshot(self.db),before)      # nothing leaked
        # The retry lands and equals a fault-free database, byte for byte.
        dataset.ingest(self.db,batch)
        clean=str(self.tmp/'clean.db')
        dataset.init(clean,{'venues':['hall']})
        dataset.ingest(clean,{'operations':SETUP})
        dataset.ingest(clean,batch)
        self.assertEqual(snapshot(self.db),snapshot(clean))
        # A second retry of the same batch is a pure replay: no stock moves.
        result=dataset.ingest(self.db,batch)
        self.assertEqual(result,{'applied':0,'replayed':3})
        self.assertEqual(snapshot(self.db),snapshot(clean))

    def test_failed_recall_rolls_back_and_retry_matches_clean(self):
        dataset.ingest(self.db,{'operations':SETUP})
        before=snapshot(self.db)
        with self.assertRaisesRegex(ValueError,'injected failure'):
            dataset.recall(self.db,{'roots':['raw']},fail_before_commit=True)
        self.assertEqual(snapshot(self.db),before)      # no root, no quarantine
        dataset.recall(self.db,{'roots':['raw']})
        clean=str(self.tmp/'clean-recall.db')
        dataset.init(clean,{'venues':['hall']})
        dataset.ingest(clean,{'operations':SETUP})
        dataset.recall(clean,{'roots':['raw']})
        self.assertEqual(snapshot(self.db),snapshot(clean))
        document=dataset.report(self.db,str(self.tmp/'report.json'))
        stock={row['lot_id']:row for row in document['stock']}
        self.assertEqual(stock['mix']['affected_grams'],{'numerator':'60','denominator':'1'})
        self.assertEqual(stock['raw']['affected_grams'],{'numerator':'40','denominator':'1'})

if __name__=='__main__':unittest.main()
