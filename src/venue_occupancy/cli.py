DESCRIPTION='Half-open booking interval sweep and capacity exceedance evidence, with a local revision dataset'
import argparse, json, sqlite3, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from . import dataset

COMMANDS=('init','apply','export')

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def build_parser():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command',nargs='?',choices=COMMANDS,help='dataset command; omit for the v1 one-shot report')
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--db',type=Path)
    return parser

def main(argv=None):
    parser=build_parser();args=parser.parse_args(argv)
    try:
        if args.command is None:
            if args.db is not None or args.source is None or args.output is None:
                raise ValueError('--source and --output are required without a command')
            if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
            report=run(args.source);save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
        if args.db is None:
            raise ValueError('--db is required for '+args.command)
        if args.command=='export':
            if args.output is None:raise ValueError('--output is required for export')
            dataset.export(args.db,args.output)
            print(json.dumps({'generation':_generation(args.db),'output':str(args.output)},ensure_ascii=False,sort_keys=True));return 0
        if args.source is None:raise ValueError('--source is required for '+args.command)
        data,_digest=read(args.source)
        if args.command=='init':
            if args.output is not None:raise ValueError('init does not take --output')
            dataset.init(args.db,data)
            print(json.dumps({'command':'init','generation':0},sort_keys=True));return 0
        if args.output is not None:raise ValueError('apply does not take --output')
        dataset.apply(args.db,data)
        print(json.dumps({'command':'apply','generation':_generation(args.db)},sort_keys=True));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2

def _generation(path):
    from . import store
    conn=store.connect(path,readonly=True)
    try:return int(store.meta(conn,'generation',0))
    finally:conn.close()

if __name__=='__main__':raise SystemExit(main())
