"""Materialized-layer acceptance: segments and metrics equal the static model.

Every assertion compares the exported dataset against `model.analyze` run over
the same set of effective bookings, i.e. the untouched v1 semantics.
"""
import json,random,tempfile,unittest
from pathlib import Path
from venue_occupancy import dataset
from venue_occupancy.model import analyze

FIELDS=('venue','capacity','peak','over_capacity_ticks','excess_unit_ticks','booked_unit_ticks','segments')

class Harness(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.db=self.dir.name+'/d.db';self.effective={};self.use_facts=True
    def tearDown(self):self.dir.cleanup()
    def init(self,horizon,venues,bookings):
        data={'horizon':{'start':horizon[0],'end':horizon[1]},
              'venues':[{'id':k,'capacity':c} for k,c in venues.items()],
              'bookings':[{'id':i,'venue':v[0],'start':v[1],'end':v[2],'quantity':v[3]} for i,v in bookings.items()]}
        dataset.init(self.db,data)
        self.horizon=horizon;self.venues=venues;self.effective=dict(bookings)
    def apply(self,records):
        dataset.apply(self.db,{'revisions':records})

    def effective_from_facts(self):
        """Independent re-derivation of the effective set straight from `facts`.

        This is deliberately not the same code path as apply: it scans every
        fact and picks the maximum revision per id in Python.
        """
        import sqlite3
        rows=sqlite3.connect(self.db).execute('SELECT id,rev,op,venue,start,end,quantity FROM facts').fetchall()
        best={}
        for bid,rev,op,venue,a,z,q in rows:
            if bid not in best or rev>best[bid][0]:best[bid]=(rev,op,venue,a,z,q)
        return {bid:(venue,a,z,q) for bid,(rev,op,venue,a,z,q) in best.items() if op==1}
    def export(self):
        out=self.dir.name+'/o.json';dataset.export(self.db,out)
        with open(out) as stream:return json.load(stream)
    def expected(self):
        effective=self.effective_from_facts() if self.use_facts else self.effective
        data={'horizon':{'start':self.horizon[0],'end':self.horizon[1]},
              'venues':[{'id':k,'capacity':c} for k,c in self.venues.items()],
              'bookings':[{'id':i,'venue':v[0],'start':v[1],'end':v[2],'quantity':v[3]} for i,v in effective.items()]}
        return analyze(data)
    def assertEquivalent(self):
        got=self.export()['result'];want=self.expected()
        self.assertEqual(got,want)
        for a,b in zip(got['venues'],want['venues']):
            for field in FIELDS:self.assertEqual(a[field],b[field],field)
        # Report-level invariants independent of the model implementation.
        for venue in got['venues']:
            segments=venue['segments']
            self.assertEqual(segments[0]['start'],self.horizon[0])
            self.assertEqual(segments[-1]['end'],self.horizon[1])
            for left,right in zip(segments,segments[1:]):
                self.assertEqual(left['end'],right['start'])
                self.assertNotEqual(left['occupancy'],right['occupancy'])
                self.assertLess(left['start'],left['end'])
            for segment in segments:self.assertEqual(segment['excess'],max(0,segment['occupancy']-venue['capacity']))
        return got

class Scenarios(Harness):
    def test_nested_and_touching(self):
        self.init((0,100),{'A':5},{'a':('A',0,60,2),'b':('A',10,50,3),'c':('A',20,30,1),'d':('A',30,40,1),'e':('A',60,70,4)})
        self.assertEquivalent()
        self.apply([{'id':'c','rev':1,'op':'upsert','venue':'A','start':25,'end':35,'quantity':2},
                    {'id':'d','rev':1,'op':'delete'}])
        self.assertEquivalent()
    def test_zero_capacity(self):
        self.init((0,50),{'A':0,'B':2},{'a':('A',0,10,1),'b':('B',0,10,1)})
        self.assertEquivalent()
        result=self.export()['result']
        zero=next(v for v in result['venues'] if v['venue']=='A')
        self.assertEqual(zero['over_capacity_ticks'],10)
        self.assertEqual(zero['excess_unit_ticks'],10)
    def test_clipped_to_empty_and_back(self):
        self.init((10,20),{'A':3},{'a':('A',0,10,2)})
        self.assertEquivalent()
        self.apply([{'id':'a','rev':1,'op':'upsert','venue':'A','start':20,'end':40,'quantity':2}])
        self.assertEquivalent()
        self.apply([{'id':'a','rev':2,'op':'upsert','venue':'A','start':0,'end':10,'quantity':2}])
        self.assertEquivalent()
        self.apply([{'id':'a','rev':3,'op':'upsert','venue':'A','start':0,'end':500,'quantity':1}])
        self.assertEquivalent()
    def test_withdraw_last_booking_keeps_empty_venue_segment(self):
        self.init((0,30),{'A':2,'B':1},{'a':('A',5,15,2)})
        self.apply([{'id':'a','rev':1,'op':'delete'}])
        result=self.assertEquivalent()
        self.assertEqual(result['venues'][0]['segments'],[{'start':0,'end':30,'occupancy':0,'excess':0}])
        self.assertEqual(result['venues'][0]['peak'],0)
        self.assertEqual(result['venues'][0]['booked_unit_ticks'],0)
    def test_merge_across_region_boundary(self):
        self.init((0,40),{'A':1},{'a':('A',0,10,1),'b':('A',20,30,1)})
        self.apply([{'id':'a','rev':1,'op':'upsert','venue':'A','start':0,'end':20,'quantity':1}])
        result=self.assertEquivalent()
        self.assertEqual(len(result['venues'][0]['segments']),2)
    def test_cross_venue_move_removes_old_and_adds_new(self):
        self.init((0,60),{'A':4,'B':4},{'a':('A',0,20,3),'b':('B',30,50,2)})
        self.apply([{'id':'a','rev':1,'op':'upsert','venue':'B','start':35,'end':45,'quantity':1}])
        result=self.assertEquivalent()
        self.assertEqual(next(v for v in result['venues'] if v['venue']=='A')['peak'],0)
        self.assertEqual(next(v for v in result['venues'] if v['venue']=='B')['peak'],3)
    def test_superseded_revisions_do_not_repaint(self):
        self.init((0,40),{'A':2},{})
        self.apply([{'id':'x','rev':5,'op':'upsert','venue':'A','start':0,'end':10,'quantity':2}])
        before=self.export()['result']
        self.apply([{'id':'x','rev':3,'op':'upsert','venue':'A','start':20,'end':30,'quantity':2},
                    {'id':'x','rev':4,'op':'delete'}])
        after=self.export()['result']
        self.assertEqual(before,after)
        self.assertEqual(self.export()['generation'],2)
    def test_row_order_of_export_is_stable(self):
        self.init((0,20),{'A':2,'B':2},{'a':('A',0,10,2),'b':('B',0,10,1)})
        first=self.export()
        self.apply([])
        self.assertEqual(first,self.export())
        self.assertEqual([v['venue'] for v in first['result']['venues']],['A','B'])

class Differential(Harness):
    def test_randomized_steps_match_static_model(self):
        rng=random.Random(20261116)
        horizon=(0,80)
        venues={'A':2,'B':1,'C':0,'D':9}
        self.init(horizon,venues,{'x':('A',0,10,2),'y':('B',5,20,3)})
        revisions={k:0 for k in ('x','y')}
        for step in range(400):
            batch=[]
            for _ in range(rng.randint(1,4)):
                bid='id%d'%rng.randint(1,15)
                revisions[bid]=revisions.get(bid,0)+rng.randint(1,3)
                record={'id':bid,'rev':revisions[bid],'op':'delete'} if rng.random()<0.25 else {
                    'id':bid,'rev':revisions[bid],'op':'upsert','venue':rng.choice(list(venues)),
                    'start':rng.randint(0,79),'end':None,'quantity':rng.randint(1,4)}
                if 'start' in record:record['end']=min(80,record['start']+rng.randint(1,20))
                batch.append(record)
            rng.shuffle(batch)
            self.apply(batch)
            if step%25==0:self.assertEquivalent()
        self.assertEquivalent()

    def test_batching_does_not_change_result(self):
        rng=random.Random(5);records=[]
        for i in range(200):
            records.append({'id':'r%d'%rng.randint(0,40),'rev':rng.randint(1,10**6),'op':'upsert','venue':rng.choice(['A','B']),
                            'start':rng.randint(0,50),'end':rng.randint(51,90),'quantity':rng.randint(1,3)})
        # Keep only non-conflicting (id,rev) replays so both runs are valid.
        seen={};clean=[]
        for record in records:
            key=(record['id'],record['rev'])
            if key in seen:
                if seen[key]==record:clean.append(record)
                continue
            seen[key]=record;clean.append(record)
        self.init((0,100),{'A':3,'B':3},{})
        flat=self.dir.name+'/flat.db';split=self.dir.name+'/split.db'
        dataset.init(flat,{'horizon':{'start':0,'end':100},'venues':[{'id':'A','capacity':3},{'id':'B','capacity':3}],'bookings':[]})
        dataset.init(split,{'horizon':{'start':0,'end':100},'venues':[{'id':'A','capacity':3},{'id':'B','capacity':3}],'bookings':[]})
        dataset.apply(flat,{'revisions':clean})
        for offset in range(0,len(clean),7):dataset.apply(split,{'revisions':clean[offset:offset+7]})
        dataset.apply(flat,{'revisions':list(reversed(clean[:50]))})
        dataset.apply(split,{'revisions':list(reversed(clean[:50]))})
        outs=[]
        for db in (flat,split):
            out=self.dir.name+'/'+Path(db).stem+'.json';dataset.export(db,out)
            with open(out) as stream:outs.append(json.load(stream))
        self.assertEqual(outs[0]['result'],outs[1]['result'])

class Seeds(Harness):
    """Several seeded runs over tight tick spaces, shared venues and multi-id batches."""
    def test_seeded_runs_match_static_model(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                self.setUp()
                rng=random.Random(seed)
                venues={'V%d'%i:rng.randint(0,6) for i in range(rng.randint(1,5))}
                horizon=(rng.randint(0,5),rng.randint(40,60))
                self.init(horizon,venues,{})
                revisions={}
                for step in range(60):
                    batch=[]
                    for _ in range(rng.randint(1,5)):
                        bid='id%d'%rng.randint(0,9)
                        revisions[bid]=revisions.get(bid,0)+rng.randint(1,2)
                        if rng.random()<0.3:
                            batch.append({'id':bid,'rev':revisions[bid],'op':'delete'})
                        else:
                            venue=rng.choice(list(venues));start=rng.randint(horizon[0],horizon[1])
                            batch.append({'id':bid,'rev':revisions[bid],'op':'upsert','venue':venue,
                                          'start':start,'end':rng.randint(start+1,horizon[1]+3),
                                          'quantity':rng.randint(1,5)})
                    rng.shuffle(batch)
                    self.apply(batch)
                    if step%10==0:self.assertEquivalent()
                self.assertEquivalent()
                self.tearDown()
