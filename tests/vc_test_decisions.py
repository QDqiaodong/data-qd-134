"""Decision layer acceptance: conflicts, unpin, unpin without a binding,
retrying an old pin after unpin, a legal decision followed by an illegal one
in the same batch, and the persisted log plus current bindings.

Manual bindings are external confirmations: they may lower the raw graph's
maximum number of pairs and are never rejected for being "less likely" than
some inferred pairing.
"""
import json,sqlite3,tempfile,unittest
from pathlib import Path
from venue_clearing import dataset

def payment(pid,aliases):
    return {'payment_id':pid,'venue':'hall','currency':'USD','amount_minor':700,
            'aliases':list(aliases)}

def credit(cid,references):
    return {'credit_id':cid,'venue':'hall','currency':'USD','amount_minor':700,
            'references':list(references)}

def bindings(db):
    conn=sqlite3.connect(db)
    try:return {row[0]:row[1] for row in conn.execute('SELECT payment_id,credit_id FROM bindings')}
    finally:conn.close()

def decision_log(db):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT decision_id,op,payment_id,credit_id FROM decisions'
                            ' ORDER BY decision_id').fetchall()
    finally:conn.close()

def tables(db):
    conn=sqlite3.connect(db)
    try:
        return {name:conn.execute('SELECT * FROM %s ORDER BY 1'%name).fetchall()
                for name in ('meta','payments','credits','payment_alias','credit_reference',
                             'edges','components','comp_vertex','inference','bindings','decisions',
                             'imports')}
    finally:conn.close()

def build(db):
    """Two payments and two credits with a complete 2x2 candidate graph."""
    dataset.init(db)
    dataset.import_batch(db,{'payments':[payment('P1',['A','B']),payment('P2',['A','B'])],
                             'credits':[credit('C1',['A']),credit('C2',['B'])]})

