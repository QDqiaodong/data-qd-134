"""Policy tests for revision facts: parsing and the highest-revision-wins rule."""
import tempfile,unittest
from venue_occupancy import dataset
from venue_occupancy.revisions import parse_batch

BASE={'horizon':{'start':0,'end':100},
      'venues':[{'id':'hall','capacity':3},{'id':'annex','capacity':1}],
      'bookings':[{'id':'a','venue':'hall','start':0,'end':10,'quantity':2}]}

class Parsing(unittest.TestCase):
    def bad(self,batch):
        with self.assertRaises(ValueError):parse_batch(batch)
    def test_field_sets_are_strict(self):
        self.bad({'revisions':[{'id':'x','rev':1,'op':'delete','venue':'hall'}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':0,'end':1}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1,'extra':0}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'cancel'}]})
        self.bad({'revisions':[{'id':'x','rev':1}]})
        self.bad({'revisions':[],'extra':1})
    def test_bounds(self):
        self.bad({'revisions':[{'id':'x','rev':0,'op':'delete'}]})
        self.bad({'revisions':[{'id':'x','rev':10**12+1,'op':'delete'}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':5,'end':5,'quantity':1}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':0,'end':5,'quantity':0}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'upsert','venue':'hall','start':False,'end':5,'quantity':1}]})
        self.bad({'revisions':[{'id':'x','rev':True,'op':'delete'}]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'delete'}]*(20001)})
    def test_in_batch_duplicate_must_agree(self):
        record={'id':'x','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1}
        self.assertEqual(len(parse_batch({'revisions':[record,record]})),1)
        self.bad({'revisions':[record,dict(record,quantity=2)]})
        self.bad({'revisions':[{'id':'x','rev':1,'op':'delete'},{'id':'x','rev':1,'op':'delete','venue':'hall'}]})

class Precedence(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.db=self.dir.name+'/d.db'
        dataset.init(self.db,BASE)
    def tearDown(self):self.dir.cleanup()
    def apply(self,*records):
        return dataset.apply(self.db,{'revisions':list(records)})
    def snapshot(self):
        import json
        out=self.dir.name+'/o.json';dataset.export(self.db,out)
        with open(out) as stream:return json.load(stream)
    def venue(self,report,name):
        return next(v for v in report['result']['venues'] if v['venue']==name)
    def test_withdrawal_then_lower_upsert_stays_withdrawn(self):
        self.apply({'id':'a','rev':9,'op':'delete'})
        self.apply({'id':'a','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':1})
        self.assertEqual(self.venue(self.snapshot(),'hall')['peak'],0)
        self.apply({'id':'a','rev':10,'op':'upsert','venue':'annex','start':50,'end':60,'quantity':1})
        report=self.snapshot()
        self.assertEqual(self.venue(report,'hall')['peak'],0)
        self.assertEqual(self.venue(report,'annex')['peak'],1)
    def test_stale_conflicting_replay_rejects_writes_too(self):
        self.apply({'id':'a','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':1})
        before=self.snapshot();gen=before['generation']
        with self.assertRaises(ValueError):
            self.apply({'id':'fresh','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1},
                       {'id':'a','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':2})
        state=self.snapshot()
        self.assertEqual(state['generation'],gen)
        self.assertEqual(state['result'],before['result'])
    def test_stale_replay_equal_payload_is_genuine_replay(self):
        record={'id':'a','rev':7,'op':'upsert','venue':'hall','start':1,'end':5,'quantity':1}
        self.apply(record)
        generation=self.snapshot()['generation']
        self.apply(record)
        self.assertEqual(self.snapshot()['generation'],generation)
    def test_delete_unknown_id_is_legal(self):
        self.apply({'id':'never','rev':3,'op':'delete'})
        self.assertEqual(self.snapshot()['generation'],1)
    def test_delete_of_new_id_then_upsert_uses_highest(self):
        self.apply({'id':'n','rev':4,'op':'delete'},{'id':'n','rev':3,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1})
        self.assertEqual(self.venue(self.snapshot(),'hall')['peak'],2)
        self.apply({'id':'n','rev':5,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1})
        self.assertEqual(self.snapshot()['generation'],2)
    def test_generation_only_moves_on_new_facts(self):
        self.apply({'id':'z','rev':2,'op':'delete'})
        self.apply({'id':'z','rev':2,'op':'delete'})
        self.apply()
        self.assertEqual(self.snapshot()['generation'],1)
        # A batch whose only new rows are all superseded by already-seen higher
        # revisions is still a new fact, so generation advances (but nothing is
        # repainted, which test_materialize covers).
        self.apply({'id':'z','rev':1,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1})
        self.assertEqual(self.snapshot()['generation'],2)
    def test_order_within_batch_irrelevant(self):
        other=self.dir.name+'/b.db';dataset.init(other,BASE)
        records=[{'id':'x','rev':3,'op':'upsert','venue':'hall','start':0,'end':5,'quantity':1},
                 {'id':'x','rev':1,'op':'upsert','venue':'hall','start':5,'end':10,'quantity':2},
                 {'id':'x','rev':2,'op':'delete'}]
        dataset.apply(self.db,{'revisions':records})
        dataset.apply(other,{'revisions':list(reversed(records))})
        import json
        a=self.dir.name+'/a.json';b=self.dir.name+'/b.json'
        dataset.export(self.db,a);dataset.export(other,b)
        self.assertEqual(json.load(open(a))['result'],json.load(open(b))['result'])
    def test_facts_retain_superseded_versions(self):
        import sqlite3
        self.apply({'id':'a','rev':4,'op':'upsert','venue':'hall','start':0,'end':1,'quantity':1})
        self.apply({'id':'a','rev':6,'op':'delete'})
        rows=sqlite3.connect(self.db).execute("SELECT rev,op FROM facts WHERE id='a' ORDER BY rev").fetchall()
        self.assertEqual(rows,[(0,1),(4,1),(6,0)])
        self.assertIsNone(sqlite3.connect(self.db).execute("SELECT 1 FROM effective WHERE id='a'").fetchone())
