"""Cross-layer acceptance: binding boundaries, component reclassification,
bridging, generation, failure rollback, snapshot reports and differential
comparison with the exhaustive oracle."""
import json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from venue_clearing import dataset
from tests import vc_test_oracle as oracle

ROOT=Path(__file__).resolve().parents[1]

def payment(pid,aliases=('A',),amount=100,currency='USD',venue='hall'):
    return {'payment_id':pid,'venue':venue,'currency':currency,'amount_minor':amount,
            'aliases':list(aliases)}

def credit(cid,references=('A',),amount=100,currency='USD',venue='hall'):
    return {'credit_id':cid,'venue':venue,'currency':currency,'amount_minor':amount,
            'references':list(references)}

def tables(db):
    conn=sqlite3.connect(db)
    try:
        return {name:conn.execute('SELECT * FROM %s ORDER BY 1'%name).fetchall()
                for name in ('meta','payments','credits','payment_alias','credit_reference',
                             'edges','components','comp_vertex','inference','bindings','decisions',
                             'imports')}
    finally:conn.close()

def statuses(db):
    conn=sqlite3.connect(db)
    try:
        return {(row[0],row[1]):(row[2],json.loads(row[3]),bool(row[4]))
                for row in conn.execute('SELECT kind,vertex_id,status,peers,can_be_unmatched FROM inference')}
    finally:conn.close()

class Verify(unittest.TestCase):
    def check(self,db):
        payments,credits,pins,stored=oracle.load(db)
        self.assertEqual(stored,oracle.edges_by_scan(payments,credits),'stored edges')
        expected=oracle.classify(payments,credits,pins)
        out=Path(db).with_suffix('.report.json');dataset.report(db,out)
        document=json.loads(out.read_text())
        expected['generation']=document['generation']
        self.assertEqual(document,expected)

