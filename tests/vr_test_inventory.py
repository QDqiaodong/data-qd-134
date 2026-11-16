"""Inventory acceptance: partial draws, multi-output blends, lot identity,
overdraw and mid-batch rollback, checked item by item against the tables."""
import sqlite3,tempfile,unittest
from fractions import Fraction
from pathlib import Path
from venue_recall import dataset

def lots(db):
    conn=sqlite3.connect(db)
    try:
        return {row[0]:(row[1],row[2],row[3],row[4]) for row in
                conn.execute('SELECT lot_id,kind,initial_grams,remaining_grams,quarantined FROM lots')}
    finally:conn.close()

def composition(db,lot_id):
    conn=sqlite3.connect(db)
    try:
        return {root:Fraction(int(num),int(den)) for root,num,den in
                conn.execute('SELECT root_id,num,den FROM composition WHERE lot_id=?',(lot_id,))}
    finally:conn.close()

def table(db,name):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT * FROM %s ORDER BY 1,2'%name).fetchall()
    finally:conn.close()

class Inventory(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.db=str(Path(self.dir.name)/'recall.db')
        dataset.init(self.db,{'venues':['hall','annex']})
    def tearDown(self):self.dir.cleanup()
    def ingest(self,operations,**kw):
        return dataset.ingest(self.db,{'operations':operations},**kw)

    def test_partial_draw_tracks_remaining_grams(self):
        self.ingest([{'op_id':'r','type':'receive','lot_id':'a','grams':100}])
        self.ingest([{'op_id':'s','type':'serve','lot_id':'a','grams':30,'venue':'hall'}])
        self.assertEqual(lots(self.db)['a'],('raw',100,70,0))
        self.ingest([{'op_id':'b','type':'blend','inputs':[{'lot_id':'a','grams':20}],
                      'outputs':[{'lot_id':'m','grams':20}]}])
        self.assertEqual(lots(self.db)['a'],('raw',100,50,0))
        self.assertEqual(lots(self.db)['m'],('blend',20,20,0))
        # The consumption ledger links every deduction to the op it fed.
        self.assertEqual(table(self.db,'consumption'),
                         [('b','a',20),('s','a',30)])

    def test_multi_output_blend_shares_one_mixture(self):
        self.ingest([{'op_id':'r1','type':'receive','lot_id':'a','grams':100},
                     {'op_id':'r2','type':'receive','lot_id':'b','grams':50},
                     {'op_id':'mix','type':'blend',
                      'inputs':[{'lot_id':'a','grams':40},{'lot_id':'b','grams':10}],
                      'outputs':[{'lot_id':'x','grams':30},{'lot_id':'y','grams':20}]}])
        self.assertEqual(lots(self.db)['a'],('raw',100,60,0))
        self.assertEqual(lots(self.db)['b'],('raw',50,40,0))
        self.assertEqual(lots(self.db)['x'],('blend',30,30,0))
        self.assertEqual(lots(self.db)['y'],('blend',20,20,0))
        for lot in ('x','y'):
            self.assertEqual(composition(self.db,lot),{'a':Fraction(4,5),'b':Fraction(1,5)})

    def test_exhausted_lot_cannot_be_recreated(self):
        self.ingest([{'op_id':'r','type':'receive','lot_id':'a','grams':50},
                     {'op_id':'s','type':'serve','lot_id':'a','grams':50,'venue':'hall'},
                     {'op_id':'r3','type':'receive','lot_id':'c','grams':20}])
        self.assertEqual(lots(self.db)['a'][2],0)
        with self.assertRaisesRegex(ValueError,'lot already exists'):
            self.ingest([{'op_id':'r2','type':'receive','lot_id':'a','grams':5}])
        # An exhausted lot is also reserved as a blend output id.
        with self.assertRaisesRegex(ValueError,'lot already exists'):
            self.ingest([{'op_id':'b','type':'blend','inputs':[{'lot_id':'c','grams':5}],
                          'outputs':[{'lot_id':'a','grams':5}]}])
        # A blend output id is equally permanent, even after full consumption.
        self.ingest([{'op_id':'b2','type':'blend','inputs':[{'lot_id':'c','grams':7}],
                      'outputs':[{'lot_id':'m','grams':7}]},
                     {'op_id':'s2','type':'serve','lot_id':'m','grams':7,'venue':'hall'}])
        with self.assertRaisesRegex(ValueError,'lot already exists'):
            self.ingest([{'op_id':'r4','type':'receive','lot_id':'m','grams':1}])
        self.assertEqual(lots(self.db)['m'],('blend',7,0,0))
        self.assertEqual(lots(self.db)['c'],('raw',20,13,0))

    def test_overdraw_rejects_the_whole_batch(self):
        self.ingest([{'op_id':'r','type':'receive','lot_id':'a','grams':50}])
        with self.assertRaisesRegex(ValueError,'insufficient stock'):
            self.ingest([{'op_id':'s','type':'serve','lot_id':'a','grams':51,'venue':'hall'}])
        with self.assertRaisesRegex(ValueError,'insufficient stock'):
            self.ingest([{'op_id':'b','type':'blend','inputs':[{'lot_id':'a','grams':51}],
                          'outputs':[{'lot_id':'m','grams':51}]}])
        self.assertEqual(lots(self.db)['a'],('raw',50,50,0))
        self.assertEqual(table(self.db,'consumption'),[])

    def test_mid_batch_failure_rolls_back_item_by_item(self):
        batch=[{'op_id':'r1','type':'receive','lot_id':'a','grams':100},
               {'op_id':'s1','type':'serve','lot_id':'a','grams':30,'venue':'hall'},
               {'op_id':'b1','type':'blend','inputs':[{'lot_id':'a','grams':20}],
                'outputs':[{'lot_id':'m','grams':20}]},
               {'op_id':'s2','type':'serve','lot_id':'a','grams':999,'venue':'hall'}]
        with self.assertRaisesRegex(ValueError,'insufficient stock'):
            self.ingest(batch)
        # Every table is byte-identical to the empty dataset: no lot, no op,
        # no consumption, no delivery, no composition row survived.
        self.assertEqual(lots(self.db),{})
        for name in ('ops','consumption','deliveries','composition'):
            self.assertEqual(table(self.db,name),[],name)
        # The valid prefix applies cleanly when resubmitted without the bad op.
        result=self.ingest(batch[:3])
        self.assertEqual(result,{'applied':3,'replayed':0})
        self.assertEqual(lots(self.db)['a'],('raw',100,50,0))
        self.assertEqual(lots(self.db)['m'],('blend',20,20,0))

    def test_batch_is_interpreted_in_array_order(self):
        # A lot received, blended and served inside one batch, in order.
        result=self.ingest([
            {'op_id':'r','type':'receive','lot_id':'a','grams':100},
            {'op_id':'b','type':'blend','inputs':[{'lot_id':'a','grams':60}],
             'outputs':[{'lot_id':'m','grams':60}]},
            {'op_id':'s','type':'serve','lot_id':'m','grams':25,'venue':'annex'}])
        self.assertEqual(result,{'applied':3,'replayed':0})
        self.assertEqual(lots(self.db)['a'],('raw',100,40,0))
        self.assertEqual(lots(self.db)['m'],('blend',60,35,0))

    def test_mass_mismatch_and_unknown_lots_reject(self):
        self.ingest([{'op_id':'r','type':'receive','lot_id':'a','grams':100}])
        with self.assertRaisesRegex(ValueError,'grams differ'):
            self.ingest([{'op_id':'b','type':'blend','inputs':[{'lot_id':'a','grams':10}],
                          'outputs':[{'lot_id':'m','grams':11}]}])
        with self.assertRaisesRegex(ValueError,'unknown lot'):
            self.ingest([{'op_id':'b','type':'blend','inputs':[{'lot_id':'ghost','grams':1}],
                          'outputs':[{'lot_id':'m','grams':1}]}])
        with self.assertRaisesRegex(ValueError,'unknown lot'):
            self.ingest([{'op_id':'s','type':'serve','lot_id':'ghost','grams':1,'venue':'hall'}])
        self.assertEqual(lots(self.db)['a'],('raw',100,100,0))

    def test_replay_never_deducts_twice_and_conflicts_reject(self):
        batch=[{'op_id':'r','type':'receive','lot_id':'a','grams':100},
               {'op_id':'s','type':'serve','lot_id':'a','grams':30,'venue':'hall'}]
        self.assertEqual(self.ingest(batch),{'applied':2,'replayed':0})
        self.assertEqual(self.ingest(batch),{'applied':0,'replayed':2})   # full retry
        self.assertEqual(lots(self.db)['a'],('raw',100,70,0))
        # A batch mixing one replayed op with one new op applies only the new one.
        result=self.ingest([batch[0],
                            {'op_id':'s2','type':'serve','lot_id':'a','grams':5,'venue':'annex'}])
        self.assertEqual(result,{'applied':1,'replayed':1})
        self.assertEqual(lots(self.db)['a'],('raw',100,65,0))
        # Identical op_id with a different payload is a conflict and rejects
        # the whole batch, including a valid op earlier in the same batch.
        with self.assertRaisesRegex(ValueError,'conflicting replay'):
            self.ingest([{'op_id':'r9','type':'receive','lot_id':'z','grams':1},
                         {'op_id':'s','type':'serve','lot_id':'a','grams':31,'venue':'hall'}])
        self.assertNotIn('z',lots(self.db))
        self.assertEqual(lots(self.db)['a'],('raw',100,65,0))
        # Within one batch: identical duplicate op_ids are ignored, different
        # payloads conflict.
        result=self.ingest([{'op_id':'d','type':'receive','lot_id':'dup','grams':3},
                            {'op_id':'d','type':'receive','lot_id':'dup','grams':3}])
        self.assertEqual(result,{'applied':1,'replayed':1})
        with self.assertRaisesRegex(ValueError,'conflicting replay'):
            self.ingest([{'op_id':'e','type':'receive','lot_id':'x','grams':1},
                         {'op_id':'e','type':'receive','lot_id':'y','grams':1}])
        self.assertNotIn('x',lots(self.db))

    def test_grams_bounds(self):
        self.ingest([{'op_id':'lo','type':'receive','lot_id':'lo','grams':1},
                     {'op_id':'hi','type':'receive','lot_id':'hi','grams':10**12}])
        for bad in (0,-1,10**12+1,True,False,1.5,'5',None):
            with self.assertRaises(ValueError,msg=repr(bad)):
                self.ingest([{'op_id':'x','type':'receive','lot_id':'x','grams':bad}])
        self.assertEqual(lots(self.db)['hi'],('raw',10**12,10**12,0))

    def test_accounting_identity_remaining_equals_initial_minus_consumption(self):
        self.ingest([{'op_id':'r1','type':'receive','lot_id':'a','grams':100},
                     {'op_id':'r2','type':'receive','lot_id':'b','grams':50},
                     {'op_id':'mix','type':'blend',
                      'inputs':[{'lot_id':'a','grams':40},{'lot_id':'b','grams':10}],
                      'outputs':[{'lot_id':'x','grams':30},{'lot_id':'y','grams':20}]},
                     {'op_id':'s1','type':'serve','lot_id':'x','grams':7,'venue':'hall'},
                     {'op_id':'b2','type':'blend','inputs':[{'lot_id':'y','grams':5}],
                      'outputs':[{'lot_id':'z','grams':5}]}])
        conn=sqlite3.connect(self.db)
        try:
            rows=conn.execute(
                'SELECT l.lot_id,l.initial_grams,l.remaining_grams,'
                ' COALESCE((SELECT SUM(c.grams) FROM consumption c WHERE c.lot_id=l.lot_id),0)'
                ' FROM lots l').fetchall()
        finally:conn.close()
        self.assertTrue(rows)
        for lot_id,initial,remaining,consumed in rows:
            self.assertEqual(remaining,initial-consumed,lot_id)
        # Every consumption row names the op whose outputs it fed.
        self.assertEqual(table(self.db,'consumption'),
                         [('b2','y',5),('mix','a',40),('mix','b',10),('s1','x',7)])

    def test_blend_item_boundaries(self):
        # 100 inputs and 100 outputs are accepted; each list keeps its own
        # uniqueness domain, so a lot may not repeat within one list.
        operations=[{'op_id':'r%03d'%i,'type':'receive','lot_id':'src%03d'%i,'grams':2}
                    for i in range(100)]
        operations.append({'op_id':'big','type':'blend',
                           'inputs':[{'lot_id':'src%03d'%i,'grams':1} for i in range(100)],
                           'outputs':[{'lot_id':'out%03d'%i,'grams':1} for i in range(100)]})
        result=self.ingest(operations)
        self.assertEqual(result,{'applied':101,'replayed':0})
        self.assertEqual(lots(self.db)['out099'],('blend',1,1,0))
        self.assertEqual(composition(self.db,'out000'),
                         {('src%03d'%i):Fraction(1,100) for i in range(100)})

if __name__=='__main__':unittest.main()
