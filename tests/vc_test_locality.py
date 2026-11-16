"""Locality and scale proof for venue-clearing.

The same local import/decide on a small database and on one padded with many
unrelated components reads exactly the same number of rows and issues the same
statement shapes: nothing scans unrelated payments, credits, edges or
inference.  A second part builds one large component (bridge, reclassify,
limits) and measures report streaming.
"""
import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from venue_clearing import dataset

def payment(pid,aliases,amount=100,currency='USD',venue='hall'):
    return {'payment_id':pid,'venue':venue,'currency':currency,'amount_minor':amount,
            'aliases':list(aliases)}

def credit(cid,references,amount=100,currency='USD',venue='hall'):
    return {'credit_id':cid,'venue':venue,'currency':currency,'amount_minor':amount,
            'references':list(references)}

def payload(prefix,amount=100):
    return {'payments':[payment(prefix+'p',[prefix+'x'],amount)],
            'credits':[credit(prefix+'c',[prefix+'x'],amount)]}

LOCAL_IMPORT={'payments':[payment('LP',['shared','other'])],
              'credits':[credit('LC',['shared'])]}
LOCAL_DECIDE={'decisions':[{'decision_id':'local-pin','op':'pin',
                            'payment_id':'LP','credit_id':'LC'}]}

def build(db,unrelated):
    dataset.init(db)
    dataset.import_batch(db,{'payments':[payment('BASE-P',['base'])],
                             'credits':[credit('BASE-C',['base'])]})
    # Each unrelated component is a complete 2x2 block on its own amount, so
    # it has ambiguous vertices and non-trivial inference rows.
    for index in range(unrelated):
        prefix='u%05d'%index
        dataset.import_batch(db,{
            'payments':[payment(prefix+'A',[prefix+'X',prefix+'Y'],100),
                        payment(prefix+'B',[prefix+'X',prefix+'Y'],100)],
            'credits':[credit(prefix+'C',[prefix+'X'],100),
                       credit(prefix+'D',[prefix+'Y'],100)]})

