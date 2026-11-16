from .common import require,ident,number,rows
from .sweep import audit

def contract(data):
    """Validate the v1 JSON contract and return (start,end,venues,bookings)."""
    require(data,'horizon venues bookings');h=data['horizon'];require(h,'start end');start=number(h['start'],0,10**12);end=number(h['end'],0,10**12)
    if end<=start:raise ValueError('horizon must have positive duration')
    venues={}
    for v in rows(data['venues'],100):
        require(v,'id capacity');vid=ident(v['id']);cap=number(v['capacity'],0,10**6)
        if vid in venues:raise ValueError('duplicate venue')
        venues[vid]=cap
    if not venues:raise ValueError('venues cannot be empty')
    bookings=[];seen=set()
    for b in rows(data['bookings'],20000):
        require(b,'id venue start end quantity');bid=ident(b['id']);venue=ident(b['venue']);a=number(b['start'],0,10**12);z=number(b['end'],0,10**12);q=number(b['quantity'],1,10**6)
        if bid in seen or venue not in venues or z<=a:raise ValueError('duplicate booking, unknown venue, or nonpositive interval')
        seen.add(bid);bookings.append({'id':bid,'venue':venue,'start':a,'end':z,'quantity':q})
    return start,end,venues,bookings

def analyze(data):
    start,end,venues,bookings=contract(data)
    return audit(venues,bookings,start,end)