class Linkage(Verify):
    def read_document(self):
        out=self.tmp/'document.json';dataset.report(self.db,out)
        return {'text':out.read_text(),'document':json.loads(out.read_text())}

    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db');dataset.init(self.db)
    def tearDown(self):self.dir.cleanup()

    def test_2x2_ambiguous_then_pinned_then_unpinned(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',['A','B']),payment('P2',['A','B'])],
                                      'credits':[credit('C1',['A']),credit('C2',['B'])]})
        before=statuses(self.db)
        for vertex in (('P','P1'),('P','P2'),('C','C1'),('C','C2')):
            self.assertEqual(before[vertex][0],'ambiguous')
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        after_pin=statuses(self.db)
        self.assertEqual(after_pin[('P','P1')][0],'pinned')
        self.assertEqual(after_pin[('C','C1')][0],'pinned')
        self.assertEqual(after_pin[('P','P2')],('forced',['C2'],False))
        self.assertEqual(after_pin[('C','C2')],('forced',['P2'],False))
        dataset.decide(self.db,{'decisions':[{'decision_id':'d2','op':'unpin','payment_id':'P1'}]})
        after_unpin=statuses(self.db)
        for vertex in (('P','P1'),('P','P2'),('C','C1'),('C','C2')):
            self.assertEqual(after_unpin[vertex][0],'ambiguous')
        self.check(self.db)

    def test_binding_only_reclassifies_its_own_component(self):
        dataset.import_batch(self.db,{
            'payments':[payment('P1'),payment('P2',['Z'])],
            'credits':[credit('C1'),credit('C2',['Z'])]})
        # Two separate components, each a single forced pair.
        conn=sqlite3.connect(self.db)
        try:
            components=conn.execute('SELECT component_id FROM comp_vertex WHERE kind=\'P\''
                                    ' ORDER BY vertex_id').fetchall()
        finally:conn.close()
        self.assertEqual(len(components),2)
        self.assertNotEqual(components[0],components[1])
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        aware=statuses(self.db)
        self.assertEqual(aware[('P','P1')][0],'pinned')
        self.assertEqual(aware[('P','P2')][0],'forced')     # untouched component
        self.check(self.db)

    def test_new_credit_bridges_two_components_and_restores_flexibility(self):
        # Two disconnected forced pairs...
        dataset.import_batch(self.db,{'payments':[payment('P1',['A']),payment('P2',['B'])],
                                      'credits':[credit('C1',['A']),credit('C2',['B'])]})
        self.assertEqual(statuses(self.db)[('P','P1')][0],'forced')
        # ...then one credit that references both alias families merges them.
        dataset.import_batch(self.db,{'payments':[],'credits':[credit('C3',['A','B'])]})
        merged=statuses(self.db)
        self.assertEqual(merged[('C','C3')][0],'ambiguous')
        self.assertEqual(merged[('P','P1')][0],'ambiguous')
        conn=sqlite3.connect(self.db)
        try:
            rows=conn.execute('SELECT COUNT(DISTINCT component_id) FROM comp_vertex').fetchone()[0]
        finally:conn.close()
        self.assertEqual(rows,1)
        self.check(self.db)

    def test_failure_in_the_middle_of_inference_rolls_every_layer_back(self):
        dataset.import_batch(self.db,{'payments':[payment('P0',['A'])],
                                      'credits':[credit('C0',['A'])]})
        before=tables(self.db)
        generation=json.loads(self.read_document()['text'])['generation']
        # Three separate new components: the third reclassification fails.
        batch={'payments':[payment('PA',['A1']),payment('PB',['A2']),payment('PC',['A3'])],
               'credits':[credit('CA',['A1']),credit('CB',['A2']),credit('CC',['A3'])]}
        with self.assertRaisesRegex(ValueError,'injected'):
            dataset.import_batch(self.db,batch,fail_after_steps=2)
        self.assertEqual(tables(self.db),before)
        self.assertEqual(json.loads(self.read_document()['text'])['generation'],generation)
        # Reopening the database (a new connection) sees the same old state.
        self.assertEqual(tables(self.db),before)
        # The retry without the fault equals a clean run from the same base.
        dataset.import_batch(self.db,batch)
        clean=str(self.tmp/'clean.db');dataset.init(clean)
        dataset.import_batch(clean,{'payments':[payment('P0',['A'])],
                                    'credits':[credit('C0',['A'])]})
        dataset.import_batch(clean,batch)
        self.assertEqual(tables(self.db),tables(clean))

    def test_failure_in_inference_after_decisions_rolls_back(self):
        dataset.import_batch(self.db,{'payments':[payment('P1'),payment('P2',['Z'])],
                                      'credits':[credit('C1'),credit('C2',['Z'])]})
        before=tables(self.db)
        generation=json.loads(self.read_document()['text'])['generation']
        with self.assertRaisesRegex(ValueError,'injected'):
            dataset.decide(self.db,{'decisions':[
                {'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'},
                {'decision_id':'d2','op':'pin','payment_id':'P2','credit_id':'C2'}]},
                fail_after_steps=1)
        self.assertEqual(tables(self.db),before)
        self.assertEqual(json.loads(self.read_document()['text'])['generation'],generation)
        dataset.decide(self.db,{'decisions':[
            {'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'},
            {'decision_id':'d2','op':'pin','payment_id':'P2','credit_id':'C2'}]})
        self.check(self.db)

    def test_two_old_components_and_several_new_vertices_bridging_at_once(self):
        # Each old component is a single pair; the new batch adds two payments
        # and one credit that all share both alias families, so the union-find
        # over four old/or new nodes must merge everything exactly once.
        dataset.import_batch(self.db,{'payments':[payment('P1',['a']),payment('P2',['b'])],
                                      'credits':[credit('C1',['a']),credit('C2',['b'])]})
        dataset.import_batch(self.db,{'payments':[payment('PA',['a','b']),
                                                  payment('PB',['a','b'])],
                                      'credits':[credit('CC',['a','b'])]})
        conn=sqlite3.connect(self.db)
        try:
            rows=conn.execute('SELECT DISTINCT component_id FROM comp_vertex').fetchall()
            membership=conn.execute('SELECT kind,vertex_id FROM comp_vertex').fetchall()
            edges=conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
            component=conn.execute('SELECT vertex_count,edge_count FROM components').fetchone()
        finally:conn.close()
        self.assertEqual(len(rows),1)
        self.assertEqual(len(membership),7)
        self.assertEqual(edges,10)
        self.assertEqual(component,(7,10))
        self.check(self.db)

    def test_import_after_binding_reclassifies_merged_component(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',['A'])],
                                      'credits':[credit('C1',['A'])]})
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        # A new payment that shares the alias joins the pinned component; its
        # own classification must be fresh, not a stale inference row.
        dataset.import_batch(self.db,{'payments':[payment('P2',['A'])],'credits':[]})
        aware=statuses(self.db)
        self.assertEqual(aware[('P','P1')][0],'pinned')
        self.assertEqual(aware[('P','P2')][0],'unmatched')
        self.check(self.db)

    def test_random_sequences_match_the_oracle(self):
        import random
        random.seed(5)
        tags=['T0','T1','T2','T3']
        pins={};payments=[];credits=[];counter=[0]
        for step in range(25):
            action=random.choice(['P','C','P','C','pin','unpin'])
            counter[0]+=1
            if action=='P':
                record=payment('P%d'%len(payments),random.sample(tags,random.randint(1,2)),
                               random.choice([100,200]),random.choice(['USD','EUR']),
                               random.choice(['hall','annex']))
                dataset.import_batch(self.db,{'payments':[record],'credits':[]})
                payments.append(record)
            elif action=='C':
                record=credit('C%d'%len(credits),random.sample(tags,random.randint(1,2)),
                              random.choice([100,200]),random.choice(['USD','EUR']),
                              random.choice(['hall','annex']))
                dataset.import_batch(self.db,{'payments':[],'credits':[record]})
                credits.append(record)
            elif action=='pin':
                edges=oracle.edges_by_scan(
                    {r['payment_id']:(r['venue'],r['currency'],r['amount_minor'],r['aliases']) for r in payments},
                    {r['credit_id']:(r['venue'],r['currency'],r['amount_minor'],r['references']) for r in credits})
                free=[(p,c) for p,c in edges if p not in pins and c not in pins.values()]
                if free:
                    p,c=random.choice(free);pins[p]=c
                    dataset.decide(self.db,{'decisions':[{'decision_id':'D%d'%counter[0],'op':'pin',
                                                          'payment_id':p,'credit_id':c}]})
            else:
                if pins:
                    p=random.choice(list(pins));del pins[p]
                    dataset.decide(self.db,{'decisions':[{'decision_id':'D%d'%counter[0],'op':'unpin',
                                                          'payment_id':p}]})
            self.check(self.db)

