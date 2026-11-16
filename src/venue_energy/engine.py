"""Settlement engine: unique register interpretation, slicing, aggregation.

The engine never estimates energy from bookings, capacity or anything other
than meter register readings.  Two adjacent readings define a half-open
interval; its consumption is accepted only when the register wrap-around
equation has exactly one feasible solution under the meter's power limit.

All time arithmetic is integer-second UTC; month boundaries are UTC calendar
months.  Money is micro currency units, emissions are milligrams of CO2;
both keep exact fractional products (denominator 3.6e6) until the monthly
round.
"""
import bisect,calendar,datetime,os,tempfile
from . import external
from .contract import EPOCH_MIN,EPOCH_MAX_EXCLUSIVE

SCHEMA_VERSION=1
RATE_DENOMINATOR=3_600_000   # 3600 seconds * 1000 Wh per kWh

# Sort keys over (meter_idx, observed_at, register_wh, read_id).
KEY_METER=lambda r:(r[0],r[1],r[2],r[3])
KEY_READID=lambda r:(r[3],r[0],r[1],r[2])

def interpret(modulus_wh,max_watts,r0,r1,dt):
    """Return the unique feasible E in Wh, or None when the interval is ambiguous.

    E = R1 - R0 + k*modulus for a non-negative integer k.  A candidate is
    feasible iff 3600*E <= max_watts*dt; non-negativity gives a lower bound
    and the power bound gives an upper bound.  Zero wraps are never assumed:
    anything other than exactly one feasible integer k rejects the interval.
    """
    if dt<=0:return None
    base=r1-r0
    k_min=0 if base>=0 else (-base+modulus_wh-1)//modulus_wh
    # 3600*(base + k*modulus) <= max_watts*dt
    bound=max_watts*dt-3600*base
    if bound<0:return None
    k_max=bound//(3600*modulus_wh)
    if k_min==k_max:return base+k_min*modulus_wh
    return None

def year_month(epoch):
    return datetime.datetime.fromtimestamp(epoch,datetime.timezone.utc).timetuple()[:2]

def month_start_epoch(year,month):
    return calendar.timegm((year,month,1,0,0,0,0,0,0))

def month_end_epoch(year,month):
    if month==12:return month_start_epoch(year+1,1)
    return month_start_epoch(year,month+1)

class TariffBook:
    """Half-open, non-overlapping tariff ranges per venue."""

    def __init__(self,tariffs):
        self._starts={};self._ends={};self._cost={};self._carbon={}
        for venue,ranges in tariffs.items():
            self._starts[venue]=[r[0] for r in ranges]
            self._ends[venue]=[r[1] for r in ranges]
            self._cost[venue]=[r[2] for r in ranges]
            self._carbon[venue]=[r[3] for r in ranges]

    def _index(self,venue,t):
        starts=self._starts[venue]
        idx=bisect.bisect_right(starts,t)-1
        if idx<0 or t>=self._ends[venue][idx]:
            raise ValueError('no tariff covers venue %r at epoch %d'%(venue,t))
        return idx

    def covers(self,venue,start,end):
        """Raise if any part of [start,end) lacks a tariff."""
        cursor=start
        while cursor<end:
            idx=self._index(venue,cursor)
            cursor=self._ends[venue][idx]

    def rate(self,venue,t):
        idx=self._index(venue,t)
        return self._cost[venue][idx],self._carbon[venue][idx]

