"""``venue-energy audit`` command line.

The legacy venue-occupancy entry point is untouched; this is a separate
console script with its own argument contract.
"""
import argparse,json,sys
from pathlib import Path
from .common import save_stream
from . import engine

DESCRIPTION='Venue energy settlement from meter readings: per-venue UTC-monthly Wh, cost, carbon and completeness'

def build_parser():
    parser=argparse.ArgumentParser(prog='venue-energy',description=DESCRIPTION)
    sub=parser.add_subparsers(dest='command')
    audit_p=sub.add_parser('audit',help='settle meters.json + readings.csv + tariffs.json into one report')
    audit_p.add_argument('--meters',type=Path,required=True,help='meters.json')
    audit_p.add_argument('--readings',type=Path,required=True,help='readings.csv')
    audit_p.add_argument('--tariffs',type=Path,required=True,help='tariffs.json')
    audit_p.add_argument('--output',type=Path,required=True,help='report JSON (atomic replace)')
    audit_p.add_argument('--workdir',type=Path,default=None,help='directory for external sort runs (default: temp dir)')
    audit_p.add_argument('--buffer-bytes',type=int,default=8_000_000)
    audit_p.add_argument('--buffer-records',type=int,default=50_000)
    audit_p.add_argument('--fan-in',type=int,default=8)
    return parser

def main(argv=None):
    args=build_parser().parse_args(argv)
    if args.command!='audit':
        print('usage: venue-energy audit --meters M --readings R --tariffs T --output O',file=sys.stderr)
        return 2
    paths=(args.meters,args.readings,args.tariffs)
    if args.output in paths:
        print('output path must differ from every input path',file=sys.stderr);return 2
    if min(args.buffer_bytes,args.buffer_records,args.fan_in)<=0:
        print('buffer and fan-in settings must be positive',file=sys.stderr);return 2
    if args.workdir is not None:
        args.workdir.mkdir(parents=True,exist_ok=True)
    try:
        report=engine.audit(args.meters,args.readings,args.tariffs,
                            workdir=args.workdir,buffer_bytes=args.buffer_bytes,
                            buffer_records=args.buffer_records,fan_in=args.fan_in)
        save_stream(args.output,lambda stream:json.dump(
            report,stream,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2))
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False))
        return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,UnicodeError) as error:
        print(str(error),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