class ReportTotals(Verify):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db');dataset.init(self.db)
    def tearDown(self):self.dir.cleanup()

    def test_maximum_pairs_is_pinned_plus_component_maxima(self):
        dataset.import_batch(self.db,{
            'payments':[payment('P1',['A','B']),payment('P2',['B']),payment('P3',['C'])],
            'credits':[credit('C1',['A']),credit('C2',['B']),credit('C3',['C'])]})
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin',
                                              'payment_id':'P2','credit_id':'C2'}]})
        out=self.tmp/'r.json';dataset.report(self.db,out)
        document=json.loads(out.read_text())
        pinned=sum(1 for row in document['payments']+document['credits']
                   if row['status']=='pinned')//2
        conn=sqlite3.connect(self.db)
        try:
            component_maxima=conn.execute('SELECT SUM(max_remaining) FROM components').fetchone()[0]
        finally:conn.close()
        self.assertEqual(document['maximum_pairs'],pinned+component_maxima)
        # The raw graph can pair all three payments, and the pin keeps that.
        self.assertEqual(document['maximum_pairs'],3)
        self.check(self.db)

    def test_a_pin_can_lower_the_maximum_number_of_pairs(self):
        dataset.import_batch(self.db,{
            'payments':[payment('P1',['A']),payment('P2',['A','B'])],
            'credits':[credit('C1',['A']),credit('C2',['B'])]})
        document=self.read()
        self.assertEqual(document['maximum_pairs'],2)
        # P2 has two possible credits, but pinning it to C1 is a legitimate
        # external confirmation even though it leaves P1 unmatched: the raw
        # graph's maximum (2) is not preserved.
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin',
                                              'payment_id':'P2','credit_id':'C1'}]})
        document=self.read()
        self.assertEqual(document['maximum_pairs'],1)
        self.assertEqual({row['id']:row['status'] for row in document['payments']},
                         {'P1':'unmatched','P2':'pinned'})
        self.check(self.db)

    def read(self):
        out=self.tmp/'read.json';dataset.report(self.db,out)
        return json.loads(out.read_text())

    def test_pure_replay_report_is_byte_identical(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',['A'])],'credits':[credit('C1',['A'])]})
        first=self.tmp/'a.json';dataset.report(self.db,first)
        dataset.import_batch(self.db,{'payments':[payment('P1',['A'])],'credits':[]})
        second=self.tmp/'b.json';dataset.report(self.db,second)
        self.assertEqual(first.read_bytes(),second.read_bytes())

class Generations(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db');dataset.init(self.db)
    def tearDown(self):self.dir.cleanup()

    def generation(self):
        out=self.tmp/'generation.json';dataset.report(self.db,out)
        return json.loads(out.read_text())['generation']

    def test_counts_only_fact_changing_batches(self):
        self.assertEqual(self.generation(),0)
        dataset.import_batch(self.db,{'payments':[payment('P1')],'credits':[]})
        self.assertEqual(self.generation(),1)
        # Pure replay does not bump.
        dataset.import_batch(self.db,{'payments':[payment('P1')],'credits':[]})
        self.assertEqual(self.generation(),1)
        dataset.import_batch(self.db,{'payments':[],'credits':[credit('C1')]})
        self.assertEqual(self.generation(),2)
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        self.assertEqual(self.generation(),3)
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        self.assertEqual(self.generation(),3)
        dataset.decide(self.db,{'decisions':[{'decision_id':'d2','op':'unpin','payment_id':'P1'}]})
        self.assertEqual(self.generation(),4)

    def test_failure_before_commit_rolls_back_every_layer(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',['A','B'])],
                                      'credits':[credit('C1',['A'])]})
        before=tables(self.db)
        generation=self.generation()
        with self.assertRaisesRegex(ValueError,'injected'):
            dataset.import_batch(self.db,{'payments':[payment('P2',['B'])],'credits':[]},
                                 fail_before_commit=True)
        self.assertEqual(tables(self.db),before)
        with self.assertRaisesRegex(ValueError,'injected'):
            dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin',
                                                  'payment_id':'P1','credit_id':'C1'}]},
                           fail_before_commit=True)
        self.assertEqual(tables(self.db),before)
        self.assertEqual(self.generation(),generation)
        # Retrying the same batches without the fault lands exactly as a
        # fault-free run would have.
        dataset.import_batch(self.db,{'payments':[payment('P2',['B'])],'credits':[]})
        dataset.decide(self.db,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        clean=str(self.tmp/'clean.db');dataset.init(clean)
        dataset.import_batch(clean,{'payments':[payment('P1',['A','B'])],
                                    'credits':[credit('C1',['A'])]})
        dataset.import_batch(clean,{'payments':[payment('P2',['B'])],'credits':[]})
        dataset.decide(clean,{'decisions':[{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'}]})
        self.assertEqual(tables(self.db),tables(clean))

class Snapshots(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db');dataset.init(self.db)
    def tearDown(self):self.dir.cleanup()

    def test_reports_see_complete_generations(self):
        payments=[payment('P%d'%i,['A','B'],100,'USD','hall') for i in range(8)]
        credits=[credit('C%d'%i,['A','B'],100,'USD','hall') for i in range(8)]
        dataset.import_batch(self.db,{'payments':payments,'credits':credits})
        # Precompute every legal state by replaying the same writes serially on
        # a second database; the observer may only see one of them.
        reference=str(self.tmp/'reference.db');dataset.init(reference)
        dataset.import_batch(reference,{'payments':payments,'credits':credits})
        legal={json.dumps(self.read(reference),sort_keys=True)}
        writes=[]
        for i in range(10):
            if i<8:
                decision={'decision_id':'d%d'%i,'op':'pin','payment_id':'P%d'%i,'credit_id':'C%d'%i}
            else:
                decision={'decision_id':'d%d'%i,'op':'unpin','payment_id':'P%d'%(i-8)}
            writes.append(decision)
            dataset.decide(reference,{'decisions':[decision]})
            legal.add(json.dumps(self.read(reference),sort_keys=True))
        stop=threading.Event();errors=[];seen=[0]
        def observe():
            while not stop.is_set():
                seen[0]+=1
                try:document=self.read()
                except Exception as error:errors.append(repr(error));continue
                key=json.dumps(document,sort_keys=True)
                if key not in legal:errors.append(key)
        observer=threading.Thread(target=observe);observer.start()
        try:
            for decision in writes:dataset.decide(self.db,{'decisions':[decision]})
        finally:
            stop.set();observer.join()
        self.assertEqual(errors,[])
        self.assertGreater(seen[0],0)

    def test_concurrent_reports_between_processes(self):
        dataset.import_batch(self.db,{'payments':[payment('P%d'%i,['A','B']) for i in range(4)],
                                      'credits':[credit('C%d'%i,['A','B']) for i in range(4)]})
        env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
        outputs=[]
        for i in range(4):
            outputs.append(self.tmp/('report-%d.json'%i))
        processes=[subprocess.Popen([sys.executable,'-m','venue_clearing.cli','report',
                                     '--db',self.db,'--output',str(path)],
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
                   for path in outputs]
        for process in processes:
            process.communicate();self.assertEqual(process.returncode,0)
        documents=[json.loads(path.read_text()) for path in outputs]
        before=json.dumps(documents[0],sort_keys=True)
        for document in documents[1:]:self.assertEqual(json.dumps(document,sort_keys=True),before)

    def read(self,db=None):
        out=self.tmp/'snapshot.json';dataset.report(db or self.db,out)
        return json.loads(out.read_text())

if __name__=='__main__':unittest.main()
