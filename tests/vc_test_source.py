"""Source layer acceptance for venue-clearing.

Both arrival orders, alias-order retries, same amount with a different
currency or venue, duplicate intersections collapsing to one edge, and a
conflict at the end of a batch leaving nothing behind -- each case checks the
candidate edges and the original records in the database, not the report.
"""
import json,sqlite3,tempfile,unittest
from pathlib import Path
from venue_clearing import dataset
from tests import vc_test_oracle as oracle

def edges(db):
    conn=sqlite3.connect(db)
    try:return {tuple(row) for row in conn.execute('SELECT payment_id,credit_id FROM edges')}
    finally:conn.close()

def tables(db):
    conn=sqlite3.connect(db)
    try:
        return {name:conn.execute('SELECT * FROM %s ORDER BY 1'%name).fetchall()
                for name in ('meta','payments','credits','payment_alias','credit_reference',
                             'edges','components','comp_vertex','inference','bindings','decisions',
                             'imports')}
    finally:conn.close()

def payment(pid,amount=500,currency='USD',venue='hall',aliases=('R1',)):
    return {'payment_id':pid,'venue':venue,'currency':currency,'amount_minor':amount,
            'aliases':list(aliases)}

def credit(cid,amount=500,currency='USD',venue='hall',references=('R1',)):
    return {'credit_id':cid,'venue':venue,'currency':currency,'amount_minor':amount,
            'references':list(references)}

class Source(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
        self.db=str(self.tmp/'clearing.db')
        dataset.init(self.db)
    def tearDown(self):self.dir.cleanup()

    def test_both_arrival_orders_yield_identical_database(self):
        # Order A: payments first, then credits.
        dataset.import_batch(self.db,{'payments':[payment('P1',aliases=['A','B'])],'credits':[]})
        dataset.import_batch(self.db,{'payments':[],'credits':[credit('C1',references=['B'])]})
        order_a=tables(self.db)
        # Order B: credits first, then payments, in a fresh database.
        other=str(self.tmp/'other.db');dataset.init(other)
        dataset.import_batch(other,{'payments':[],'credits':[credit('C1',references=['B'])]})
        dataset.import_batch(other,{'payments':[payment('P1',aliases=['A','B'])],'credits':[]})
        order_b=tables(other)
        # The per-batch source log records which order the records arrived in;
        # generation counts committed batches.  Everything derived must agree.
        self.assertEqual({k:v for k,v in order_a.items() if k not in ('meta','imports')},
                         {k:v for k,v in order_b.items() if k not in ('meta','imports')})
        self.assertEqual([(row[0],row[1],row[2]+row[3]) for row in order_a['imports']],
                         [(row[0],row[1],row[2]+row[3]) for row in order_b['imports']])
        # And the source layer is exactly what the two batches imported.
        self.assertEqual([row[2:] for row in order_a['imports']],
                         [(1,0,0),(0,1,1)])
        self.assertEqual(edges(other),{('P1','C1')})

    def test_alias_order_is_not_significant_and_retry_is_ignored(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',aliases=['A','B','C'])],'credits':[]})
        before=tables(self.db)
        result=dataset.import_batch(self.db,{'payments':[payment('P1',aliases=['C','A','B'])],'credits':[]})
        self.assertEqual((result['payments'],result['payment_replays']),(0,1))
        after=tables(self.db)
        # Pure replay: no new facts, so the whole database is unchanged.
        self.assertEqual(after,before)
        # A differently ordered references array is the same credit too.
        dataset.import_batch(self.db,{'payments':[],'credits':[credit('C1',references=['Z','Y'])]})
        snapshot=tables(self.db)
        result=dataset.import_batch(self.db,{'payments':[],'credits':[credit('C1',references=['Y','Z'])]})
        self.assertEqual((result['credits'],result['credit_replays']),(0,1))
        self.assertEqual(tables(self.db),snapshot)

    def test_same_amount_but_other_currency_or_venue_is_not_a_candidate(self):
        dataset.import_batch(self.db,{
            'payments':[payment('P-USD',currency='USD'),payment('P-EUR',currency='EUR'),
                        payment('P-ANNEX',venue='annex')],
            'credits':[credit('C-USD',currency='USD'),
                       credit('C-EUR',currency='EUR',references=['R1'])]})
        # Only P-USD/C-USD and P-EUR/C-EUR share venue+currency+amount; the
        # annex payment has references in common but a different venue.
        self.assertEqual(edges(self.db),{('P-USD','C-USD'),('P-EUR','C-EUR')})

    def test_duplicate_intersection_is_one_edge(self):
        dataset.import_batch(self.db,{
            'payments':[payment('P1',aliases=['A','B','C'])],
            'credits':[credit('C1',references=['A','B','C'])]})
        self.assertEqual(edges(self.db),{('P1','C1')})
        conn=sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM edges WHERE payment_id=? AND credit_id=?',
                ('P1','C1')).fetchone()[0],1)
        finally:conn.close()

    def test_candidate_edge_exists_in_either_arrival_order_even_before_report(self):
        dataset.import_batch(self.db,{'payments':[payment('P1')],'credits':[]})
        # A credit arriving later must find the payment through the alias index.
        result=dataset.import_batch(self.db,{'payments':[],'credits':[credit('C1',references=['R1'])]})
        self.assertEqual(result['edges'],1)
        self.assertEqual(edges(self.db),{('P1','C1')})

    def test_batch_conflict_at_the_end_rejects_everything(self):
        dataset.import_batch(self.db,{'payments':[payment('P0')],'credits':[credit('C0')]})
        before=tables(self.db)
        batch={'payments':[payment('P1'),payment('P2'),payment('P1',amount=501)],
               'credits':[credit('C1'),credit('C2')]}
        with self.assertRaisesRegex(ValueError,'conflicting payment_id'):
            dataset.import_batch(self.db,batch)
        self.assertEqual(tables(self.db),before)
        # A conflict against stored state behaves the same way.
        with self.assertRaisesRegex(ValueError,'conflicting replay'):
            dataset.import_batch(self.db,{'payments':[payment('P1')],
                                          'credits':[credit('C0',amount=999)]})
        self.assertEqual(tables(self.db),before)

    def test_records_persist_exactly_as_imported(self):
        record=payment('P1',aliases=['B','A'])
        dataset.import_batch(self.db,{'payments':[record],'credits':[credit('C1',references=['B'])]})
        payments,credits,pins,stored=oracle.load(self.db)
        self.assertEqual(payments['P1'],('hall','USD',500,['A','B']))
        self.assertEqual(credits['C1'],('hall','USD',500,['B']))
        self.assertEqual(stored,{('P1','C1')})
        self.assertEqual(pins,{})

    def test_isolated_vertices_are_classified(self):
        dataset.import_batch(self.db,{'payments':[payment('P1',aliases=['A'])],
                                      'credits':[credit('C1',references=['B'])]})
        documented=_document(self.db)
        self.assertEqual(documented['payments'][0]['status'],'unmatched')
        self.assertEqual(documented['credits'][0]['status'],'unmatched')
        self.assertEqual(documented['maximum_pairs'],0)

def _report(db):
    out=Path(db).with_suffix('.report.json')
    dataset.report(db,out)
    return out.read_text()

def _document(db):
    return json.loads(_report(db))

if __name__=='__main__':unittest.main()
