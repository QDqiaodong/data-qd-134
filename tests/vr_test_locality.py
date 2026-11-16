"""Locality proof: the same small ingest/recall on a small and a large
database touches the same number of rows — ingest reads only the compositions
of the lots it consumes, recall quarantines only the affected subgraph, and
neither scans the whole dataset."""
import tempfile,unittest
from pathlib import Path
from venue_recall import dataset

def build(db,extra_lots):
    dataset.init(db,{'venues':['hall']})
    operations=[{'op_id':'r-a','type':'receive','lot_id':'a','grams':1000},
                {'op_id':'r-b','type':'receive','lot_id':'b','grams':1000},
                {'op_id':'mix','type':'blend','inputs':[{'lot_id':'a','grams':100},{'lot_id':'b','grams':100}],
                 'outputs':[{'lot_id':'m','grams':200}]}]
    # Unrelated lots with their own compositions, never touched by the local
    # correction below; they only exist to grow the database.
    for i in range(extra_lots):
        operations.append({'op_id':'fill-%06d'%i,'type':'receive','lot_id':'fill-%06d'%i,'grams':10})
        operations.append({'op_id':'fblend-%06d'%i,'type':'blend',
                           'inputs':[{'lot_id':'fill-%06d'%i,'grams':4}],
                           'outputs':[{'lot_id':'fout-%06d'%i,'grams':4}]})
    dataset.ingest(db,{'operations':operations})

CORRECTION=[{'op_id':'use','type':'blend','inputs':[{'lot_id':'m','grams':10}],
             'outputs':[{'lot_id':'m-out','grams':10}]},
            {'op_id':'serve','type':'serve','lot_id':'m-out','grams':3,'venue':'hall'}]

class Locality(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.tmp=Path(self.dir.name)
    def tearDown(self):self.dir.cleanup()

    def test_ingest_and_recall_row_counts_are_size_independent(self):
        small=str(self.tmp/'small.db');large=str(self.tmp/'large.db')
        build(small,0);build(large,400)
        stats_small={};stats_large={}
        dataset.ingest(small,{'operations':CORRECTION},stats=stats_small)
        dataset.ingest(large,{'operations':CORRECTION},stats=stats_large)
        # The blend reads only the composition of its single input lot (two
        # roots); the serve reads no composition at all — identical counts on
        # both databases.
        self.assertEqual(stats_small,stats_large)
        self.assertEqual(stats_small['composition_read'],2)
        recall_small={};recall_large={}
        dataset.recall(small,{'roots':['a']},stats=recall_small)
        dataset.recall(large,{'roots':['a']},stats=recall_large)
        self.assertEqual(recall_small,recall_large)
        # a, m, m-out carry a positive ratio to a: exactly three quarantines,
        # however many unrelated lots the database holds.
        self.assertEqual(recall_small['lots_quarantined'],3)

    def test_ingest_never_scans_whole_tables(self):
        db=str(self.tmp/'trace.db');build(db,50)
        statements=[]
        dataset.ingest(db,{'operations':CORRECTION},trace=statements.append)
        for statement in statements:
            upper=statement.upper()
            if upper.startswith('SELECT'):
                # Every read is a primary-key seek on the named row, never an
                # unqualified scan of lots, composition, ops or deliveries.
                self.assertIn('WHERE',upper,statement)
        dataset.recall(db,{'roots':['a']},trace=statements.append)
        quarantines=[s for s in statements if s.startswith('UPDATE lots SET quarantined')]
        self.assertEqual(len(quarantines),1)
        self.assertIn('root_id=',quarantines[0])   # driven by the composition(root_id) index

if __name__=='__main__':unittest.main()