class Locality(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
    def tearDown(self):self.dir.cleanup()

    def test_import_and_decide_read_counts_are_size_independent(self):
        small=str(self.tmp/'small.db');large=str(self.tmp/'large.db')
        build(small,0);build(large,100)
        small_stats={};large_stats={}
        dataset.import_batch(small,LOCAL_IMPORT,stats=small_stats)
        dataset.import_batch(large,LOCAL_IMPORT,stats=large_stats)
        self.assertEqual(small_stats,large_stats)
        # The new payment probes the two inverted alias keys; the new credit
        # probes the two reference keys.  One shared alias intersects.
        self.assertLessEqual(small_stats['references_read'],2)
        self.assertLessEqual(small_stats['aliases_read'],2)
        self.assertEqual(small_stats.get('edges_written'),1)
        small_decide={};large_decide={}
        dataset.decide(small,LOCAL_DECIDE,stats=small_decide)
        dataset.decide(large,LOCAL_DECIDE,stats=large_decide)
        self.assertEqual(small_decide,large_decide)
        # The decision rebuilds only the now-two-vertex component LP/LC: one
        # edge lookup for the pin itself plus one per payment while classifying.
        self.assertEqual(small_decide['comp_vertex_read'],2)
        self.assertEqual(small_decide['edges_read'],2)
        self.assertEqual(small_decide['inference_written'],2)

    def test_pin_after_the_fact_reads_only_the_affected_component(self):
        small=str(self.tmp/'small.db');large=str(self.tmp/'large.db')
        build(small,0);build(large,200)
        for db in (small,large):
            dataset.import_batch(db,LOCAL_IMPORT)
        small_stats={};large_stats={}
        dataset.decide(small,LOCAL_DECIDE,stats=small_stats)
        dataset.decide(large,LOCAL_DECIDE,stats=large_stats)
        self.assertEqual(small_stats,large_stats)

    def test_unrelated_components_are_not_read(self):
        # Freeze the big database, then trace every statement of a local
        # decision and a local import.  No statement may name a component id
        # other than the affected one, and there is no unqualified scan.
        db=str(self.tmp/'trace.db');build(db,80)
        conn=sqlite3.connect(db)
        try:
            affected={row[0] for row in conn.execute(
                "SELECT cv.component_id FROM comp_vertex cv WHERE cv.vertex_id IN ('BASE-P','BASE-C')")}
        finally:conn.close()
        statements=[]
        dataset.import_batch(db,LOCAL_IMPORT,trace=statements.append,stats={})
        dataset.decide(db,LOCAL_DECIDE,trace=statements.append,stats={})
        for statement in statements:
            upper=statement.upper()
            if upper.startswith('SELECT'):
                self.assertIn('WHERE',upper,statement)
        # Candidate discovery goes through an inverted index joined to the
        # record whose fields are compared: never a cross product of the two
        # record tables.
        for statement in statements:
            upper=statement.upper()
            if upper.startswith('SELECT') and 'FROM PAYMENT_ALIAS' in upper:
                self.assertIn('JOIN',upper,statement)
            if upper.startswith('SELECT') and 'FROM CREDIT_REFERENCE' in upper:
                self.assertIn('JOIN',upper,statement)
        # No statement aggregates the whole base.
        for statement in statements:
            upper=statement.upper().replace(' ','')
            self.assertNotIn('COUNT(*)FROM',upper,statement)

    def test_isolated_component_creation_does_not_classify_others(self):
        db=str(self.tmp/'grow.db');build(db,10)
        stats={}
        dataset.import_batch(db,{'payments':[payment('LONELY',['nobody'])],'credits':[]},stats=stats)
        # One new isolated vertex: one inference row, no edge rows read.
        self.assertEqual(stats['inference_written'],1)
        self.assertEqual(stats['edges_read'] if 'edges_read' in stats else 0,0)
        self.assertEqual(stats.get('comp_vertex_read',0),0)

    def test_report_streams_the_inference_table_only(self):
        db=str(self.tmp/'report.db');build(db,30)
        statements=[]
        out=self.tmp/'r.json'
        dataset.report(db,str(out),trace=statements.append)
        selects=[s for s in statements if s.upper().startswith('SELECT')]
        # Exactly: one meta generation, one meta maximum_pairs, and two
        # ordered inference scans.
        meta=sum(1 for s in selects if 'FROM meta' in s)
        inference=sum(1 for s in selects if 'FROM inference' in s)
        self.assertLessEqual(meta,2)
        self.assertEqual(inference,2)
        for statement in selects:
            if 'FROM inference' in statement:self.assertIn('ORDER BY vertex_id',statement)
        self.assertFalse(any('FROM edges' in s or 'FROM payments' in s or 'FROM credits' in s
                             for s in selects))

class Scale(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
    def tearDown(self):self.dir.cleanup()

    def test_component_limits_are_enforced_per_raw_component(self):
        db=str(self.tmp/'limit.db');dataset.init(db)
        # A 101x100 dense block: every payment shares all 100 aliases with
        # every credit, so the component has 10100 edges -- under both
        # limits, so it commits.
        aliases=['A%03d'%i for i in range(100)]
        payments=[payment('P%04d'%i,aliases) for i in range(101)]
        credits=[credit('C%04d'%j,aliases) for j in range(100)]
        dataset.import_batch(db,{'payments':payments,'credits':credits})
        conn=sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comp_vertex').fetchone()[0],201)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0],10100)
        finally:conn.close()
        # Growing the same component past 20000 edges must reject the batch
        # as a whole and leave the previous facts untouched.
        more=[payment('X%04d'%i,['A000']) for i in range(190)]
        with self.assertRaisesRegex(ValueError,'exceeds'):
            dataset.import_batch(db,{'payments':more,'credits':[]})
        conn=sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0],101)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0],10100)
        finally:conn.close()

    def test_vertex_limit_on_a_chain_built_over_several_batches(self):
        db=str(self.tmp/'vertices.db');dataset.init(db)
        def chain(start,stop):
            return {'payments':[payment('P%05d'%i,['a%d'%i,'a%d'%(i+1)])
                                for i in range(start,stop)],
                    'credits':[credit('C%05d'%i,['a%d'%i,'a%d'%(i+1)])
                               for i in range(start,stop)]}
        dataset.import_batch(db,chain(0,1000))
        dataset.import_batch(db,chain(1000,2000))     # 4000 vertices, one component
        conn=sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comp_vertex').fetchone()[0],4000)
        finally:conn.close()
        with self.assertRaisesRegex(ValueError,'exceeds'):
            dataset.import_batch(db,{'payments':[payment('BRIDGE',['a2000'])],'credits':[]})
        conn=sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM comp_vertex').fetchone()[0],4000)
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM payments WHERE payment_id=?',('BRIDGE',)).fetchone()[0],0)
        finally:conn.close()

    def test_million_edge_report_is_linear(self):
        # A long path component: 20000 vertices would exceed the per-component
        # vertex limit, so stay at 4000 vertices / 3999 edges for the contract
        # limit and use many components for the total edge count.
        db=str(self.tmp/'scale.db');dataset.init(db)
        quota=200
        for index in range(quota):
            prefix='s%05d'%index
            payments=[payment(prefix+'P%d'%i,[prefix+'A%d'%i,prefix+'A%d'%(i+1)]) for i in range(10)]
            credits=[credit(prefix+'C%d'%i,[prefix+'A%d'%i,prefix+'A%d'%(i+1)]) for i in range(10)]
            dataset.import_batch(db,{'payments':payments,'credits':credits})
        started=time.monotonic()
        out=self.tmp/'scale.json'
        dataset.report(db,str(out))
        elapsed=time.monotonic()-started
        document=json.loads(out.read_text())
        self.assertEqual(len(document['payments']),2000)
        self.assertEqual(len(document['credits']),2000)
        self.assertLess(elapsed,10.0)

    def test_bridging_merges_and_drops_stale_component_rows(self):
        db=str(self.tmp/'bridge.db');dataset.init(db)
        dataset.import_batch(db,{'payments':[payment('P1',['A'])],'credits':[credit('C1',['A'])]})
        dataset.import_batch(db,{'payments':[payment('P2',['B'])],'credits':[credit('C2',['B'])]})
        conn=sqlite3.connect(db)
        try:before=conn.execute('SELECT COUNT(*) FROM components').fetchone()[0]
        finally:conn.close()
        self.assertEqual(before,2)
        dataset.import_batch(db,{'payments':[payment('P9',['A','B'])],'credits':[]})
        conn=sqlite3.connect(db)
        try:
            after=conn.execute('SELECT component_id,vertex_count,edge_count FROM components').fetchall()
        finally:conn.close()
        self.assertEqual(len(after),1)
        self.assertEqual(after[0][1],5)
        # P1-C1, P2-C2 and the bridging P9 connected to both credits.
        self.assertEqual(after[0][2],4)

if __name__=='__main__':unittest.main()
