"""Dataset lifecycle: init, import, decide and report over one SQLite file.

The persisted state is three layers computed together:

* facts: payments, credits, their inverted alias/reference relations and the
  candidate edges derived from ``venue/currency/amount_minor equality plus a
  nonempty alias/reference intersection``;
* manual bindings and the append-only decision log;
* the derived component table, one row per *raw* candidate connected
  component, holding its vertex/edge counts and the maximum matching size left
  after pinned endpoints are removed, plus the classification of every vertex.

All three layers move in one ``BEGIN IMMEDIATE`` transaction.  A pin or unpin
reclassifies every vertex of the affected raw component (not just the two
binding rows); an import that bridges two old components merges them and
reclassifies the merged component, so stale inference is never left behind.
Raw edges are never deleted by bindings.

Locality: import touches only the batch's own ids, the inverted indexes for the
batch's aliases/references, the components of the endpoints it actually
connects, and those components' vertices/edges.  decide touches only the
decision endpoints' components.  Neither reads unrelated components, and the
running ``maximum_pairs`` total lives in ``meta`` so no global aggregate is
needed.  report streams the inference table.
"""
import json,os,uuid
from pathlib import Path
from .common import save_stream
from .contract import (canonical_credit,canonical_decision,canonical_payment,
                       parse_decisions,parse_import)
from .graph import classify
from . import store

MAX_COMPONENT_VERTICES=4000
MAX_COMPONENT_EDGES=20000

class DatasetError(ValueError):pass

def _bump(stats,key,count=1):
    if stats is not None:stats[key]=stats.get(key,0)+count

def _placeholders(n):
    return ','.join('?'*n)

def _generation(conn):
    return int(store.meta(conn,'generation','0'))

# ---------------------------------------------------------------- init

def init(path,trace=None):
    """Create an empty dataset; refuse to touch a nonempty file.

    The database is built in a sibling file and renamed into place, so a
    failed init leaves no half-created file behind.
    """
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

# ---------------------------------------------------------------- import

def _insert_payments(conn,records,new_payments,stats):
    seen={};replay=0
    for record in records:
        payment_id=record[0];payload=canonical_payment(record)
        if payment_id in seen:
            if seen[payment_id]!=payload:
                raise DatasetError('conflicting payment_id in batch: '+payment_id)
            replay+=1;continue
        seen[payment_id]=payload
        row=conn.execute('SELECT payload FROM payments WHERE payment_id=?',(payment_id,)).fetchone()
        _bump(stats,'payments_read',1)
        if row is not None:
            if row[0]!=payload:
                raise DatasetError('conflicting replay for payment_id: '+payment_id)
            replay+=1;continue
        conn.execute('INSERT INTO payments(payment_id,venue,currency,amount_minor,payload)'
                     ' VALUES(?,?,?,?,?)',(record[0],record[1],record[2],record[3],payload))
        conn.executemany('INSERT INTO payment_alias(alias,payment_id) VALUES(?,?)',
                         [(alias,payment_id) for alias in record[4]])
        _bump(stats,'payments_written',1)
        _bump(stats,'aliases_written',len(record[4]))
        new_payments.append(record)
    return replay

def _insert_credits(conn,records,new_credits,stats):
    seen={};replay=0
    for record in records:
        credit_id=record[0];payload=canonical_credit(record)
        if credit_id in seen:
            if seen[credit_id]!=payload:
                raise DatasetError('conflicting credit_id in batch: '+credit_id)
            replay+=1;continue
        seen[credit_id]=payload
        row=conn.execute('SELECT payload FROM credits WHERE credit_id=?',(credit_id,)).fetchone()
        _bump(stats,'credits_read',1)
        if row is not None:
            if row[0]!=payload:
                raise DatasetError('conflicting replay for credit_id: '+credit_id)
            replay+=1;continue
        conn.execute('INSERT INTO credits(credit_id,venue,currency,amount_minor,payload)'
                     ' VALUES(?,?,?,?,?)',(record[0],record[1],record[2],record[3],payload))
        conn.executemany('INSERT INTO credit_reference(reference,credit_id) VALUES(?,?)',
                         [(reference,credit_id) for reference in record[4]])
        _bump(stats,'credits_written',1)
        _bump(stats,'references_written',len(record[4]))
        new_credits.append(record)
    return replay

