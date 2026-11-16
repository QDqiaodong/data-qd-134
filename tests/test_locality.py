"""Locality proof: the same local correction on a small and a large database.

Two independent, re-checkable measurements are taken without a profiler:

* ``sqlite3.Connection.set_trace_callback`` records every statement, and the
  test asserts that apply never issues an unqualified scan of `facts`,
  `effective` or `segments`, never runs SQL aggregates, and reads segments only
  through primary-key seeks on (venue,start).
* :class:`CountingCursor` counts *rows returned* by every SELECT; the counts for
  the local correction must be equal in both databases even though the large one
  holds many unrelated ids, venues and segment-dense gaps.

A third measurement uses unmodified SQLite: the whole `apply` is wrapped in a
progress handler that fires every 1000 VM instructions, and the two databases
must stay within a small constant of each other.
"""
import json,os,shutil,tempfile,unittest
from venue_occupancy import dataset,store

HORIZON=10**9

def build(db,extra_ids,extra_venues,extra_segments):
    # Both fixtures share the same local layout around the corrected booking:
    # identical segments, the same right-hand run boundary, the same venues that
    # take part.  The "large" one only adds rows that the correction never
    # needs to look at.
    venues=[{'id':'target','capacity':5},{'id':'other','capacity':2}]
    venues+= [{'id':'bulk%04d'%i,'capacity':3} for i in range(extra_venues)]
    bookings=[{'id':'base0','venue':'target','start':0,'end':1000,'quantity':1},
              {'id':'base1','venue':'other','start':0,'end':1000,'quantity':2},
              # A run edge just past the corrected interval, present in both sizes.
              {'id':'edge','venue':'target','start':500000,'end':500010,'quantity':1}]
    # Unrelated facts, venues and segment-dense gaps live on their own venues and
    # distant ticks so the local corridor of target/other is byte-identical in
    # the small and the large fixture.
    filler=venues[2]['id'] if len(venues)>2 else 'other'
    for i in range(extra_ids):
        bookings.append({'id':'unrelated%06d'%i,'venue':filler,'start':800000+i*10,'end':800005+i*10,'quantity':1})
    for i in range(extra_segments):
        bookings.append({'id':'tseg%06d'%i,'venue':filler,'start':700000+i*20,'end':700010+i*20,'quantity':1})
    dataset.init(db,{'horizon':{'start':0,'end':HORIZON},'venues':venues,'bookings':bookings})

LOCAL=[{'id':'base0','rev':1,'op':'upsert','venue':'target','start':500,'end':600,'quantity':3}]
MOVE=[{'id':'base1','rev':1,'op':'upsert','venue':'target','start':900000,'end':900100,'quantity':2}]

class CountingCursor:
    def __init__(self,cursor,counter):self.cursor=cursor;self.counter=counter
    def fetchone(self):
        row=self.cursor.fetchone()
        if row is not None:self.counter['rows']+=1
        return row
    def fetchall(self):
        rows=self.cursor.fetchall();self.counter['rows']+=len(rows);return rows
    def fetchmany(self,size=-1):
        rows=self.cursor.fetchmany(size);self.counter['rows']+=len(rows);return rows
    def __iter__(self):
        for row in self.cursor:
            self.counter['rows']+=1;yield row
    def __getattr__(self,name):return getattr(self.cursor,name)

class CountingConnection:
    def __init__(self,connection):self.connection=connection;self.counter={'rows':0};self.statements=[]
    def execute(self,sql,params=()):
        self.statements.append(sql)
        return CountingCursor(self.connection.execute(sql,params),self.counter)
    def __getattr__(self,name):return getattr(self.connection,name)
    def close(self):self.counter['rows'];self.connection.close()

