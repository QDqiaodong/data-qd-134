"""Dataset lifecycle: init, ingest, recall and report over one SQLite file.

Every writer runs in a single BEGIN IMMEDIATE transaction, so a batch either
commits whole or rolls back whole, and two racing writers are serialized by
the database write lock: a recall and a serve that race are equivalent to one
of the two serial orders, never to an interleaving.  The recall set is never
read outside such a transaction to decide a later write: ingest checks the
quarantined flag on the lot row inside its own write transaction, and recall
sets that flag in the same transaction that records the root.

Genealogy is materialized eagerly: when a blend output is created, its exact
rational composition over the raw lots is computed from the already stored
compositions of its direct inputs (a single dynamic-programming step over the
DAG, never a path enumeration) and stored as decimal-text numerator/denominator
pairs.  Consumption never changes a lot's composition.  Recall therefore
quarantines with one indexed UPDATE, and report is a linear join.

The optional `stats` mapping counts rows read/written at the storage layer so
locality can be verified without a profiler.  `fail_before_commit` raises after
all changes were written but before COMMIT, letting tests prove that a failure
rolls the whole batch back and that a retry then lands cleanly.
"""
import json,os,uuid
from fractions import Fraction
from pathlib import Path
from .common import save_stream
from . import store
from .contract import canonical_payload,parse_init,parse_operations,parse_roots

class DatasetError(ValueError):pass

def _bump(stats,key,count=1):
    if stats is not None:stats[key]=stats.get(key,0)+count

# ---------------------------------------------------------------- init

def init(path,data,stats=None,trace=None):
    """Create a fresh dataset from {"venues":[...]}.

    Refuses to touch a database file that already holds any content: the
    dataset owns the file.  The database is built in a sibling file and
    renamed into place, so a failed init leaves no half-created file behind.
    """
    venues=parse_init(data)
    target=Path(path)
    if target.exists() and target.stat().st_size:
        raise DatasetError('refusing to initialize a nonempty database file: '+str(path))
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
        conn.executemany('INSERT INTO venues(id) VALUES(?)',[(v,) for v in sorted(venues)])
        _bump(stats,'venues_written',len(venues))
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

# ---------------------------------------------------------------- ingest

def _lot_row(conn,lot_id,stats):
    row=conn.execute('SELECT remaining_grams,quarantined FROM lots WHERE lot_id=?',(lot_id,)).fetchone()
    _bump(stats,'lots_read',1)
    return row

def _composition(conn,lot_id,stats):
    rows=conn.execute('SELECT root_id,num,den FROM composition WHERE lot_id=?',(lot_id,)).fetchall()
    _bump(stats,'composition_read',len(rows))
    return {root:Fraction(int(num),int(den)) for root,num,den in rows}

def _create_lot(conn,op_id,lot_id,kind,amount,mix,stats):
    conn.execute('INSERT INTO lots(lot_id,kind,op_id,initial_grams,remaining_grams,quarantined)'
                 ' VALUES(?,?,?,?,?,0)',(lot_id,kind,op_id,amount,amount))
    _bump(stats,'lots_written',1)
    for root,ratio in mix.items():
        if ratio:
            conn.execute('INSERT INTO composition(lot_id,root_id,num,den) VALUES(?,?,?,?)',
                         (lot_id,root,str(ratio.numerator),str(ratio.denominator)))
            _bump(stats,'composition_written',1)

def _consume(conn,op_id,lot_id,amount,stats):
    conn.execute('UPDATE lots SET remaining_grams=remaining_grams-? WHERE lot_id=?',(amount,lot_id))
    conn.execute('INSERT INTO consumption(op_id,lot_id,grams) VALUES(?,?,?)',(op_id,lot_id,amount))
    _bump(stats,'lots_written',1)
    _bump(stats,'consumption_written',1)

