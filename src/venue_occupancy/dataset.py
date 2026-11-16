"""Dataset lifecycle: init, apply and export over one SQLite file.

Facts and materialized occupancy commit together in a single transaction, so a
crash can never expose facts without their segments, metrics and generation.
apply touches only the ids named in the incoming batch: their stored facts,
their currently effective revisions, and the segment neighbourhoods of the old
and new intervals of the versions that actually change.  Nothing here loads all
facts, all bookings or the horizon timeline; export streams one venue at a time
from a single read snapshot.

The optional `stats` mapping counts rows read/written at the storage layer and
`trace` receives every SQL statement; both exist so locality can be verified
without a profiler and without changing behaviour.  `fail_after_materialize`
raises after the derived tables were written but before commit, which lets the
tests prove that a failure rolls facts, segments, metrics and generation back
together.
"""
import json,os,uuid
from pathlib import Path
from .common import save_stream
from . import materialize, store
from .model import contract
from .revisions import UPSERT,DELETE,parse_batch

class DatasetError(ValueError):pass

def _bump(stats,key,count=1):
    if stats is not None:stats[key]=stats.get(key,0)+count

def publish_result(conn,generation,write,stats=None):
    """Stream {generation,result} from the derived tables of one snapshot."""
    start=int(store.meta(conn,'horizon_start'));end=int(store.meta(conn,'horizon_end'))
    venues=list(conn.execute('SELECT id,capacity FROM venues ORDER BY id'));_bump(stats,'venues_read',len(venues))
    write('{"generation":%d,"result":{"horizon":{"start":%d,"end":%d},"venues":['%(generation,start,end))
    for index,(vid,capacity) in enumerate(venues):
        if index:write(',')
        level=conn.execute('SELECT peak,over_capacity_ticks,excess_unit_ticks,booked_unit_ticks FROM venue_metrics WHERE venue=?',(vid,)).fetchone()
        _bump(stats,'metrics_read',1)
        if level is None:raise DatasetError('missing venue metrics')
        write('{"venue":%s,"capacity":%d,"peak":%d,"over_capacity_ticks":%d,"excess_unit_ticks":%s,"booked_unit_ticks":%s,"segments":['%
              (json.dumps(vid,ensure_ascii=False),capacity,level[0],int(level[1]),int(level[2]),int(level[3])))
        first=True
        for s,e,occ in conn.execute('SELECT start,end,occupancy FROM segments WHERE venue=? ORDER BY start',(vid,)):
            _bump(stats,'segments_read',1)
            if not first:write(',')
            first=False
            excess=occ-capacity
            write('{"start":%d,"end":%d,"occupancy":%d,"excess":%d}'%(s,e,occ,excess if excess>0 else 0))
        write(']}')
    write(']}}')

def _fact_tuple(record):
    """(id,rev,op,venue,start,end,quantity) from a parsed batch record."""
    kind,bid,rev,venue,a,z,q=record
    return (bid,rev,UPSERT if kind=='upsert' else DELETE,venue,a,z,q)

def _insert_fact(conn,fact,stats):
    conn.execute('INSERT INTO facts(id,rev,op,venue,start,end,quantity) VALUES(?,?,?,?,?,?,?)',fact)
    _bump(stats,'facts_written',1)

def _upsert_effective(conn,fact,stats):
    conn.execute('INSERT INTO effective(id,rev,op,venue,start,end,quantity) VALUES(?,?,?,?,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET rev=excluded.rev,op=excluded.op,venue=excluded.venue,start=excluded.start,end=excluded.end,quantity=excluded.quantity',fact)
    _bump(stats,'effective_written',1)

def _select(conn,sql,params,stats,key):
    rows=conn.execute(sql,params).fetchall();_bump(stats,key,len(rows));return rows

def _read_facts(conn,ids,stats):
    found={};values=sorted(ids)
    for offset in range(0,len(values),400):
        chunk=values[offset:offset+400]
        for row in _select(conn,'SELECT id,rev,op,venue,start,end,quantity FROM facts WHERE id IN (%s)'%','.join('?'*len(chunk)),chunk,stats,'facts_read'):
            found[(row[0],row[1])]=tuple(row)
    return found

def _read_effective(conn,ids,stats):
    found={};values=sorted(ids)
    for offset in range(0,len(values),400):
        chunk=values[offset:offset+400]
        for row in _select(conn,'SELECT id,rev,op,venue,start,end,quantity FROM effective WHERE id IN (%s)'%','.join('?'*len(chunk)),chunk,stats,'effective_read'):
            found[row[0]]=tuple(row)
    return found

def _capacities(conn,venues,stats):
    found={};values=sorted(venues)
    for offset in range(0,len(values),400):
        chunk=values[offset:offset+400]
        for row in _select(conn,'SELECT id,capacity FROM venues WHERE id IN (%s)'%','.join('?'*len(chunk)),chunk,stats,'venues_read'):found[row[0]]=row[1]
    return found

def _contribute(bucket,horizon,fact,sign):
    a=max(horizon[0],fact[4]);z=min(horizon[1],fact[5])
    if a<z:bucket.setdefault(fact[3],[]).append((a,z,sign*fact[6]))

