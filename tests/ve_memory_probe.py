"""Standalone memory-bound probe for the external-sort pipeline.

Generates ``--rows`` readings (default 5,000,000) in random order with
duplicates, runs the full audit with deliberately small in-memory buffers so
many spill/merge levels are exercised, and prints the peak resident set size
reported by the OS (ru_maxrss).  The test suite invokes this in a subprocess;
it is also runnable directly for manual capacity checks.

Usage:
    python3 tests/ve_memory_probe.py --rows 5000000 --workdir /tmp/ve
"""
import argparse,csv,json,os,random,resource,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from venue_energy import engine          # noqa: E402

EPOCH_MIN=946684800
EPOCH_MAX_EXCLUSIVE=4102444800

def build(workdir,rows_n,venues_n=25):
    """Stream rows through per-meter CSV shards merged in random order.

    The generator must not itself hold all rows: this probe measures the
    engine's footprint, so each shard is written and closed immediately and
    the final readings.csv is produced by an interleaved shuffle of shard
    iterators (one open file per meter, constant memory).
    """
    import heapq
    rng=random.Random(20261116)
    workdir=Path(workdir);workdir.mkdir(parents=True,exist_ok=True)
    meters=[]
    for i in range(venues_n):
        meters.append({'meter_id':'m%03d'%i,'venue':'v%02d'%(i%5),
                       'modulus_wh':rng.choice([100000,1000000]),
                       'max_watts':rng.randint(2000,20000)})
    (workdir/'meters.json').write_text(json.dumps({'meters':meters}))
    tariffs=[]
    for venue in sorted({m['venue'] for m in meters}):
        tariffs.append({'venue':venue,'start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
                        'cost_micro_per_kwh':rng.randint(1000,200000),
                        'carbon_mg_per_kwh':rng.randint(1000,500000)})
    (workdir/'tariffs.json').write_text(json.dumps({'tariffs':tariffs}))
    per_meter=max(1,rows_n//venues_n)
    shard_paths=[];total=0
    for mi,meter in enumerate(meters):
        mod=meter['modulus_wh'];watts=meter['max_watts']
        shard=workdir/('shard-%03d.csv'%mi);shard_paths.append(shard)
        t=EPOCH_MIN+3600;reg=0
        with open(shard,'w',newline='') as f:
            writer=csv.writer(f)
            for j in range(per_meter):
                dt=rng.randint(60,3600);cap=watts*dt//3600
                inc=rng.randint(0,min(cap,mod-1))
                if 3600*(inc+mod)<=watts*dt:inc=max(0,cap-mod)
                reg=(reg+inc)%mod;t=min(t+dt,EPOCH_MAX_EXCLUSIVE-3600)
                writer.writerow((meter['meter_id'],'rd%08d'%total,t,reg));total+=1
                if rng.random()<0.04:writer.writerow((meter['meter_id'],'rd%08d'%(total-1),t,reg))
                elif rng.random()<0.02:writer.writerow((meter['meter_id'],'rd%08d'%(total-1),t,reg))
    # Interleave shards with random draw weights; duplicates stay adjacent-safe.
    handles=[open(p,newline='') for p in shard_paths]
    try:
        iters=[csv.reader(h) for h in handles]
        rows=[next(it) for it in iters]
        weights=[rng.random() for _ in rows]
        with open(workdir/'readings.csv','w',newline='') as out:
            writer=csv.writer(out);writer.writerow(['meter_id','read_id','observed_at','register_wh'])
            while rows:
                mi=min(range(len(rows)),key=lambda i:weights[i])
                writer.writerow(rows[mi])
                try:rows[mi]=next(iters[mi])
                except StopIteration:
                    rows.pop(mi);weights.pop(mi);iters.pop(mi);continue
                weights[mi]=rng.random()
    finally:
        for h in handles:h.close()
        for p in shard_paths:p.unlink()
    return total

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,default=5_000_000)
    parser.add_argument('--workdir',default=None)
    parser.add_argument('--buffer-bytes',type=int,default=8_000_000)
    parser.add_argument('--buffer-records',type=int,default=50_000)
    parser.add_argument('--fan-in',type=int,default=8)
    args=parser.parse_args()
    own=args.workdir is None
    workdir=tempfile.mkdtemp(prefix='ve-mem-') if own else args.workdir
    started=time.time()
    written=build(workdir,args.rows)
    report=engine.audit(Path(workdir)/'meters.json',Path(workdir)/'readings.csv',
                        Path(workdir)/'tariffs.json',workdir=Path(workdir),
                        buffer_bytes=args.buffer_bytes,buffer_records=args.buffer_records,
                        fan_in=args.fan_in)
    peak_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss   # KiB on Linux
    leftover=list(Path(workdir).glob('*.run'))
    print(json.dumps({'rows_written':written,'months':len(report['months']),
                      'peak_rss_mib':round(peak_kb/1024,1),
                      'leftover_run_files':len(leftover),
                      'elapsed_s':round(time.time()-started,1)},sort_keys=True))
    if own:
        for name in os.listdir(workdir):os.unlink(Path(workdir)/name)
        os.rmdir(workdir)

if __name__=='__main__':main()
