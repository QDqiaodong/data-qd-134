"""Revision facts: strict parsing plus the per-id "highest revision wins" rule.

A stored revision row is (id, rev, op, venue, start, end, quantity) where op is
1 for upsert and 0 for delete.  For an upsert deleted fields are NULL.  For every
id only the row with the largest rev is effective; a delete at the top removes
the id without contributing, and lower revisions may never resurface.  A replayed
(id,rev) with identical parsed fields is a no-op; with different fields it is a
conflict and rejects the whole batch, even when that revision is superseded.
"""
from .common import require,ident,number

UPSERT,DELETE=1,0
FIELDS={'upsert':('id','rev','op','venue','start','end','quantity'),'delete':('id','rev','op')}
MAX_REV=10**12;MAX_BATCH=20000;MAX_BATCH_BYTES=8_000_000

def parse_batch(data):
    """Validate {revisions:[...]} and return ordered parsed records.

    Each record is ('upsert',id,rev,venue,start,end,quantity) or
    ('delete',id,rev,None,None,None,None); ids/venues are not yet looked up.
    """
    require(data,'revisions')
    items=data['revisions']
    if not isinstance(items,list) or len(items)>MAX_BATCH:raise ValueError('expected bounded revisions array')
    parsed=[];seen={}
    for item in items:
        if not isinstance(item,dict):raise ValueError('expected revision object')
        op=item.get('op')
        if op not in FIELDS or set(item)!=set(FIELDS[op]):raise ValueError('revision fields do not match op')
        bid=ident(item['id']);rev=number(item['rev'],1,MAX_REV)
        if op=='upsert':
            venue=ident(item['venue']);a=number(item['start'],0,10**12);z=number(item['end'],0,10**12);q=number(item['quantity'],1,10**6)
            if z<=a:raise ValueError('upsert must have positive duration')
            record=('upsert',bid,rev,venue,a,z,q)
        else:record=('delete',bid,rev,None,None,None,None)
        previous=seen.get((bid,rev))
        if previous is not None and previous!=record:raise ValueError('conflicting replay for id/rev')
        if previous is None:seen[(bid,rev)]=record;parsed.append(record)
    return parsed

def canonical(record):
    """Comparable field tuple used for replay equality."""
    return record
