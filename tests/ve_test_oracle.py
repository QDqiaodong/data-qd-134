"""Independent full oracle: every report field is recomputed from scratch.

The oracle deliberately re-implements the rules without importing any
pipeline sorting/slicing code: raw lists, explicit k enumeration and a
Fraction-based allocator.  Engine buffers are forced tiny so every case also
exercises multi-level external merges.
"""
import csv,json,random,tempfile,unittest
from fractions import Fraction
from pathlib import Path
from venue_energy import engine
from venue_energy.contract import EPOCH_MIN,EPOCH_MAX_EXCLUSIVE

YEAR0=946684800
YEAR1=978307200             # 2001-01-01
DEN=3_600_000

def epoch_month_bounds(t):
    import calendar,datetime
    y,m=datetime.datetime.fromtimestamp(t,datetime.timezone.utc).timetuple()[:2]
    start=calendar.timegm((y,m,1,0,0,0,0,0,0))
    end=calendar.timegm((y+1,1,1,0,0,0,0,0,0)) if m==12 else calendar.timegm((y,m+1,1,0,0,0,0,0,0))
    return y,m,start,end

class Oracle:
    def __init__(self,meters,tariffs):
        self.meters={m['meter_id']:m for m in meters}
        grouped={}
        for r in tariffs:
            grouped.setdefault(r['venue'],[]).append(
                (r['start'],r['end'],r['cost_micro_per_kwh'],r['carbon_mg_per_kwh']))
        self.tariffs={v:sorted(rs) for v,rs in grouped.items()}

    def rate_at(self,venue,t):
        for s,e,c,g in self.tariffs[venue]:
            if s<=t<e:return c,g
        raise AssertionError('oracle tariff gap')

    def interpret(self,meter,r0,r1,dt):
        mod=self.meters[meter]['modulus_wh'];w=self.meters[meter]['max_watts']
        feasible=[]
        k=0
        while True:
            e=r1-r0+k*mod
            if e<0:
                k+=1;continue
            if 3600*e>w*dt:break
            feasible.append(e);k+=1
            if k>10000:raise AssertionError('enumeration cap')
        return feasible[0] if len(feasible)==1 else None

    def allocate(self,e,durations):
        total=sum(durations)
        base=[e*d//total for d in durations]
        rem=e-sum(base)
        # Spec: leftover Wh are handed out one at a time in time order.
        for i in range(rem):base[i%len(base)]+=1
        return base

    def run(self,rows):
        # read_id replay/conflict semantics
        by_read={}
        for row in rows:
            key=row['read_id'];sig=(row['meter_id'],row['observed_at'],row['register_wh'])
            if key in by_read and by_read[key]!=sig:return 'conflict'
            by_read[key]=sig
        per_meter={}
        for row in rows:per_meter.setdefault(row['meter_id'],[]).append(
            (row['observed_at'],row['register_wh'],row['read_id']))
        months={}
        def cell(venue,y,m):
            return months.setdefault((venue,y,m),{'wh':0,'cost':Fraction(0),'carbon':Fraction(0),'complete':True})
        for meter,seq in per_meter.items():
            venue=self.meters[meter]['venue']
            seq=sorted(seq)
            dedup=[]
            for item in seq:
                if dedup and dedup[-1][0]==item[0]:
                    if dedup[-1][1]!=item[1]:return 'timestamp-conflict'
                    continue
                dedup.append(item)
            for (t0,r0,_),(t1,r1,_) in zip(dedup,dedup[1:]):
                dt=t1-t0
                e=self.interpret(meter,r0,r1,dt)
                touched=[]
                cursor=t0
                while cursor<t1:
                    y,m,ms,me=epoch_month_bounds(cursor);touched.append((y,m,ms,me))
                    cursor=me
                if e is None:
                    for y,m,_,_ in touched:cell(venue,y,m)['complete']=False
                    continue
                cuts={t0,t1}
                for _,_,ms,me in touched:
                    if t0<ms<t1:cuts.add(ms)
                    if t0<me<t1:cuts.add(me)
                for s,te,_,_ in self.tariffs[venue]:
                    if t0<s<t1:cuts.add(s)
                    if t0<te<t1:cuts.add(te)
                cuts=sorted(cuts)
                durations=[b-a for a,b in zip(cuts,cuts[1:])]
                pieces=self.allocate(e,durations)
                for a,d,wh in zip(cuts[:-1],durations,pieces):
                    y,m,_,_=epoch_month_bounds(a);c,g=self.rate_at(venue,a)
                    row=cell(venue,y,m);row['wh']+=wh
                    row['cost']+=Fraction(wh*c*d,DEN);row['carbon']+=Fraction(wh*g*d,DEN)
        out=[]
        for (venue,y,m),row in sorted(months.items()):
            out.append({'venue':venue,'year':y,'month':'%02d'%m,'wh':row['wh'],
                        'cost_micro':(row['cost'].numerator*2+row['cost'].denominator)//(2*row['cost'].denominator),
                        'carbon_mg':(row['carbon'].numerator*2+row['carbon'].denominator)//(2*row['carbon'].denominator),
                        'complete':row['complete']})
        return out

def build_case(seed,meters_n=3,rows_n=400):
    rng=random.Random(seed)
    meters=[]
    for i in range(meters_n):
        meters.append({'meter_id':'m%02d'%i,'venue':'v%02d'%(i%2),
                       'modulus_wh':rng.choice([1000,5000,10000]),
                       'max_watts':rng.randint(50,500)})
    tariffs=[]
    for venue in sorted({m['venue'] for m in meters}):
        cuts=sorted([YEAR0+rng.randrange(YEAR1-YEAR0) for _ in range(4)])
        bounds=[EPOCH_MIN]+cuts+[EPOCH_MAX_EXCLUSIVE]
        for s,e in zip(bounds,bounds[1:]):
            if e<=s:continue
            tariffs.append({'venue':venue,'start':s,'end':e,
                            'cost_micro_per_kwh':rng.randint(0,200000),
                            'carbon_mg_per_kwh':rng.randint(0,500000)})
    rows=[];rid=0
    for meter in meters:
        mod=meter['modulus_wh'];w=meter['max_watts']
        t=YEAR0+rng.randrange(86400);reg=rng.randrange(mod)
        while len(rows)<rows_n and t<YEAR1-7200:
            dt=rng.randint(60,6000)
            if rng.random()<0.15:
                # Force ambiguity: long enough for at least one extra wrap.
                dt=max(dt,3600*mod//max(w,1)+rng.randint(1,3600))
                nxt=(reg+rng.randrange(mod))%mod
            else:
                cap=w*dt//3600
                inc=rng.randint(0,min(cap,mod-1)) if cap else 0
                # guard: k=1 must be infeasible so the case stays unique
                if 3600*(inc+mod)<=w*dt:inc=max(0,cap-mod)
                nxt=(reg+inc)%mod
            t+=dt
            if t>=EPOCH_MAX_EXCLUSIVE:break
            row={'meter_id':meter['meter_id'],'read_id':'rd%07d'%rid,
                 'observed_at':t,'register_wh':nxt}
            rows.append(row);rid+=1
            if rng.random()<0.1:rows.append(dict(row))           # exact duplicate
            if rng.random()<0.05:rows.append(dict(row))          # same (meter,time) again
            reg=nxt
    rng.shuffle(rows)
    return meters,tariffs,rows

def write_inputs(tmp,meters,tariffs,rows):
    (tmp/'meters.json').write_text(json.dumps({'meters':meters}))
    (tmp/'tariffs.json').write_text(json.dumps({'tariffs':tariffs}))
    with open(tmp/'readings.csv','w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['meter_id','read_id','observed_at','register_wh'])
        w.writeheader();w.writerows(rows)

class OracleTests(unittest.TestCase):
    def _compare(self,seed):
        meters,tariffs,rows=build_case(seed)
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);write_inputs(tmp,meters,tariffs,rows)
            report=engine.audit(tmp/'meters.json',tmp/'readings.csv',tmp/'tariffs.json',
                                workdir=tmp,buffer_bytes=9000,buffer_records=120,fan_in=4)
            actual={(r['venue'],r['year'],r['month']):r for r in report['months']}
            expected=Oracle(meters,tariffs).run(rows)
            self.assertIsInstance(expected,list)
            expected_map={(r['venue'],r['year'],r['month']):r for r in expected}
            self.assertEqual(set(actual),set(expected_map))
            for key,exp in expected_map.items():
                got=dict(actual[key]);got.pop('complete',None)
                self.assertEqual(got,{k:v for k,v in exp.items() if k!='complete'},key)
                self.assertEqual(actual[key]['complete'],exp['complete'],key)
            self.assertEqual(report['reading_rows'],len(rows))
            self.assertFalse(list(tmp.glob('*.run')))        # workdir cleaned

    def test_oracle_matches_twenty_random_cases(self):
        for seed in range(20):
            with self.subTest(seed=seed):self._compare(seed)

    def test_ambiguous_interval_excludes_energy_and_flags_month(self):
        # One unique 100 Wh interval, then an ambiguous one in the same month.
        meters=[{'meter_id':'m','venue':'v','modulus_wh':1000,'max_watts':100}]
        tariffs=[{'venue':'v','start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
                  'cost_micro_per_kwh':100000,'carbon_mg_per_kwh':400000}]
        t=YEAR0
        rows=[{'meter_id':'m','read_id':'a','observed_at':t,'register_wh':0},
              {'meter_id':'m','read_id':'b','observed_at':t+3600,'register_wh':100},
              # 10 hours at 100 W allows 0..1000 Wh: several wraps feasible.
              {'meter_id':'m','read_id':'c','observed_at':t+39600,'register_wh':100}]
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);write_inputs(tmp,meters,tariffs,rows)
            report=engine.audit(tmp/'meters.json',tmp/'readings.csv',tmp/'tariffs.json',workdir=tmp)
            month=report['months'][0]
            self.assertFalse(month['complete'])
            self.assertEqual(month['wh'],100)                     # ambiguous Wh excluded
            self.assertEqual(month['cost_micro'],10000)
            self.assertEqual(month['carbon_mg'],40000)

    def test_ambiguous_interval_flags_every_crossing_month(self):
        import calendar
        t0=calendar.timegm((2000,1,31,23,0,0,0,0,0))
        t1=calendar.timegm((2000,2,1,2,0,0,0,0,0))
        meters=[{'meter_id':'m','venue':'v','modulus_wh':100,'max_watts':1000}]
        tariffs=[{'venue':'v','start':EPOCH_MIN,'end':EPOCH_MAX_EXCLUSIVE,
                  'cost_micro_per_kwh':1,'carbon_mg_per_kwh':1}]
        rows=[{'meter_id':'m','read_id':'a','observed_at':t0,'register_wh':10},
              {'meter_id':'m','read_id':'b','observed_at':t1,'register_wh':10}]
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);write_inputs(tmp,meters,tariffs,rows)
            report=engine.audit(tmp/'meters.json',tmp/'readings.csv',tmp/'tariffs.json',workdir=tmp)
            by_month={(r['year'],r['month']):r for r in report['months']}
            self.assertEqual(set(by_month),{(2000,'01'),(2000,'02')})
            self.assertTrue(all(not r['complete'] for r in by_month.values()))
            self.assertTrue(all(r['wh']==0 for r in by_month.values()))

    def test_conflicting_read_id_rejected(self):
        meters,tariffs,rows=build_case(1)
        rows[1]=dict(rows[0]);rows[1]['register_wh']=(rows[1]['register_wh']+1)%1000
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);write_inputs(tmp,meters,tariffs,rows)
            with self.assertRaises(ValueError):
                engine.audit(tmp/'meters.json',tmp/'readings.csv',tmp/'tariffs.json',workdir=tmp)

if __name__=='__main__':unittest.main()