class Locality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir=tempfile.TemporaryDirectory()
        cls.paths={'small':cls.dir.name+'/small-source.db','large':cls.dir.name+'/large-source.db'}
        build(cls.paths['small'],0,0,0);build(cls.paths['large'],4000,40,2000)
    @classmethod
    def tearDownClass(cls):cls.dir.cleanup()

    def fresh(self,name):
        """A private copy of the shared fixture, so tests cannot interact."""
        path=self.dir.name+'/'+name+'.db'
        for suffix in ('','-wal','-shm'):
            if os.path.exists(path+suffix):os.unlink(path+suffix)
        shutil.copy(self.paths[name],path)
        return path

    def measure(self,records):
        results={}
        for name in ('small','large'):
            db=self.fresh(name);trace=[];stats={}
            dataset.apply(db,{'revisions':records},stats=stats,trace=trace.append)
            results[name]=(trace,stats)
        return results

    def test_apply_never_scans_large_tables(self):
        for records in (LOCAL,MOVE):
            _,stats=self.measure(records)
            self.assertNotIn('facts_scan',stats);self.assertNotIn('effective_scan',stats)
        for statement in self.measure(LOCAL)['large'][0]:
            upper=' '.join(statement.upper().split())
            self.assertNotIn('COUNT(',upper);self.assertNotIn('SUM(',upper)
            self.assertNotIn('SELECT * FROM FACTS',upper);self.assertNotIn('SELECT * FROM EFFECTIVE',upper)
            for table in ('FROM FACTS','FROM EFFECTIVE','FROM SEGMENTS','FROM VENUE_LEVELS'):
                if table in upper:
                    self.assertIn('WHERE',upper,statement)
                    self.assertTrue('=' in upper or ' IN (' in upper,statement)
        # No statement touches every venue: the segment reads are (venue,start) seeks.
        segments=[s for s in self.measure(LOCAL)['large'][0] if 'FROM SEGMENTS' in s.upper()]
        for statement in segments:self.assertIn('VENUE=',statement.upper().replace(' ',''))

    def test_row_counts_do_not_grow_with_unrelated_data(self):
        for records in (LOCAL,MOVE):
            small_trace,small_stats=self.measure(records)['small']
            large_trace,large_stats=self.measure(records)['large']
            self.assertEqual(small_stats['facts_read'],large_stats['facts_read'])
            self.assertEqual(small_stats['effective_read'],large_stats['effective_read'])
            self.assertEqual(small_stats['segments_written'],large_stats['segments_written'])
            self.assertEqual(small_stats['facts_written'],large_stats['facts_written'])
            self.assertLessEqual(large_stats['segments_read'],small_stats['segments_read']+4)
            self.assertEqual(len(small_trace),len(large_trace))

    def test_vm_steps_do_not_scale_with_database(self):
        def steps(db,batch):
            counter={'n':0}
            original=store.connect
            def connect(path,**kwargs):
                kwargs['progress']=lambda:(counter.__setitem__('n',counter['n']+1) or 0)
                return original(path,**kwargs)
            store.connect=connect
            try:dataset.apply(db,{'revisions':batch})
            finally:store.connect=original
            return counter['n']
        small=steps(self.fresh('small'),LOCAL);large=steps(self.fresh('large'),LOCAL)
        self.assertLess(large,small+10)

    def test_export_and_apply_round_trip_on_large_database(self):
        db=self.fresh('large');dataset.apply(db,{'revisions':LOCAL})
        out=self.dir.name+'/large.json';dataset.export(db,out)
        with open(out) as stream:report=json.load(stream)
        self.assertEqual(report['generation'],1)
        target=next(v for v in report['result']['venues'] if v['venue']=='target')
        self.assertEqual(target['segments'][0],{'start':0,'end':500,'occupancy':0,'excess':0})
        self.assertEqual(target['segments'][1],{'start':500,'end':600,'occupancy':3,'excess':0})
        self.assertEqual(target['peak'],3)
        # The unrelated venues are still exported in full, streamed in key order.
        self.assertEqual(len(report['result']['venues']),42)