class Decisions(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db')
        build(self.db)
    def tearDown(self):self.dir.cleanup()

    def decide(self,*decisions):
        return dataset.decide(self.db,{'decisions':list(decisions)})

    def test_pin_records_binding_and_log(self):
        result=self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.assertEqual(result['decisions'],1)
        self.assertEqual(bindings(self.db),{'P1':'C1'})
        self.assertEqual(decision_log(self.db),[('d1','pin','P1','C1')])

    def test_occupied_endpoints_reject_the_whole_batch(self):
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        before=tables(self.db)
        cases=[
            [{'decision_id':'d2','op':'pin','payment_id':'P1','credit_id':'C2'}],
            [{'decision_id':'d3','op':'pin','payment_id':'P2','credit_id':'C1'}],
            [{'decision_id':'d4','op':'pin','payment_id':'P1','credit_id':'C1','extra':1}],
            [{'decision_id':'d5','op':'pin','payment_id':'P2','credit_id':'C2'},
             {'decision_id':'d6','op':'pin','payment_id':'P1','credit_id':'C2'}],
        ]
        for decisions in cases:
            with self.assertRaises(ValueError):dataset.decide(self.db,{'decisions':decisions})
            self.assertEqual(tables(self.db),before)
        # A legal re-pin of exactly the same bound pair is accepted.
        result=self.decide({'decision_id':'d7','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.assertEqual(result['decisions'],1)
        self.assertEqual(bindings(self.db),{'P1':'C1'})

    def test_pin_must_be_an_existing_candidate_edge(self):
        before=tables(self.db)
        with self.assertRaisesRegex(ValueError,'candidate edge'):
            self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C9'})
        with self.assertRaisesRegex(ValueError,'candidate edge'):
            self.decide({'decision_id':'d2','op':'pin','payment_id':'P9','credit_id':'C1'})
        self.assertEqual(tables(self.db),before)

    def test_unpin_and_unpin_without_binding(self):
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.decide({'decision_id':'d2','op':'unpin','payment_id':'P1'})
        self.assertEqual(bindings(self.db),{})
        generation_before=json.loads(self.report()['text'])['generation']
        # Unpin with no binding is legal and still recorded, so the generation
        # advances by one (a new decision exists).
        self.decide({'decision_id':'d3','op':'unpin','payment_id':'P1'})
        self.assertEqual(bindings(self.db),{})
        generation_after=json.loads(self.report()['text'])['generation']
        self.assertEqual(generation_after,generation_before+1)
        with self.assertRaisesRegex(ValueError,'unknown payment'):
            self.decide({'decision_id':'d4','op':'unpin','payment_id':'P9'})

    def test_retrying_the_old_pin_after_unpin_does_not_rebind(self):
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.decide({'decision_id':'d2','op':'unpin','payment_id':'P1'})
        before=tables(self.db)
        result=self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.assertEqual(result['decisions'],0)      # pure replay
        self.assertEqual(bindings(self.db),{})
        self.assertEqual(tables(self.db),before)

    def test_a_new_decision_id_can_rebind_after_unpin(self):
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.decide({'decision_id':'d2','op':'unpin','payment_id':'P1'})
        self.decide({'decision_id':'d3','op':'pin','payment_id':'P1','credit_id':'C2'})
        self.assertEqual(bindings(self.db),{'P1':'C2'})

    def test_conflicting_replay_of_a_decision_id(self):
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        before=tables(self.db)
        for decisions in (
            [{'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C2'}],
            [{'decision_id':'d1','op':'unpin','payment_id':'P1'}],
        ):
            with self.assertRaisesRegex(ValueError,'conflicting replay'):
                dataset.decide(self.db,{'decisions':decisions})
            self.assertEqual(tables(self.db),before)

    def test_duplicate_decision_id_in_one_batch(self):
        # Same id, same payload: the later copy is ignored.
        result=self.decide({'decision_id':'d1','op':'unpin','payment_id':'P1'},
                           {'decision_id':'d1','op':'unpin','payment_id':'P1'})
        self.assertEqual(result['decisions'],1)
        self.assertEqual(decision_log(self.db),[('d1','unpin','P1',None)])
        # Same id, different payload: the batch is rejected whole.
        before=tables(self.db)
        with self.assertRaisesRegex(ValueError,'conflicting decision_id'):
            self.decide({'decision_id':'d2','op':'unpin','payment_id':'P1'},
                        {'decision_id':'d2','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.assertEqual(tables(self.db),before)

    def test_legal_decision_followed_by_illegal_one_rolls_back(self):
        before=tables(self.db)
        with self.assertRaises(ValueError):
            self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'},
                        {'decision_id':'d2','op':'pin','payment_id':'P1','credit_id':'C2'})
        self.assertEqual(tables(self.db),before)
        self.assertEqual(decision_log(self.db),[])
        # The same valid decision alone still works afterwards.
        self.decide({'decision_id':'d1','op':'pin','payment_id':'P1','credit_id':'C1'})
        self.assertEqual(bindings(self.db),{'P1':'C1'})

    def test_replay_only_batch_does_not_advance_the_generation(self):
        self.decide({'decision_id':'d1','op':'unpin','payment_id':'P1'})
        generation=json.loads(self.report()['text'])['generation']
        result=self.decide({'decision_id':'d1','op':'unpin','payment_id':'P1'})
        self.assertEqual(result['decisions'],0)
        self.assertEqual(json.loads(self.report()['text'])['generation'],generation)

    def report(self):
        out=self.tmp/'report.json'
        dataset.report(self.db,out)
        return {'text':out.read_text(),'document':json.loads(out.read_text())}

    def test_decision_batch_limit(self):
        decisions=[{'decision_id':'d%05d'%i,'op':'unpin','payment_id':'P1'}
                   for i in range(2001)]
        before=tables(self.db)
        with self.assertRaises(ValueError):
            dataset.decide(self.db,{'decisions':decisions})
        self.assertEqual(tables(self.db),before)

if __name__=='__main__':unittest.main()