def _candidate_edges(conn,new_payments,new_credits,stats):
    """Candidate edges introduced by this batch, found through the inverted
    indexes only.  Candidate-ness is a pure function of the fields of the two
    records, so records that already existed can never gain a new edge."""
    pairs=set()
    for record in new_payments:
        payment_id,venue,currency,amount,aliases=record
        rows=conn.execute(
            'SELECT DISTINCT cr.credit_id FROM credit_reference cr'
            ' JOIN credits c ON c.credit_id=cr.credit_id'
            ' WHERE cr.reference IN (%s) AND c.venue=? AND c.currency=? AND c.amount_minor=?'
            %_placeholders(len(aliases)),(*aliases,venue,currency,amount)).fetchall()
        _bump(stats,'references_read',len(rows))
        for (credit_id,) in rows:pairs.add((payment_id,credit_id))
    for record in new_credits:
        credit_id,venue,currency,amount,references=record
        rows=conn.execute(
            'SELECT DISTINCT pa.payment_id FROM payment_alias pa'
            ' JOIN payments p ON p.payment_id=pa.payment_id'
            ' WHERE pa.alias IN (%s) AND p.venue=? AND p.currency=? AND p.amount_minor=?'
            %_placeholders(len(references)),(*references,venue,currency,amount)).fetchall()
        _bump(stats,'aliases_read',len(rows))
        for (payment_id,) in rows:pairs.add((payment_id,credit_id))
    return pairs

def _bindings_in_components(conn,component_ids,stats):
    if not component_ids:return {}
    rows=conn.execute(
        "SELECT b.payment_id,b.credit_id FROM bindings b"
        " JOIN comp_vertex cv ON cv.kind='P' AND cv.vertex_id=b.payment_id"
        ' WHERE cv.component_id IN (%s)'%_placeholders(len(component_ids)),
        tuple(component_ids)).fetchall()
    _bump(stats,'bindings_read',len(rows))
    return {payment_id:credit_id for payment_id,credit_id in rows}

def _classify_and_write(conn,vertices,component_ids,stats):
    """Classify every vertex of one raw component and persist the rows.

    Manual bindings whose payment belongs to one of `component_ids` are fixed
    first: their endpoints leave the matching graph and are reported as
    `pinned` to their unique partner.  Returns the maximum number of remaining
    pairs (size of a maximum matching after removing fixed endpoints).
    """
    payments=sorted(v for kind,v in vertices if kind=='P')
    credits=sorted(vertex_id for kind,vertex_id in vertices if kind=='C')
    adjacency={payment:[] for payment in payments}
    for payment in payments:
        for (credit_id,) in conn.execute('SELECT credit_id FROM edges WHERE payment_id=?',
                                         (payment,)):
            adjacency[payment].append(credit_id)
            _bump(stats,'edges_read',1)
    bindings=_bindings_in_components(conn,component_ids,stats)
    partner={}
    for payment_id,credit_id in bindings.items():
        partner[('P',payment_id)]=credit_id
        partner[('C',credit_id)]=payment_id
    pinned_payments=set(bindings)
    pinned_credits=set(bindings.values())
    live_payments=[p for p in payments if p not in pinned_payments]
    live_credits=[c for c in credits if c not in pinned_credits]
    restricted={p:[c for c in adjacency[p] if c not in pinned_credits] for p in live_payments}
    size,peers,can_unmatched=classify(live_payments,live_credits,restricted)
    rows=[]
    for kind,vertex_id in vertices:
        key=(kind,vertex_id)
        if key in partner:
            status='pinned';possible=[partner[key]];can=False
        else:
            possible=sorted(peers[('L' if kind=='P' else 'R',vertex_id)])
            can=can_unmatched[('L' if kind=='P' else 'R',vertex_id)]
            if not possible:status='unmatched'
            elif len(possible)==1 and not can:status='forced'
            else:status='ambiguous'
        rows.append((kind,vertex_id,status,
                     json.dumps(possible,ensure_ascii=False,separators=(',',':')),
                     1 if can else 0))
    conn.executemany(
        'INSERT INTO inference(kind,vertex_id,status,peers,can_be_unmatched) VALUES(?,?,?,?,?)'
        ' ON CONFLICT(kind,vertex_id) DO UPDATE SET status=excluded.status,'
        ' peers=excluded.peers, can_be_unmatched=excluded.can_be_unmatched',rows)
    _bump(stats,'inference_written',len(rows))
    return size

def _component_of(conn,kind,vertex_id,stats):
    row=conn.execute('SELECT component_id FROM comp_vertex WHERE kind=? AND vertex_id=?',
                     (kind,vertex_id)).fetchone()
    _bump(stats,'comp_vertex_read',1)
    if row is None:raise DatasetError('corrupt component membership for '+vertex_id)
    return row[0]

