"""Recall acceptance: quarantine of whole descendants, retroactive impact on
deliveries, idempotent roots, two recalled roots in one blend, recall order
independence, and replay-after-recall semantics."""
import sqlite3,tempfile,unittest
from fractions import Fraction
from pathlib import Path
from venue_recall import dataset

def quarantined(db):
    conn=sqlite3.connect(db)
    try:return {row[0] for row in conn.execute('SELECT lot_id FROM lots WHERE quarantined=1')}
    finally:conn.close()

def remaining(db,lot_id):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT remaining_grams FROM lots WHERE lot_id=?',(lot_id,)).fetchone()[0]
    finally:conn.close()

def lot_exists(db,lot_id):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT 1 FROM lots WHERE lot_id=?',(lot_id,)).fetchone() is not None
    finally:conn.close()

def ratio(document_row):
    value=document_row['affected_grams']
    return Fraction(int(value['numerator']),int(value['denominator']))

class Recall(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'recall.db')
        dataset.init(self.db,{'venues':['hall','annex']})
        dataset.ingest(self.db,{'operations':[
            {'op_id':'r1','type':'receive','lot_id':'flour-a','grams':1000},
            {'op_id':'r2','type':'receive','lot_id':'flour-b','grams':600},
            {'op_id':'b1','type':'blend',
             'inputs':[{'lot_id':'flour-a','grams':400},{'lot_id':'flour-b','grams':200}],
             'outputs':[{'lot_id':'dough-x','grams':600}]},
            {'op_id':'s1','type':'serve','lot_id':'dough-x','grams':150,'venue':'hall'},
            {'op_id':'b2','type':'blend',
             'inputs':[{'lot_id':'dough-x','grams':300},{'lot_id':'flour-a','grams':100}],
             'outputs':[{'lot_id':'dough-y','grams':400}]},
            {'op_id':'s2','type':'serve','lot_id':'dough-y','grams':80,'venue':'annex'}]})
    def tearDown(self):self.dir.cleanup()
    def report(self,name='report.json'):
        return dataset.report(self.db,str(self.tmp/name))

    def test_recall_quarantines_whole_descendants_and_keeps_deliveries(self):
        dataset.recall(self.db,{'roots':['flour-b']})
        # Every lot with a positive flour-b ratio is quarantined as a whole,
        # not just by its attributed mass; the clean flour-a stays open.
        self.assertEqual(quarantined(self.db),{'flour-b','dough-x','dough-y'})
        self.assertEqual(remaining(self.db,'dough-x'),150)   # untouched by the recall
        document=self.report()
        # The pre-recall deliveries survive and enter the impact report.
        self.assertEqual([row['op_id'] for row in document['deliveries']],['s1','s2'])
        self.assertEqual(ratio(document['deliveries'][0]),50)     # 150 x 1/3
        self.assertEqual(ratio(document['deliveries'][1]),20)     #  80 x 1/4
        stock={row['lot_id']:row for row in document['stock']}
        self.assertEqual(set(stock),{'flour-b','dough-x','dough-y'})
        self.assertEqual(ratio(stock['dough-x']),50)              # 150 x 1/3
        self.assertEqual(ratio(stock['dough-y']),80)              # 320 x 1/4
        self.assertEqual(ratio(stock['flour-b']),400)             # 400 x 1
        self.assertTrue(all(row['quarantined'] for row in stock.values()))
        # Neither blend nor serve may consume a quarantined lot afterwards.
        for op in ({'op_id':'x1','type':'serve','lot_id':'dough-x','grams':1,'venue':'hall'},
                   {'op_id':'x2','type':'blend','inputs':[{'lot_id':'dough-y','grams':1}],
                    'outputs':[{'lot_id':'dough-z','grams':1}]},
                   {'op_id':'x3','type':'serve','lot_id':'flour-b','grams':1,'venue':'hall'}):
            with self.assertRaisesRegex(ValueError,'quarantined'):
                dataset.ingest(self.db,{'operations':[op]})
        self.assertEqual(remaining(self.db,'dough-x'),150)

    def test_quarantined_op_rejects_its_whole_batch(self):
        dataset.recall(self.db,{'roots':['flour-b']})
        # A legal receive in the same batch does not rescue the batch.
        with self.assertRaisesRegex(ValueError,'quarantined'):
            dataset.ingest(self.db,{'operations':[
                {'op_id':'ok','type':'receive','lot_id':'flour-c','grams':10},
                {'op_id':'bad','type':'serve','lot_id':'dough-x','grams':1,'venue':'hall'}]})
        self.assertFalse(lot_exists(self.db,'flour-c'))       # rolled back as a whole
        dataset.ingest(self.db,{'operations':[
            {'op_id':'ok','type':'receive','lot_id':'flour-c','grams':10}]})
        self.assertEqual(remaining(self.db,'flour-c'),10)     # clean retry lands

    def test_recall_roots_must_be_known_raw_lots(self):
        with self.assertRaisesRegex(ValueError,'unknown lot'):
            dataset.recall(self.db,{'roots':['ghost']})
        with self.assertRaisesRegex(ValueError,'not a raw lot'):
            dataset.recall(self.db,{'roots':['dough-x']})
        self.assertEqual(self.report()['recalled_roots'],[])
        # Duplicate roots in one request and repeated requests are no-ops.
        dataset.recall(self.db,{'roots':['flour-b','flour-b']})
        first=self.report()
        dataset.recall(self.db,{'roots':['flour-b']})
        self.assertEqual(self.report('again.json'),first)
        self.assertEqual(quarantined(self.db),{'flour-b','dough-x','dough-y'})

    def test_two_recalled_roots_in_one_blend_count_true_mass_once(self):
        db=str(self.tmp/'two.db')
        dataset.init(db,{'venues':['hall']})
        dataset.ingest(db,{'operations':[
            {'op_id':'r1','type':'receive','lot_id':'a','grams':100},
            {'op_id':'r2','type':'receive','lot_id':'b','grams':100},
            {'op_id':'m','type':'blend','inputs':[{'lot_id':'a','grams':50},{'lot_id':'b','grams':50}],
             'outputs':[{'lot_id':'mix','grams':100}]},
            {'op_id':'s','type':'serve','lot_id':'mix','grams':10,'venue':'hall'}]})
        dataset.recall(db,{'roots':['a']})
        document=dataset.report(db,str(self.tmp/'one.json'))
        delivery=document['deliveries'][0]
        self.assertEqual(ratio(delivery),5)                   # 10 x 1/2, not 10
        stock={row['lot_id']:row for row in document['stock']}
        self.assertEqual(ratio(stock['mix']),45)              # 90 x 1/2
        self.assertNotIn('b',stock)                           # b is not recalled
        dataset.recall(db,{'roots':['b']})
        document=dataset.report(db,str(self.tmp/'both.json'))
        # Both roots recalled: the mix is fully affected, counted once — a
        # per-root whole-batch count would claim 2 x 90 = 180.
        stock={row['lot_id']:row for row in document['stock']}
        self.assertEqual(ratio(stock['mix']),90)
        self.assertEqual(ratio(document['deliveries'][0]),10)
        self.assertEqual(ratio(stock['a']),50)
        self.assertEqual(ratio(stock['b']),50)
        self.assertEqual(document['recalled_roots'],['a','b'])

    def test_recall_order_does_not_change_the_report(self):
        def build(tag,requests):
            db=str(self.tmp/('order-%s.db'%tag))
            dataset.init(db,{'venues':['hall']})
            dataset.ingest(db,{'operations':[
                {'op_id':'r1','type':'receive','lot_id':'a','grams':90},
                {'op_id':'r2','type':'receive','lot_id':'b','grams':60},
                {'op_id':'m','type':'blend','inputs':[{'lot_id':'a','grams':30},{'lot_id':'b','grams':60}],
                 'outputs':[{'lot_id':'mix','grams':90}]},
                {'op_id':'s','type':'serve','lot_id':'mix','grams':9,'venue':'hall'}]})
            for roots in requests:dataset.recall(db,{'roots':roots})
            return dataset.report(db,str(self.tmp/('order-%s.json'%tag)))
        forward=build('forward',[['a'],['b']])
        backward=build('backward',[['b'],['a']])
        together=build('together',[['a','b']])
        self.assertEqual(forward,backward)
        self.assertEqual(forward,together)
        # a: 30/90 = 1/3, b: 60/90 = 2/3 of the mix; the 9 g delivery is fully affected.
        self.assertEqual(ratio(forward['deliveries'][0]),9)
        stock={row['lot_id']:row for row in forward['stock']}
        self.assertEqual(ratio(stock['mix']),81)

    def test_replayed_op_after_recall_is_ignored_not_rejected(self):
        dataset.recall(self.db,{'roots':['flour-b']})
        before=self.report()
        # The same serve op, already accepted before the recall, is a replay:
        # ignored with success even though its lot is now quarantined.
        result=dataset.ingest(self.db,{'operations':[
            {'op_id':'s1','type':'serve','lot_id':'dough-x','grams':150,'venue':'hall'}]})
        self.assertEqual(result,{'applied':0,'replayed':1})
        self.assertEqual(remaining(self.db,'dough-x'),150)
        self.assertEqual(self.report('after.json'),before)
        # A *different* payload with the same op_id is still a conflict.
        with self.assertRaisesRegex(ValueError,'conflicting replay'):
            dataset.ingest(self.db,{'operations':[
                {'op_id':'s1','type':'serve','lot_id':'dough-x','grams':151,'venue':'hall'}]})

    def test_delivery_of_exhausted_lot_stays_in_the_report(self):
        db=str(self.tmp/'drained.db')
        dataset.init(db,{'venues':['hall']})
        dataset.ingest(db,{'operations':[
            {'op_id':'r','type':'receive','lot_id':'a','grams':40},
            {'op_id':'s','type':'serve','lot_id':'a','grams':40,'venue':'hall'}]})
        dataset.recall(db,{'roots':['a']})
        document=dataset.report(db,str(self.tmp/'drained.json'))
        # The lot is gone from stock (nothing remains) but the delivery is
        # preserved and fully attributed.
        self.assertEqual(document['stock'],[])
        self.assertEqual(document['deliveries'],
                         [{'op_id':'s','venue':'hall','lot_id':'a','grams':40,
                           'affected_grams':{'numerator':'40','denominator':'1'}}])

    def test_affected_grams_are_reduced_fractions(self):
        db=str(self.tmp/'frac.db')
        dataset.init(db,{'venues':['hall']})
        dataset.ingest(db,{'operations':[
            {'op_id':'r1','type':'receive','lot_id':'a','grams':90},
            {'op_id':'r2','type':'receive','lot_id':'b','grams':60},
            {'op_id':'m','type':'blend','inputs':[{'lot_id':'a','grams':30},{'lot_id':'b','grams':60}],
             'outputs':[{'lot_id':'mix','grams':90}]},
            {'op_id':'s','type':'serve','lot_id':'mix','grams':7,'venue':'hall'}]})
        dataset.recall(db,{'roots':['b']})
        document=dataset.report(db,str(self.tmp/'frac.json'))
        # 7 g at ratio 2/3: 14/3 stays a fraction; 83 g remain: 166/3.
        self.assertEqual(document['deliveries'][0]['affected_grams'],
                         {'numerator':'14','denominator':'3'})
        stock={row['lot_id']:row for row in document['stock']}
        self.assertEqual(stock['mix']['affected_grams'],{'numerator':'166','denominator':'3'})
        self.assertNotIn('a',stock)                            # a is not recalled
        # b's 60 g were all blended away, so b itself is exhausted and absent.
        self.assertEqual(set(stock),{'mix'})

if __name__=='__main__':unittest.main()