def init(path,data,stats=None,trace=None):
    """Create a fresh dataset from the v1 contract JSON.

    Refuses to touch a database file that already holds any table: the dataset
    owns the file, and silently adding tables to somebody else's database would
    be an overwrite the caller did not ask for.
    """
    start,end,venues,bookings=contract(data)
    target=Path(path)
    if target.exists() and target.stat().st_size:raise DatasetError('refusing to initialize a nonempty database file: '+str(path))
    # Build the dataset in a sibling file and rename it into place, so a failed
    # init leaves no half-created database behind (atomic rename also means an
    # existing empty target is replaced rather than corrupted).
    temporary=target.with_name(target.name+'.init-'+uuid.uuid4().hex)
    for suffix in ('','-wal','-shm'):
        if Path(str(temporary)+suffix).exists():raise DatasetError('temporary path already exists')
    def discard():
        for suffix in ('','-wal','-shm'):
            leftover=Path(str(temporary)+suffix)
            if leftover.exists():leftover.unlink()
    conn=store.connect(temporary,trace=trace)
    try:
        conn.execute('BEGIN IMMEDIATE')
        store.init_schema(conn)
        store.set_meta(conn,'horizon_start',start);store.set_meta(conn,'horizon_end',end);store.set_meta(conn,'generation',0)
        conn.executemany('INSERT INTO venues(id,capacity) VALUES(?,?)',sorted(venues.items()));_bump(stats,'venues_written',len(venues))
        for b in bookings:
            fact=(b['id'],0,UPSERT,b['venue'],b['start'],b['end'],b['quantity'])
            _insert_fact(conn,fact,stats);_upsert_effective(conn,fact,stats)
        for vid,capacity in venues.items():materialize.rebuild_venue(conn,vid,capacity,start,end,stats)
        conn.execute('COMMIT')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        conn.close();discard()
        raise
    else:
        conn.close()
        try:os.replace(temporary,target)
        except BaseException:
            discard();raise
    return 0

def apply(path,batch,stats=None,trace=None,fail_after_materialize=False):
    parsed=parse_batch(batch)
    if not parsed:return 0
    if not Path(path).is_file():raise DatasetError('dataset is not initialized: '+str(path))
    conn=store.connect(path,trace=trace)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        start=int(store.meta(conn,'horizon_start'));end=int(store.meta(conn,'horizon_end'))
        ids={record[1] for record in parsed}
        facts=_read_facts(conn,ids,stats);effective=_read_effective(conn,ids,stats)
        # Validate the whole batch before writing anything: a conflicting replay
        # or an unknown venue rejects even revisions that are already superseded.
        for record in parsed:
            fact=_fact_tuple(record);existing=facts.get((fact[0],fact[1]))
            if existing is not None and existing!=fact:raise DatasetError('conflicting replay for id/rev')
        upsert_venues={_fact_tuple(record)[3] for record in parsed if record[0]=='upsert'}
        capacities=_capacities(conn,upsert_venues,stats)
        if len(capacities)!=len(upsert_venues):raise DatasetError('unknown venue')
        winner={}
        for (bid,rev),fact in facts.items():
            if bid not in winner or rev>winner[bid][1]:winner[bid]=fact
        new_facts=[]
        for record in parsed:
            fact=_fact_tuple(record)
            if (fact[0],fact[1]) in facts:continue
            _insert_fact(conn,fact,stats);new_facts.append(fact)
            current=winner.get(fact[0])
            if current is None or fact[1]>current[1]:winner[fact[0]]=fact
        if not new_facts:
            conn.execute('COMMIT');return 0
        effective_prior=dict(effective)
        contributions={};disturbed=set()
        for bid,new in winner.items():
            prior=effective_prior.get(bid)
            if prior is not None and prior[1]==new[1]:continue   # winner unchanged
            if prior is None and new[2]==DELETE:continue          # nothing was effective
            if prior is not None and prior[2]==UPSERT:
                _contribute(contributions,(start,end),prior,-1);disturbed.add(prior[3])
            if new[2]==DELETE:
                conn.execute('DELETE FROM effective WHERE id=?',(bid,));_bump(stats,'effective_written',1)
            else:
                _upsert_effective(conn,new,stats)
                _contribute(contributions,(start,end),new,1);disturbed.add(new[3])
        capacities=_capacities(conn,disturbed,stats)
        if len(capacities)!=len(disturbed):raise DatasetError('unknown venue')
        for vid,intervals in contributions.items():
            materialize.apply_intervals(conn,vid,capacities[vid],start,end,intervals,stats)
        if fail_after_materialize:raise DatasetError('injected failure after materialize')
        store.set_meta(conn,'generation',int(store.meta(conn,'generation',0))+1)
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return 0

def export(path,output,stats=None,trace=None):
    conn=store.connect(path,readonly=True,trace=trace)
    try:
        conn.execute('BEGIN')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        generation=int(store.meta(conn,'generation',0))
        save_stream(output,lambda stream:publish_result(conn,generation,stream.write,stats))
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return 0