def _component_totals(conn,component_ids,stats):
    vertices=0;edges=0;maximum=0
    for component in component_ids:
        row=conn.execute('SELECT vertex_count,edge_count,max_remaining FROM components'
                         ' WHERE component_id=?',(component,)).fetchone()
        _bump(stats,'components_read',1)
        vertices+=row[0];edges+=row[1];maximum+=row[2]
    return vertices,edges,maximum

def _write_component(conn,vertices,edge_count,maximum,old_ids,stats):
    if old_ids:target=old_ids[0]
    else:
        target=int(store.meta(conn,'next_component','1'))
        store.set_meta(conn,'next_component',target+1)
    if old_ids:
        conn.execute('DELETE FROM comp_vertex WHERE component_id IN (%s)'
                     %_placeholders(len(old_ids)),tuple(old_ids))
        for component in old_ids:
            if component!=target:
                conn.execute('DELETE FROM components WHERE component_id=?',(component,))
                _bump(stats,'components_written',1)
    conn.executemany('INSERT INTO comp_vertex(kind,vertex_id,component_id) VALUES(?,?,?)',
                     [(kind,vertex_id,target) for kind,vertex_id in vertices])
    _bump(stats,'comp_vertex_written',len(vertices))
    conn.execute('INSERT OR REPLACE INTO components(component_id,vertex_count,edge_count,max_remaining)'
                 ' VALUES(?,?,?,?)',(target,len(vertices),edge_count,maximum))
    _bump(stats,'components_written',1)

def _apply_import(conn,data,stats,fail_after_steps=None):
    payments,credits=parse_import(data)
    new_payments=[];new_credits=[]
    payment_replays=_insert_payments(conn,payments,new_payments,stats)
    credit_replays=_insert_credits(conn,credits,new_credits,stats)
    pairs=_candidate_edges(conn,new_payments,new_credits,stats)
    if pairs:
        conn.executemany('INSERT OR IGNORE INTO edges(payment_id,credit_id) VALUES(?,?)',
                         sorted(pairs))
        _bump(stats,'edges_written',len(pairs))
    new_payment_ids={record[0] for record in new_payments}
    new_credit_ids={record[0] for record in new_credits}
    # Union-find over old component ids and one virtual negative node per new
    # vertex; every new candidate edge joins its two endpoints, so a batch can
    # bridge two previously separate components (or start a new one).
    parent={}
    def find(node):
        parent.setdefault(node,node)
        root=node
        while parent[root]!=root:root=parent[root]
        while parent[node]!=root:
            parent[node],node=root,parent[node]
        return root
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra!=rb:parent[rb]=ra
    member_node={}      # (kind,vertex_id) -> union-find node
    counter=[-1]
    def node_for(kind,vertex_id,is_new):
        key=(kind,vertex_id)
        node=member_node.get(key)
        if node is not None:return node
        if is_new:
            node=counter[0];counter[0]-=1
        else:
            node=_component_of(conn,kind,vertex_id,stats)
        member_node[key]=node
        # Several vertices of the same old component map to that component id;
        # resetting its parent here would undo an earlier union, so only seed
        # the node if it is not already part of the forest.
        parent.setdefault(node,node)
        return node
    pair_nodes=[]
    for payment_id,credit_id in pairs:
        payment_node=node_for('P',payment_id,payment_id in new_payment_ids)
        credit_node=node_for('C',credit_id,credit_id in new_credit_ids)
        union(payment_node,credit_node)
        pair_nodes.append((payment_node,credit_node))
    groups={}
    def group(root):
        return groups.setdefault(root,{'old':set(),'new':set(),'new_edges':0})
    for node_key,node in member_node.items():
        kind,vertex_id=node_key
        is_new=vertex_id in (new_payment_ids if kind=='P' else new_credit_ids)
        bucket=group(find(node))
        if is_new:bucket['new'].add(node_key)
        elif node>=0:bucket['old'].add(node)
    for payment_node,_credit_node in pair_nodes:
        group(find(payment_node))['new_edges']+=1
    # A new vertex with no candidate edge at all is an isolated component.
    for kind,ids in (('P',new_payment_ids),('C',new_credit_ids)):
        for vertex_id in ids:
            if (kind,vertex_id) not in member_node:
                groups[('singleton',kind,vertex_id)]={'old':set(),'new':{(kind,vertex_id)},
                                                      'new_edges':0}
    maximum_delta=0
    for step,root in enumerate(sorted(groups,key=repr)):
        if fail_after_steps is not None and step>=fail_after_steps:
            raise DatasetError('injected failure after %d components'%step)
        current=groups[root]
        old_ids=sorted(current['old'])
        old_vertices,old_edges,old_maximum=_component_totals(conn,old_ids,stats)
        vertices=[]
        for component in old_ids:
            vertices.extend(conn.execute(
                'SELECT kind,vertex_id FROM comp_vertex WHERE component_id=?',(component,)).fetchall())
            _bump(stats,'comp_vertex_read',1)
        vertices.extend(sorted(current['new']))
        edge_count=old_edges+current['new_edges']
        if len(vertices)>MAX_COMPONENT_VERTICES or edge_count>MAX_COMPONENT_EDGES:
            raise DatasetError('candidate component exceeds %d vertices or %d edges'
                               %(MAX_COMPONENT_VERTICES,MAX_COMPONENT_EDGES))
        maximum=_classify_and_write(conn,vertices,old_ids,stats)
        _write_component(conn,vertices,edge_count,maximum,old_ids,stats)
        maximum_delta+=maximum-old_maximum
    fresh=len(new_payments)+len(new_credits)
    if fresh:
        generation=_generation(conn)+1
        store.set_meta(conn,'generation',generation)
        store.set_meta(conn,'maximum_pairs',
                       int(store.meta(conn,'maximum_pairs','0'))+maximum_delta)
        conn.execute('INSERT INTO imports(generation,payments_new,credits_new,edges_new)'
                     ' VALUES(?,?,?,?)',(generation,len(new_payments),len(new_credits),len(pairs)))
        _bump(stats,'imports_written',1)
    return {'payments':len(new_payments),'credits':len(new_credits),'edges':len(pairs),
            'payment_replays':payment_replays,'credit_replays':credit_replays,
            'generation':_generation(conn)}

