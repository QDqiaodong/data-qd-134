"""``venue-clearing init|import|decide|report`` command line.

A separate console script next to venue-occupancy, venue-energy and
venue-recall; the legacy entry points and databases are untouched.  Success
exits 0, any input or computation error exits 2.
"""
import argparse,json,sqlite3,sys
from pathlib import Path
from .common import read
from . import dataset

DESCRIPTION=('Payment and bank-credit clearing pipeline: candidate graph from equal '
             'amounts plus shared aliases, manual pins, and the full maximum-matching '
             'solution classification')

def build_parser():
    parser=argparse.ArgumentParser(prog='venue-clearing',description=DESCRIPTION)
    sub=parser.add_subparsers(dest='command')
    init_p=sub.add_parser('init',help='create an empty dataset')
    init_p.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
    import_p=sub.add_parser('import',help='apply one {"payments":[...],"credits":[...]} batch')
    import_p.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
    import_p.add_argument('--source',type=Path,required=True,help='input JSON (<=8 MB)')
    decide_p=sub.add_parser('decide',help='apply one {"decisions":[...]} batch in order')
    decide_p.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
    decide_p.add_argument('--source',type=Path,required=True,help='input JSON (<=8 MB)')
    report_p=sub.add_parser('report',help='write the classification from one read snapshot')
    report_p.add_argument('--db',type=Path,required=True,help='SQLite dataset file')
    report_p.add_argument('--output',type=Path,required=True,help='report JSON (atomic replace)')
    return parser

def main(argv=None):
    args=build_parser().parse_args(argv)
    if args.command is None:
        print('usage: venue-clearing init --db D | import --db D --source J | '
              'decide --db D --source J | report --db D --output J',file=sys.stderr)
        return 2
    try:
        if args.command=='init':
            dataset.init(args.db)
            print(json.dumps({'command':'init'},sort_keys=True))
            return 0
        if args.command=='report':
            if args.output.resolve()==args.db.resolve():
                raise ValueError('output path must differ from the database path')
            dataset.report(args.db,args.output)
            print(json.dumps({'command':'report'},sort_keys=True))
            return 0
        data=read(args.source)
        if args.command=='import':
            result=dataset.import_batch(args.db,data)
            print(json.dumps(dict(result,command='import'),sort_keys=True))
            return 0
        result=dataset.decide(args.db,data)
        print(json.dumps(dict(result,command='decide'),sort_keys=True))
        return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