def _apply_receive(conn,op,stats):
    _kind,op_id,lot_id,amount=op
    if conn.execute('SELECT 1 FROM lots WHERE lot_id=?',(lot_id,)).fetchone():
        raise DatasetError('lot already exists: '+lot_id)
    _bump(stats,'lots_read',1)
    _create_lot(conn,op_id,lot_id,'raw',amount,{lot_id:Fraction(1)},stats)

def _apply_blend(conn,op,stats):
    _kind,op_id,inputs,outputs=op
    total_in=sum(amount for _lot,amount in inputs)
    total_out=sum(amount for _lot,amount in outputs)
    if total_in!=total_out:raise DatasetError('blend input and output grams differ')
    for lot_id,need in inputs:
        row=_lot_row(conn,lot_id,stats)
        if row is None:raise DatasetError('unknown lot: '+lot_id)
        if row[1]:raise DatasetError('lot is quarantined: '+lot_id)
        if row[0]<need:raise DatasetError('insufficient stock in lot: '+lot_id)
    for lot_id,_amount in outputs:
        if conn.execute('SELECT 1 FROM lots WHERE lot_id=?',(lot_id,)).fetchone():
            raise DatasetError('lot already exists: '+lot_id)
        _bump(stats,'lots_read',1)
    # One DP step over the DAG: the output composition is the gram-weighted
    # mean of the direct inputs' stored compositions.  Branches that diverged
    # and rejoin add their true masses here; no ancestor path is enumerated.
    mix={}
    for lot_id,need in inputs:
        for root,ratio in _composition(conn,lot_id,stats).items():
            mix[root]=mix.get(root,Fraction(0))+ratio*need/total_in
    for lot_id,need in inputs:_consume(conn,op_id,lot_id,need,stats)
    for lot_id,amount in outputs:_create_lot(conn,op_id,lot_id,'blend',amount,mix,stats)

def _apply_serve(conn,op,stats):
    _kind,op_id,lot_id,amount,venue=op
    row=_lot_row(conn,lot_id,stats)
    if row is None:raise DatasetError('unknown lot: '+lot_id)
    if row[1]:raise DatasetError('lot is quarantined: '+lot_id)
    if row[0]<amount:raise DatasetError('insufficient stock in lot: '+lot_id)
    if conn.execute('SELECT 1 FROM venues WHERE id=?',(venue,)).fetchone() is None:
        raise DatasetError('unknown venue: '+venue)
    _bump(stats,'venues_read',1)
    _consume(conn,op_id,lot_id,amount,stats)
    conn.execute('INSERT INTO deliveries(op_id,lot_id,venue,grams) VALUES(?,?,?,?)',
                 (op_id,lot_id,venue,amount))
    _bump(stats,'deliveries_written',1)

def ingest(path,data,stats=None,trace=None,fail_before_commit=False):
    """Apply {"operations":[...]} in array order; the batch commits or rolls
    back as a whole.  Returns {'applied':a,'replayed':r}."""
    ops=parse_operations(data)
    if not ops:return {'applied':0,'replayed':0}
    if not Path(path).is_file():raise DatasetError('dataset is not initialized: '+str(path))
    conn=store.connect(path,trace=trace)
    applied=replayed=0
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        for op in ops:
            op_id=op[1];payload=canonical_payload(op)
            row=conn.execute('SELECT payload FROM ops WHERE op_id=?',(op_id,)).fetchone()
            _bump(stats,'ops_read',1)
            if row is not None:
                # A replay is decided before any stock rule: an accepted op
                # stays ignored on retry even if its lot was recalled since.
                if row[0]==payload:replayed+=1;continue
                raise DatasetError('conflicting replay for op_id: '+op_id)
            if op[0]=='receive':_apply_receive(conn,op,stats)
            elif op[0]=='blend':_apply_blend(conn,op,stats)
            else:_apply_serve(conn,op,stats)
            conn.execute('INSERT INTO ops(op_id,type,payload) VALUES(?,?,?)',(op_id,op[0],payload))
            _bump(stats,'ops_written',1)
            applied+=1
        if fail_before_commit:raise DatasetError('injected failure before commit')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return {'applied':applied,'replayed':replayed}