def import_batch(path,data,stats=None,trace=None,fail_before_commit=False,
                 fail_after_steps=None):
    """Apply {payments,credits} atomically; returns batch counters.

    `fail_after_steps` raises after that many components were reclassified,
    which lets tests prove that a failure in the middle of writing inference
    rows rolls back the inference, component, edge and fact layers together.
    """
    if not Path(path).is_file():raise DatasetError('dataset is not initialized: '+str(path))
    conn=store.connect(path,trace=trace)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        result=_apply_import(conn,data,stats,fail_after_steps)
        if fail_before_commit:raise DatasetError('injected failure before commit')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return result

# ---------------------------------------------------------------- decide

def _rebuild_component(conn,component,stats):
    row=conn.execute('SELECT max_remaining FROM components WHERE component_id=?',
                     (component,)).fetchone()
    _bump(stats,'components_read',1)
    vertices=conn.execute('SELECT kind,vertex_id FROM comp_vertex WHERE component_id=?',
                          (component,)).fetchall()
    _bump(stats,'comp_vertex_read',1)
    maximum=_classify_and_write(conn,vertices,[component],stats)
    conn.execute('UPDATE components SET max_remaining=? WHERE component_id=?',(maximum,component))
    _bump(stats,'components_written',1)
    return maximum-row[0]

