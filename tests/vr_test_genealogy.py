"""Genealogy acceptance: an independent small-scale rational oracle recomputes
every output composition from the operation log and checks it against the
database and the report — covering same-source fork-merge, multi-layer blends
whose denominators exceed 64 bits, and a 30-layer diamond that would take
exponential time if ancestor paths were enumerated."""
import random,sqlite3,tempfile,time,unittest
from fractions import Fraction
from pathlib import Path
from venue_recall import dataset

class Oracle:
    """A deliberately independent re-implementation: plain dicts of Fraction,
    no imports from venue_recall."""
    def __init__(self):
        self.comp={}        # lot -> {root: Fraction}
        self.stock={}       # lot -> remaining grams
        self.deliveries=[]  # (op_id, lot, venue, grams)
    def receive(self,lot,grams):
        self.comp[lot]={lot:Fraction(1)};self.stock[lot]=grams
    def blend(self,inputs,outputs):
        total=sum(g for _l,g in inputs)
        mix={}
        for lot,g in inputs:
            self.stock[lot]-=g
            for root,ratio in self.comp[lot].items():
                mix[root]=mix.get(root,Fraction(0))+ratio*g/total
        for lot,g in outputs:
            self.comp[lot]=dict(mix);self.stock[lot]=g
    def serve(self,op_id,lot,venue,grams):
        self.stock[lot]-=grams
        self.deliveries.append((op_id,lot,venue,grams))
    def expected_report(self,recalled):
        recalled=set(recalled)
        deliveries=[]
        for op_id,lot,venue,grams in self.deliveries:
            ratio=sum((self.comp[lot].get(r,Fraction(0)) for r in recalled),Fraction(0))
            if ratio>0:deliveries.append((op_id,venue,lot,grams,grams*ratio))
        stock=[]
        for lot,remaining in self.stock.items():
            ratio=sum((self.comp[lot].get(r,Fraction(0)) for r in recalled),Fraction(0))
            if remaining>0 and ratio>0:stock.append((lot,remaining,remaining*ratio))
        deliveries.sort();stock.sort()
        return deliveries,stock

def read_composition(db):
    conn=sqlite3.connect(db)
    try:
        result={}
        for lot,root,num,den in conn.execute('SELECT lot_id,root_id,num,den FROM composition'):
            result.setdefault(lot,{})[root]=Fraction(int(num),int(den))
        return result
    finally:conn.close()

def read_stock(db):
    conn=sqlite3.connect(db)
    try:
        return {lot:remaining for lot,remaining in
                conn.execute('SELECT lot_id,remaining_grams FROM lots')}
    finally:conn.close()

def recalled_roots(db):
    conn=sqlite3.connect(db)
    try:return [row[0] for row in conn.execute('SELECT root_id FROM recalls ORDER BY root_id')]
    finally:conn.close()

def composition_rows(db):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT COUNT(*) FROM composition').fetchone()[0]
    finally:conn.close()

def check_report(test,document,oracle,recalled):
    """The report must equal the oracle's expectation row by row, with exact
    reduced fractions — not merely the same number of rows."""
    test.assertEqual(document['recalled_roots'],sorted(recalled))
    deliveries,stock=oracle.expected_report(recalled)
    actual=[(row['op_id'],row['venue'],row['lot_id'],row['grams'],
             Fraction(int(row['affected_grams']['numerator']),int(row['affected_grams']['denominator'])))
            for row in document['deliveries']]
    test.assertEqual(actual,deliveries)
    actual=[(row['lot_id'],row['remaining_grams'],
             Fraction(int(row['affected_grams']['numerator']),int(row['affected_grams']['denominator'])))
            for row in document['stock']]
    test.assertEqual(actual,stock)
    test.assertTrue(all(row['quarantined'] is True for row in document['stock']))
    # Mass conservation: every recalled gram is either in stock or delivered.
    return sum((row[4] for row in deliveries),Fraction(0))+sum((row[2] for row in stock),Fraction(0))

