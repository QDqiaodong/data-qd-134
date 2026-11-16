"""``venue-recall init|ingest|recall|report`` command line.

A separate console script next to venue-occupancy and venue-energy; the legacy
entry points are untouched.  Success exits 0, any input or computation error
exits 2.
"""
import argparse,json,sqlite3,sys
from pathlib import Path
from .common import read
from . import dataset,store

DESCRIPTION=('Ingredient lot genealogy and recall pipeline: exact rational attribution, '
             'quarantine of affected lots, and impact reports')

def build_parser():
    parser=argparse.ArgumentParser(prog='venue-recall',description=DESCRIPTION)
    sub=parser.add_subparsers(dest='command')
    for name,help_text in (('init','create the dataset from {"venues":[...]}'),
                           ('ingest','apply one {"operations":[...]} batch atomically'),
                           ('recall','add {"roots":[...]} to the permanent recall set')):
        command=sub.add_parser(name,help=help_text)
        command.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
        command.add_argument('--source',type=Path,required=True,help='input JSON (<=8 MB)')
    report_p=sub.add_parser('report',help='write {recalled_roots,deliveries,stock} from one read snapshot')
    report_p.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
    report_p.add_argument('--output',type=Path,required=True,help='report JSON (atomic replace)')
    return parser

def _recalled_roots(path):
    conn=store.connect(path,readonly=True)
    try:return conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0]
    finally:conn.close()

def main(argv=None):
    args=build_parser().parse_args(argv)
    if args.command is None:
        print('usage: venue-recall init|ingest|recall --db D --source J | venue-recall report --db D --output J',
              file=sys.stderr)
        return 2
    try:
        if args.command=='report':
            if args.output.resolve()==args.db.resolve():
                raise ValueError('output path must differ from the database path')
            document=dataset.report(args.db,args.output)
            print(json.dumps(document,sort_keys=True,ensure_ascii=False,allow_nan=False))
            return 0
        data=read(args.source)
        if args.command=='init':
            dataset.init(args.db,data)
            print(json.dumps({'command':'init'},sort_keys=True))
            return 0
        if args.command=='ingest':
            result=dataset.ingest(args.db,data)
            print(json.dumps({'command':'ingest','applied':result['applied'],'replayed':result['replayed']},sort_keys=True))
            return 0
        dataset.recall(args.db,data)
        print(json.dumps({'command':'recall','recalled_roots':_recalled_roots(args.db)},sort_keys=True))
        return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