def _apply_decisions(conn,data,stats,fail_after_steps=None):
    decisions=parse_decisions(data)
    seen={};affected=set();binding_delta=0;fresh=0
    for decision in decisions:
        decision_id=decision[1];payload=canonical_decision(decision)
        if decision_id in seen:
            if seen[decision_id]!=payload:
                raise DatasetError('conflicting decision_id in batch: '+decision_id)
            continue
        seen[decision_id]=payload
        row=conn.execute('SELECT payload FROM decisions WHERE decision_id=?',(decision_id,)).fetchone()
        _bump(stats,'decisions_read',1)
        if row is not None:
            if row[0]!=payload:
                raise DatasetError('conflicting replay for decision_id: '+decision_id)
            continue
        if decision[0]=='pin':
            payment_id,credit_id=decision[2],decision[3]
            edge=conn.execute('SELECT 1 FROM edges WHERE payment_id=? AND credit_id=?',
                              (payment_id,credit_id)).fetchone()
            _bump(stats,'edges_read',1)
            if edge is None:
                raise DatasetError('pin is not a candidate edge: %s/%s'%(payment_id,credit_id))
            bound_payment=conn.execute('SELECT credit_id FROM bindings WHERE payment_id=?',
                                       (payment_id,)).fetchone()
            bound_credit=conn.execute('SELECT payment_id FROM bindings WHERE credit_id=?',
                                      (credit_id,)).fetchone()
            _bump(stats,'bindings_read',2)
            same=(bound_payment is not None and bound_payment[0]==credit_id
                  and bound_credit is not None and bound_credit[0]==payment_id)
            if (bound_payment is not None or bound_credit is not None) and not same:
                raise DatasetError('pin endpoints are already bound: %s/%s'
                                   %(payment_id,credit_id))
            if not same:
                conn.execute('INSERT INTO bindings(payment_id,credit_id,decision_id) VALUES(?,?,?)',
                             (payment_id,credit_id,decision_id))
                _bump(stats,'bindings_written',1)
                binding_delta+=1
                affected.add(_component_of(conn,'P',payment_id,stats))
            conn.execute('INSERT INTO decisions(decision_id,payload,op,payment_id,credit_id)'
                         ' VALUES(?,?,?,?,?)',(decision_id,payload,'pin',payment_id,credit_id))
        else:
            payment_id=decision[2]
            if conn.execute('SELECT 1 FROM payments WHERE payment_id=?',(payment_id,)).fetchone() is None:
                _bump(stats,'payments_read',1)
                raise DatasetError('unpin references an unknown payment: '+payment_id)
            _bump(stats,'payments_read',1)
            component=_component_of(conn,'P',payment_id,stats)
            cursor=conn.execute('DELETE FROM bindings WHERE payment_id=?',(payment_id,))
            _bump(stats,'bindings_written',1)
            if cursor.rowcount:
                binding_delta-=1
                affected.add(component)
            conn.execute('INSERT INTO decisions(decision_id,payload,op,payment_id,credit_id)'
                         ' VALUES(?,?,?,?,?)',(decision_id,payload,'unpin',payment_id,None))
        _bump(stats,'decisions_written',1)
        fresh+=1
    maximum_delta=0
    for step,component in enumerate(sorted(affected)):
        if fail_after_steps is not None and step>=fail_after_steps:
            raise DatasetError('injected failure after %d components'%step)
        maximum_delta+=_rebuild_component(conn,component,stats)
    if fresh:
        store.set_meta(conn,'maximum_pairs',
                       int(store.meta(conn,'maximum_pairs','0'))+maximum_delta+binding_delta)
        store.set_meta(conn,'generation',_generation(conn)+1)
    return {'decisions':fresh,'generation':_generation(conn)}

def decide(path,data,stats=None,trace=None,fail_before_commit=False,
           fail_after_steps=None):
    """Apply {decisions} in array order, atomically.

    `fail_after_steps` raises after that many affected components were
    reclassified, proving that a half-written inference rolls back with the
    decision log and bindings.
    """
    if not Path(path).is_file():raise DatasetError('dataset is not initialized: '+str(path))
    conn=store.connect(path,trace=trace)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        result=_apply_decisions(conn,data,stats,fail_after_steps)
        if fail_before_commit:raise DatasetError('injected failure before commit')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
    return result

# ---------------------------------------------------------------- report

def _write_report(conn,stream):
    generation=_generation(conn)
    maximum_pairs=int(store.meta(conn,'maximum_pairs','0'))
    stream.write('{\n  "generation": %d,\n  "maximum_pairs": %d,\n'%(generation,maximum_pairs))
    for index,kind in enumerate(('P','C')):
        stream.write('  "%s": ['%('payments' if kind=='P' else 'credits'))
        cursor=conn.execute('SELECT vertex_id,status,peers,can_be_unmatched FROM inference'
                            ' WHERE kind=? ORDER BY vertex_id',(kind,))
        first=True
        for vertex_id,status,peers,can_be_unmatched in cursor:
            row={'id':vertex_id,'status':status,'possible_peers':json.loads(peers),
                 'can_be_unmatched':bool(can_be_unmatched)}
            stream.write(('\n    ' if first else ',\n    ')
                         +json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')))
            first=False
        stream.write(('\n  ]' if not first else ']')+(',' if index==0 else '')+'\n')
    stream.write('}')

def report(path,output,trace=None):
    """Write the report from one read snapshot; atomic replace, read-only."""
    conn=store.connect(path,readonly=True,trace=trace)
    try:
        conn.execute('BEGIN')
        if not store.initialized(conn):raise DatasetError('dataset is not initialized')
        save_stream(output,lambda stream:_write_report(conn,stream))
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:conn.execute('ROLLBACK')
        raise
    finally:conn.close()