class Genealogy(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'recall.db')
        dataset.init(self.db,{'venues':['hall','annex']})
        self.oracle=Oracle()
    def tearDown(self):self.dir.cleanup()
    def ingest(self,operations):
        return dataset.ingest(self.db,{'operations':operations})
    def report(self,oracle,recalled,name='report.json'):
        dataset.recall(self.db,{'roots':recalled})
        document=dataset.report(self.db,str(self.tmp/name))
        return document,check_report(self,document,oracle,recalled_roots(self.db))

    def test_fork_merge_accumulates_true_mass_once(self):
        # One source forks into two lots and rejoins: the merged lot is 100%
        # the source, counted once — not once per path.
        self.ingest([{'op_id':'r','type':'receive','lot_id':'R','grams':1000},
                     {'op_id':'b1','type':'blend','inputs':[{'lot_id':'R','grams':500}],
                      'outputs':[{'lot_id':'X','grams':500}]},
                     {'op_id':'b2','type':'blend','inputs':[{'lot_id':'R','grams':300}],
                      'outputs':[{'lot_id':'Y','grams':300}]},
                     {'op_id':'b3','type':'blend','inputs':[{'lot_id':'X','grams':200},{'lot_id':'Y','grams':150}],
                      'outputs':[{'lot_id':'Z','grams':350}]},
                     {'op_id':'s1','type':'serve','lot_id':'Z','grams':70,'venue':'hall'}])
        self.oracle.receive('R',1000)
        self.oracle.blend([('R',500)],[('X',500)])
        self.oracle.blend([('R',300)],[('Y',300)])
        self.oracle.blend([('X',200),('Y',150)],[('Z',350)])
        self.oracle.serve('s1','Z','hall',70)
        self.assertEqual(read_composition(self.db),self.oracle.comp)
        self.assertEqual(read_stock(self.db),self.oracle.stock)
        document,affected=self.report(self.oracle,['R'])
        delivery=next(row for row in document['deliveries'] if row['op_id']=='s1')
        # 70 g delivered, all of it R — a path-counting implementation would say 140.
        self.assertEqual(delivery['affected_grams'],{'numerator':'70','denominator':'1'})
        merged=next(row for row in document['stock'] if row['lot_id']=='Z')
        self.assertEqual(merged['affected_grams'],{'numerator':'280','denominator':'1'})
        self.assertEqual(affected,1000)     # every received gram accounted for exactly once

    def test_denominators_beyond_64_bits(self):
        # Three layers with distinct large prime totals: the composition
        # denominator is their ~10^27 product, far beyond int64.
        p1,p2,p3=999999937,999999929,999999893
        self.ingest([{'op_id':'r1','type':'receive','lot_id':'R1','grams':10**12},
                     {'op_id':'r2','type':'receive','lot_id':'R2','grams':10**12},
                     {'op_id':'m1','type':'blend','inputs':[{'lot_id':'R1','grams':1},{'lot_id':'R2','grams':p1-1}],
                      'outputs':[{'lot_id':'M1','grams':p1}]},
                     {'op_id':'m2','type':'blend','inputs':[{'lot_id':'M1','grams':1},{'lot_id':'R2','grams':p2-1}],
                      'outputs':[{'lot_id':'M2','grams':p2}]},
                     {'op_id':'m3','type':'blend','inputs':[{'lot_id':'M2','grams':1},{'lot_id':'R2','grams':p3-1}],
                      'outputs':[{'lot_id':'M3','grams':p3}]},
                     {'op_id':'s','type':'serve','lot_id':'M3','grams':1,'venue':'annex'}])
        self.oracle.receive('R1',10**12);self.oracle.receive('R2',10**12)
        self.oracle.blend([('R1',1),('R2',p1-1)],[('M1',p1)])
        self.oracle.blend([('M1',1),('R2',p2-1)],[('M2',p2)])
        self.oracle.blend([('M2',1),('R2',p3-1)],[('M3',p3)])
        self.oracle.serve('s','M3','annex',1)
        product=p1*p2*p3
        self.assertGreater(product,2**64)
        self.assertEqual(read_composition(self.db),self.oracle.comp)
        self.assertEqual(self.oracle.comp['M3'],{'R1':Fraction(1,product),
                                                 'R2':Fraction(product-1,product)})
        document,_affected=self.report(self.oracle,['R1'])
        delivery=next(row for row in document['deliveries'] if row['op_id']=='s')
        self.assertEqual(delivery['affected_grams'],{'numerator':'1','denominator':str(product)})
        stock=next(row for row in document['stock'] if row['lot_id']=='M3')
        reduced=Fraction(p3-1,product)      # (p3-1) g remain, each 1/product R1
        self.assertEqual(stock['affected_grams'],
                         {'numerator':str(reduced.numerator),'denominator':str(reduced.denominator)})

    def test_thirty_layer_diamond_never_enumerates_paths(self):
        # Each layer forks the mixture into two lots and rejoins them: the
        # number of ancestor paths doubles per layer (>= 2**30 at the bottom),
        # so any path enumeration would not finish; the DP materialization
        # stores a constant number of composition rows per lot and finishes
        # instantly.
        ops=[{'op_id':'r0','type':'receive','lot_id':'R0','grams':10**9},
             {'op_id':'r1','type':'receive','lot_id':'R1','grams':10**9},
             {'op_id':'m0','type':'blend','inputs':[{'lot_id':'R0','grams':10},{'lot_id':'R1','grams':10}],
              'outputs':[{'lot_id':'L0','grams':20}]}]
        self.oracle.receive('R0',10**9);self.oracle.receive('R1',10**9)
        self.oracle.blend([('R0',10),('R1',10)],[('L0',20)])
        for i in range(30):
            ops.append({'op_id':'a%d'%i,'type':'blend',
                        'inputs':[{'lot_id':'L%d'%i,'grams':10},{'lot_id':'R0','grams':30}],
                        'outputs':[{'lot_id':'A%d'%i,'grams':40}]})
            ops.append({'op_id':'b%d'%i,'type':'blend',
                        'inputs':[{'lot_id':'L%d'%i,'grams':10},{'lot_id':'R1','grams':10}],
                        'outputs':[{'lot_id':'B%d'%i,'grams':20}]})
            ops.append({'op_id':'l%d'%(i+1),'type':'blend',
                        'inputs':[{'lot_id':'A%d'%i,'grams':20},{'lot_id':'B%d'%i,'grams':10}],
                        'outputs':[{'lot_id':'L%d'%(i+1),'grams':30}]})
            self.oracle.blend([('L%d'%i,10),('R0',30)],[('A%d'%i,40)])
            self.oracle.blend([('L%d'%i,10),('R1',10)],[('B%d'%i,20)])
            self.oracle.blend([('A%d'%i,20),('B%d'%i,10)],[('L%d'%(i+1),30)])
        ops.append({'op_id':'s','type':'serve','lot_id':'L30','grams':7,'venue':'hall'})
        started=time.monotonic()
        self.ingest(ops)
        elapsed=time.monotonic()-started
        self.oracle.serve('s','L30','hall',7)
        self.assertLess(elapsed,10.0)       # actual: milliseconds
        self.assertLessEqual(composition_rows(self.db),500)   # no per-path storage
        self.assertEqual(read_composition(self.db),self.oracle.comp)
        document,_affected=self.report(self.oracle,['R0','R1'])
        delivery=next(row for row in document['deliveries'] if row['op_id']=='s')
        expected=7*(self.oracle.comp['L30']['R0']+self.oracle.comp['L30']['R1'])
        self.assertEqual(Fraction(int(delivery['affected_grams']['numerator']),
                                  int(delivery['affected_grams']['denominator'])),expected)
        # The composition is a non-trivial rational, not 0 or 1.
        self.assertNotIn(self.oracle.comp['L30']['R0'],(Fraction(0),Fraction(1)))

    def test_randomized_against_oracle(self):
        rng=random.Random(20261116)
        stock={};live=[]
        batches=[];ops=[]
        for i in range(240):
            live=[lot for lot in live if stock[lot]>0]
            roll=rng.random()
            if roll<0.30 or not live:
                lot='raw-%03d'%i
                grams=rng.choice((1,7,50,997,10**6))
                op={'op_id':'recv-%03d'%i,'type':'receive','lot_id':lot,'grams':grams}
                live.append(lot);stock[lot]=grams
            elif roll<0.68:
                inputs=[]
                for lot in rng.sample(live,min(len(live),rng.randint(1,3))):
                    take=rng.randint(1,stock[lot])
                    inputs.append({'lot_id':lot,'grams':take});stock[lot]-=take
                total=sum(item['grams'] for item in inputs)
                cuts=sorted(rng.randint(0,total) for _ in range(rng.randint(0,1)))
                pieces=[a-b for a,b in zip(cuts+[total],[0]+cuts)]
                outputs=[]
                for j,piece in enumerate(pieces):
                    if piece==0:continue
                    lot='mix-%03d-%d'%(i,j)
                    outputs.append({'lot_id':lot,'grams':piece})
                    live.append(lot);stock[lot]=piece
                op={'op_id':'blend-%03d'%i,'type':'blend','inputs':inputs,'outputs':outputs}
            else:
                lot=rng.choice(live)
                grams=rng.randint(1,stock[lot]);stock[lot]-=grams
                op={'op_id':'serve-%03d'%i,'type':'serve','lot_id':lot,'grams':grams,
                    'venue':rng.choice(('hall','annex'))}
            ops.append(op)
            if rng.random()<0.25:batches.append(ops);ops=[]
        if ops:batches.append(ops)
        # Apply batches; every batch is also replayed in full and must be a
        # no-op.  Then an independent oracle recomputes everything from the log.
        for batch in batches:
            self.assertEqual(self.ingest(batch)['applied'],len(batch))
            self.assertEqual(self.ingest(batch),{'applied':0,'replayed':len(batch)})
        oracle=Oracle();received={}
        for batch in batches:
            for op in batch:
                if op['type']=='receive':
                    oracle.receive(op['lot_id'],op['grams']);received[op['lot_id']]=op['grams']
                elif op['type']=='blend':
                    oracle.blend([(i['lot_id'],i['grams']) for i in op['inputs']],
                                 [(o['lot_id'],o['grams']) for o in op['outputs']])
                else:
                    oracle.serve(op['op_id'],op['lot_id'],op['venue'],op['grams'])
        self.assertEqual(read_composition(self.db),oracle.comp)
        self.assertEqual(read_stock(self.db),oracle.stock)
        # Every composition is a convex combination: ratios sum to exactly 1.
        for lot,mix in oracle.comp.items():
            self.assertEqual(sum(mix.values(),Fraction(0)),1,lot)
        # Recall a few roots; the report matches the oracle row by row, and a
        # repeated recall is a no-op with an identical report.
        roots=rng.sample(sorted(received),3)
        document,affected=self.report(oracle,roots)
        self.assertEqual(document['recalled_roots'],sorted(roots))
        document2,affected=self.report(oracle,roots,'again.json')
        self.assertEqual(document2,document)
        self.assertEqual(affected,sum((received[r] for r in roots),Fraction(0)))

if __name__=='__main__':unittest.main()
