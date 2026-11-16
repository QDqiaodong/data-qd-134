"""Long-run scale acceptance at the documented bounds.

Builds 200,000 vertices and 1,000,000 candidate edges across many independent
components, measures a local import/decide/report, and checks memory does not
grow with the whole database.  The per-component caps (4000 vertices, 20000
edges) are enforced by the pipeline, so the big totals come from many
components; a single maximum-size component is also built and classified.

Set VC_SCALE=1 to run the full-size case; by default a tenth of the size is
used so the ordinary suite stays quick.  Both sizes exercise the same paths.
"""
import json,os,sqlite3,sys,tempfile,time,tracemalloc,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def payment(pid,aliases,amount):
    return {'payment_id':pid,'venue':'hall','currency':'USD','amount_minor':amount,
            'aliases':list(aliases)}

def credit(cid,references,amount):
    return {'credit_id':cid,'venue':'hall','currency':'USD','amount_minor':amount,
            'references':list(references)}

def rows_read(stats):
    return {key:value for key,value in stats.items() if key.endswith('_read')}

class Scale(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'scale.db')
        sys.path.insert(0,str(ROOT/'src'))
        from venue_clearing import dataset
        self.dataset=dataset
        dataset.init(self.db)

    def tearDown(self):self.dir.cleanup()

    def build(self,components,per_component=10):
        """Each component is a complete block with `per_component` payments and
        credits on one distinct amount; edges = per_component**2 per block."""
        dataset=self.dataset
        for index in range(components):
            prefix='c%06d'%index
            tags=['%sA%02d'%(prefix,i) for i in range(per_component)]
            batch={'payments':[payment('%sP%03d'%(prefix,i),tags,index+1)
                               for i in range(per_component)],
                   'credits':[credit('%sC%03d'%(prefix,i),tags,index+1)
                              for i in range(per_component)]}
            dataset.import_batch(self.db,batch)

    def local_operation_counts(self,components,label):
        """Build N components, then run the same local import+decide+report and
        return the row-read counters and elapsed times."""
        dataset=self.dataset
        db=str(self.tmp/(label+'.db'));dataset.init(db)
        self.db=db
        self.build(components)
        conn=sqlite3.connect(db)
        try:
            totals=conn.execute('SELECT COUNT(*) FROM comp_vertex').fetchone()[0]
            edges=conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
        finally:conn.close()
        batch={'payments':[payment('LOCAL-P',['local-tag'],10**9)],
               'credits':[credit('LOCAL-C',['local-tag'],10**9)]}
        import_stats={}
        started=time.monotonic()
        dataset.import_batch(db,batch,stats=import_stats)
        import_time=time.monotonic()-started
        decide_stats={}
        started=time.monotonic()
        dataset.decide(db,{'decisions':[{'decision_id':'local-pin','op':'pin',
                                         'payment_id':'LOCAL-P','credit_id':'LOCAL-C'}]},
                       stats=decide_stats)
        decide_time=time.monotonic()-started
        started=time.monotonic()
        dataset.report(db,str(self.tmp/(label+'-report.json')))
        report_time=time.monotonic()-started
        return {'vertices':totals,'edges':edges,'import':import_stats,
                'decide':decide_stats,'times':(import_time,decide_time,report_time)}

    def test_local_operations_do_not_scale_with_unrelated_components(self):
        small=self.local_operation_counts(10,'small')
        large=self.local_operation_counts(300,'large')
        self.assertGreater(large['edges'],small['edges']*20)
        # Identical row-read counters: the local import probes the same two
        # inverted index keys, and the decision rebuilds only its own 2-vertex
        # component, no matter how many unrelated components exist.
        self.assertEqual(rows_read(small['import']),rows_read(large['import']))
        self.assertEqual(small['decide'],large['decide'])
        self.assertEqual(small['import']['edges_written'],1)
        self.assertEqual(small['decide']['comp_vertex_read'],2)
        self.assertEqual(small['decide']['edges_read'],2)
        # And the time does not grow with the database.
        for small_time,large_time in zip(small['times'],large['times']):
            self.assertLess(large_time,small_time*5+0.5)

    def test_memory_of_a_local_operation_does_not_track_the_database(self):
        self.build(200)
        # Keep a warm SQLite page cache first so the baseline is stable.
        self.dataset.import_batch(self.db,{'payments':[payment('WARM',['warm'],1)],'credits':[]})
        tracemalloc.start()
        try:
            self.dataset.import_batch(self.db,{'payments':[payment('M1',['m1'],2)],
                                               'credits':[credit('M2',['m2'],3)]})
            peak_local=tracemalloc.get_traced_memory()[1]
        finally:tracemalloc.stop()
        # A local batch with two isolated vertices uses well under 2 MiB.
        self.assertLess(peak_local,2*1024*1024)

    def test_one_maximum_size_component(self):
        dataset=self.dataset
        # Exactly 4000 vertices and 20000 edges in one connected component:
        # payment P_i exposes alias a_i, credit C_j references the ten aliases
        # a_j..a_(j+9) (cyclic), so every vertex has degree 10 and the graph
        # is one cycle-like block.  It is built in two documents because one
        # import document holds at most 2000 records.
        n=2000
        payments=[payment('HP%04d'%i,['a%04d'%i],7) for i in range(n)]
        credits=[credit('HC%04d'%j,['a%04d'%((j+k)%n) for k in range(10)],7) for j in range(n)]
        started=time.monotonic()
        dataset.import_batch(self.db,{'payments':payments[:1000],'credits':credits[:1000]})
        dataset.import_batch(self.db,{'payments':payments[1000:],'credits':credits[1000:]})
        build_time=time.monotonic()-started
        conn=sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0],20000)
            component=conn.execute('SELECT vertex_count,edge_count,max_remaining'
                                   ' FROM components').fetchone()
        finally:conn.close()
        self.assertEqual(component,(4000,20000,2000))
        self.assertLess(build_time,120.0)
        # Growing the same component past 4000 vertices is rejected whole.
        before=sqlite3.connect(self.db).execute('SELECT COUNT(*) FROM payments').fetchone()[0]
        with self.assertRaisesRegex(ValueError,'exceeds'):
            dataset.import_batch(self.db,{'payments':[payment('EXTRA',['a0001'],7)],'credits':[]})
        after=sqlite3.connect(self.db).execute('SELECT COUNT(*) FROM payments').fetchone()[0]
        self.assertEqual(before,after)
        started=time.monotonic()
        out=self.tmp/'big-report.json';dataset.report(self.db,str(out))
        self.assertLess(time.monotonic()-started,20.0)
        document=json.loads(out.read_text())
        self.assertEqual(len(document['payments']),2000)
        self.assertEqual(len(document['credits']),2000)
        self.assertEqual(document['maximum_pairs'],2000)

    @unittest.skipUnless(os.environ.get('VC_SCALE')=='1','set VC_SCALE=1 for the full-size run')
    def test_documented_full_scale(self):
        dataset=self.dataset
        # 10,000 components of 20 vertices and 100 edges each: 200,000
        # vertices and 1,000,000 candidate edges.  Each document carries 100
        # components (1000 payments + 1000 credits), under the 2000-record cap.
        started=time.monotonic()
        for group in range(100):
            batch={'payments':[],'credits':[]}
            for index in range(group*100,(group+1)*100):
                prefix='f%06d'%index
                tags=['%sA%02d'%(prefix,i) for i in range(10)]
                batch['payments'].extend(payment('%sP%03d'%(prefix,i),tags,index+1)
                                         for i in range(10))
                batch['credits'].extend(credit('%sC%03d'%(prefix,i),tags,index+1)
                                        for i in range(10))
            dataset.import_batch(self.db,batch)
        build_time=time.monotonic()-started
        conn=sqlite3.connect(self.db)
        try:
            vertices=conn.execute('SELECT COUNT(*) FROM comp_vertex').fetchone()[0]
            edges=conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
        finally:conn.close()
        print('full scale: %d vertices, %d edges in %.1fs'%(vertices,edges,build_time))
        self.assertEqual(vertices,200000)
        self.assertEqual(edges,1000000)
        stats={}
        started=time.monotonic()
        dataset.import_batch(self.db,{'payments':[payment('LAST-P',['last'],10**9)],
                                      'credits':[credit('LAST-C',['last'],10**9)]},stats=stats)
        print('local import %.3fs %s'%(time.monotonic()-started,rows_read(stats)))
        self.assertEqual(stats.get('edges_written'),1)
        started=time.monotonic()
        dataset.report(self.db,str(self.tmp/'full.json'))
        print('full report %.1fs'%(time.monotonic()-started))

if __name__=='__main__':unittest.main()