def allocate(energy_wh,segments):
    """Allocate integer Wh over time-ordered segments.

    Each segment receives floor(E*dur/total); the remaining Wh are handed out
    one at a time in chronological segment order, so the pieces always sum
    exactly to E even when there are more pieces than Wh.
    """
    total=sum(duration for _,duration in segments)
    if total<=0 or energy_wh<=0:return [0]*len(segments)
    # floor(E * dur_i / total) per piece, then hand the leftover Wh out one
    # at a time in chronological (segment index) order, exactly as specified.
    pieces=[energy_wh*duration//total for _,duration in segments]
    remainder=energy_wh-sum(pieces)
    for i in range(remainder):
        pieces[i%len(pieces)]+=1
    return pieces

def slice_interval(venue,t0,t1,energy_wh,book):
    """Yield (year, month, duration_seconds, cost_rate, carbon_rate, wh) pieces.

    Boundaries are UTC month starts plus tariff starts/ends inside [t0,t1).
    """
    year,month=year_month(t0)
    boundaries={t0,t1}
    cursor=t0
    while cursor<t1:
        cursor=min(month_end_epoch(year,month),t1)
        boundaries.add(cursor)
        if cursor<t1:
            year,month=year_month(cursor)
    starts=book._starts.get(venue,[])
    for pos in range(bisect.bisect_right(starts,t0),bisect.bisect_left(starts,t1)):
        boundaries.add(starts[pos])
    for end in book._ends.get(venue,[]):
        if t0<end<t1:boundaries.add(end)
    cuts=sorted(boundaries)
    segments=[(a,b-a) for a,b in zip(cuts,cuts[1:]) if b>a]
    pieces=allocate(energy_wh,segments)
    for (start,duration),wh in zip(segments,pieces):
        cost_rate,carbon_rate=book.rate(venue,start)
        ym=year_month(start)
        yield ym[0],ym[1],duration,cost_rate,carbon_rate,wh

class MonthlyAggregator:
    """Exact per (venue, UTC month) accumulator."""

    def __init__(self):
        self.rows={}

    def _key(self,venue,year,month):
        return (venue,year,month)

    def add_slice(self,venue,year,month,duration,cost_rate,carbon_rate,wh):
        key=self._key(venue,year,month)
        row=self.rows.setdefault(key,{'wh':0,'cost_num':0,'carbon_num':0,'complete':True})
        row['wh']+=wh
        # wh/1000 kWh * rate * duration/3600 h, kept as an exact fraction.
        row['cost_num']+=wh*cost_rate*duration
        row['carbon_num']+=wh*carbon_rate*duration

    def mark_ambiguous(self,venue,t0,t1):
        """Every UTC month touched by an excluded interval becomes incomplete."""
        year,month=year_month(t0)
        while True:
            key=self._key(venue,year,month)
            row=self.rows.setdefault(key,{'wh':0,'cost_num':0,'carbon_num':0,'complete':True})
            row['complete']=False
            end=month_end_epoch(year,month)
            if end>=t1:break
            year,month=year_month(end)

    def result(self):
        out=[]
        for (venue,year,month),row in sorted(self.rows.items()):
            cost_num=row['cost_num'];carbon_num=row['carbon_num']
            # Half-up rounding of the exact monthly product.
            cost=(2*cost_num+RATE_DENOMINATOR)//(2*RATE_DENOMINATOR)
            carbon=(2*carbon_num+RATE_DENOMINATOR)//(2*RATE_DENOMINATOR)
            out.append({'venue':venue,'year':year,'month':'%02d'%month,
                        'wh':row['wh'],'cost_micro':cost,'carbon_mg':carbon,
                        'complete':row['complete']})
        return out

def _dedupe_read_ids(stream):
    """Raise on same read_id with different parsed fields; collapse exact dupes."""
    current_id=None;first=None
    for record in stream:
        if record[3]!=current_id:
            current_id=record[3];first=record
            continue
        if record[:3]!=first[:3]:
            raise ValueError('conflicting readings share read_id: '+current_id)
        # Identical parsed fields: duplicate, ignored.

def _scan_meters(stream,meters,order,book,aggregator):
    """Chronological per-meter scan over globally meter-sorted records."""
    current=None            # meter_idx of the meter being scanned
    previous=None           # (observed_at, register_wh) of the kept reading
    for meter_idx,observed_at,register_wh,read_id in stream:
        venue,modulus,max_watts=meters[order[meter_idx]]
        if meter_idx!=current:
            current=meter_idx;previous=(observed_at,register_wh);continue
        prior_t,prior_r=previous
        if observed_at==prior_t:
            if register_wh==prior_r:
                continue                        # duplicate (meter,time,register)
            raise ValueError('different readings for one meter at one timestamp: '+order[meter_idx])
        energy=interpret(modulus,max_watts,prior_r,register_wh,observed_at-prior_t)
        if energy is None:
            book.covers(venue,prior_t,observed_at)
            aggregator.mark_ambiguous(venue,prior_t,observed_at)
        else:
            book.covers(venue,prior_t,observed_at)
            for year,month,duration,cost_rate,carbon_rate,wh in slice_interval(
                    venue,prior_t,observed_at,energy,book):
                aggregator.add_slice(venue,year,month,duration,cost_rate,carbon_rate,wh)
        previous=(observed_at,register_wh)

def audit(meters_path,readings_path,tariffs_path,workdir=None,
          buffer_bytes=8_000_000,buffer_records=50_000,fan_in=8,keep_workdir=False):
    """Run the full pipeline and return the report dictionary."""
    from .contract import load_meters,load_tariffs,iter_reading_rows
    order,by_id=load_meters(meters_path)
    index_of={meter_id:i for i,meter_id in enumerate(order)}
    venues={by_id[meter_id][0] for meter_id in order}
    tariffs=load_tariffs(tariffs_path,venues)
    for venue in venues:
        if venue not in tariffs:
            raise ValueError('no tariff defined for venue: '+venue)
    book=TariffBook(tariffs)
    own_dir=workdir is None
    if own_dir:
        workdir=tempfile.mkdtemp(prefix='venue-energy-')
    meter_spill=external.Spiller(workdir,'meter-',buffer_bytes,buffer_records,KEY_METER)
    readid_spill=external.Spiller(workdir,'readid-',buffer_bytes//2,buffer_records//2,KEY_READID)
    try:
        count=0
        for meter_id,read_id,observed_at,register_wh in iter_reading_rows(readings_path):
            if meter_id not in index_of:raise ValueError('reading references unknown meter_id: '+meter_id)
            if not EPOCH_MIN<=observed_at<EPOCH_MAX_EXCLUSIVE:
                raise ValueError('observed_at outside 2000..2100 UTC epoch range')
            venue,modulus,max_watts=by_id[meter_id]
            if not 0<=register_wh<modulus:
                raise ValueError('register_wh outside [0, modulus_wh) for meter '+meter_id)
            record=(index_of[meter_id],observed_at,register_wh,read_id)
            meter_spill.add(record);readid_spill.add(record);count+=1
        meter_spill.flush();readid_spill.flush()
        # Phase 1: read-id replay/conflict detection, streaming from disk.
        readid_runs=external.reduce_runs(readid_spill.runs,workdir,KEY_READID,fan_in,'rid-merge-')
        try:
            _dedupe_read_ids(external.merged_stream(readid_runs,KEY_READID))
        finally:
            for path in readid_runs:
                if path not in readid_spill.runs and os.path.exists(path):os.unlink(path)
        # Phase 2: chronological per-meter interpretation and settlement.
        meter_runs=external.reduce_runs(meter_spill.runs,workdir,KEY_METER,fan_in,'m-merge-')
        aggregator=MonthlyAggregator()
        _scan_meters(external.merged_stream(meter_runs,KEY_METER),by_id,order,book,aggregator)
    finally:
        if not keep_workdir:
            # Every file the pipeline creates uses the .run suffix, regardless
            # of spill or merge level, so cleanup is independent of the merge
            # fan-out actually chosen.
            for name in os.listdir(workdir):
                if name.endswith('.run'):os.unlink(os.path.join(workdir,name))
            meter_spill.runs=[];readid_spill.runs=[]
            if own_dir:os.rmdir(workdir)
    from .common import sha256_file
    return {'schema_version':SCHEMA_VERSION,
            'input_sha256':{'meters.json':sha256_file(meters_path),
                            'readings.csv':sha256_file(readings_path),
                            'tariffs.json':sha256_file(tariffs_path)},
            'reading_rows':count,
            'months':aggregator.result()}