# ---------------------------------------------------------------- recall

def recall(path,data,stats=None,trace=None,fail_before_commit=False):
    """Atomically add {"roots":[...]} to the permanent recall set and quarantine
    every lot whose composition over any new root is positive."""
    roots=parse_roots(data)
    if not roots:return 0
    if not Path(path).is_file():raise DatasetError('dataset is not initialized: '+str(path))
    conn=store.connect(path,trace=trace)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        for root in roots:
            row=conn.execute('SELECT kind FROM lots WHERE lot_id=?',(root,)).fetchone()
            _bump(stats,'lots_read',1)
            if row is None:raise DatasetError('unknown lot: '+root)
            if row[0]!='raw':raise DatasetError('recall root is not a raw lot: '+root)
        for root in roots:
            cursor=conn.execute('INSERT OR IGNORE INTO recalls(root_id) VALUES(?)',(root,))
            if cursor.rowcount:_bump(stats,'recalls_written',1)
            # composition holds only positive entries, so every lot listed for
            # this root is affected; its whole remaining stock is quarantined.
            quarantined=conn.execute(
                'UPDATE lots SET quarantined=1 WHERE quarantined=0 AND lot_id IN'
                ' (SELECT lot_id FROM composition WHERE root_id=?)',(root,)).rowcount
            _bump(stats,'lots_quarantined',quarantined)
        if fail_before_commit:raise DatasetError('injected failure before commit')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return 0

# ---------------------------------------------------------------- report

def _ratio(value):
    return {'numerator':str(value.numerator),'denominator':str(value.denominator)}

def build_report(conn,stats=None):
    """Assemble {recalled_roots,deliveries,stock} from one read snapshot."""
    roots=[row[0] for row in conn.execute('SELECT root_id FROM recalls ORDER BY root_id')]
    _bump(stats,'recalls_read',len(roots))
    # affected[lot] = sum of the lot's composition ratios over all recalled
    # roots: each root contributes its true mass fraction exactly once, no
    # matter how many paths connect it to the lot.
    affected={}
    for lot_id,num,den in conn.execute(
            'SELECT lot_id,num,den FROM composition WHERE root_id IN (SELECT root_id FROM recalls)'):
        _bump(stats,'composition_read',1)
        affected[lot_id]=affected.get(lot_id,Fraction(0))+Fraction(int(num),int(den))
    deliveries=[]
    for op_id,lot_id,venue,amount in conn.execute(
            'SELECT op_id,lot_id,venue,grams FROM deliveries ORDER BY op_id'):
        _bump(stats,'deliveries_read',1)
        ratio=affected.get(lot_id)
        if ratio:
            deliveries.append({'op_id':op_id,'venue':venue,'lot_id':lot_id,'grams':amount,
                               'affected_grams':_ratio(amount*ratio)})
    stock=[]
    for lot_id,remaining in conn.execute(
            'SELECT lot_id,remaining_grams FROM lots WHERE remaining_grams>0 ORDER BY lot_id'):
        _bump(stats,'lots_read',1)
        ratio=affected.get(lot_id)
        if ratio:
            stock.append({'lot_id':lot_id,'remaining_grams':remaining,
                          'affected_grams':_ratio(remaining*ratio),'quarantined':True})
    return {'recalled_roots':roots,'deliveries':deliveries,'stock':stock}

def report(path,output,stats=None,trace=None):
    """Write the impact report from one read snapshot; atomic replace, the old
    file survives any failure, and the database is only read, never written."""
    conn=store.connect(path,readonly=True,trace=trace)
    try:
        conn.execute('BEGIN')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        document=build_report(conn,stats)
        save_stream(output,lambda stream:json.dump(
            document,stream,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2))
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return document
