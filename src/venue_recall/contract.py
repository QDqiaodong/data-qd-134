"""Strict parsing of the three venue-recall input documents.

init   : {"venues":[venue_id, ...]}   1..100 unique identifiers
ingest : {"operations":[op, ...]}     at most 20000 ops, applied in array order
recall : {"roots":[lot_id, ...]}      identifiers of raw lots

Parsed operation tuples (blend lists keep the source array order, so a replay
is identical only when the arrays match item by item, order included):

  ('receive', op_id, lot_id, grams)
  ('blend',   op_id, ((lot_id,grams),...), ((lot_id,grams),...))
  ('serve',   op_id, lot_id, grams, venue)
"""
import json
from .common import require,ident,grams

MAX_BATCH=20000
MAX_BLEND_ITEMS=100
MAX_VENUES=100

def parse_init(data):
    require(data,'venues')
    venues=data['venues']
    if not isinstance(venues,list) or not 1<=len(venues)<=MAX_VENUES:
        raise ValueError('expected 1..%d venues'%MAX_VENUES)
    venues=[ident(v) for v in venues]
    if len(set(venues))!=len(venues):raise ValueError('duplicate venue identifier')
    return venues

def _amount_list(value,role):
    if not isinstance(value,list) or not 1<=len(value)<=MAX_BLEND_ITEMS:
        raise ValueError('blend %s must be an array of 1..%d items'%(role,MAX_BLEND_ITEMS))
    parsed=[];seen=set()
    for item in value:
        require(item,'lot_id grams')
        lot=ident(item['lot_id']);amount=grams(item['grams'])
        if lot in seen:raise ValueError('duplicate lot_id in blend '+role)
        seen.add(lot);parsed.append((lot,amount))
    return tuple(parsed)

def parse_operations(data):
    require(data,'operations')
    items=data['operations']
    if not isinstance(items,list) or len(items)>MAX_BATCH:
        raise ValueError('expected a bounded operations array')
    parsed=[]
    for item in items:
        if not isinstance(item,dict):raise ValueError('expected operation object')
        kind=item.get('type')
        if kind=='receive':
            if set(item)!={'op_id','type','lot_id','grams'}:
                raise ValueError('receive fields must be exactly op_id,type,lot_id,grams')
            parsed.append(('receive',ident(item['op_id']),ident(item['lot_id']),grams(item['grams'])))
        elif kind=='blend':
            if set(item)!={'op_id','type','inputs','outputs'}:
                raise ValueError('blend fields must be exactly op_id,type,inputs,outputs')
            parsed.append(('blend',ident(item['op_id']),
                           _amount_list(item['inputs'],'inputs'),
                           _amount_list(item['outputs'],'outputs')))
        elif kind=='serve':
            if set(item)!={'op_id','type','lot_id','grams','venue'}:
                raise ValueError('serve fields must be exactly op_id,type,lot_id,grams,venue')
            parsed.append(('serve',ident(item['op_id']),ident(item['lot_id']),
                           grams(item['grams']),ident(item['venue'])))
        else:
            raise ValueError('unknown operation type')
    return parsed

def canonical_payload(op):
    """Order-preserving canonical JSON of the parsed payload (op_id excluded).

    Replay equality is decided on this string: the parsed full payload, with
    blend input/output arrays in their original order.
    """
    kind=op[0]
    if kind=='receive':
        body={'type':'receive','lot_id':op[2],'grams':op[3]}
    elif kind=='blend':
        body={'type':'blend',
              'inputs':[{'lot_id':lot,'grams':amount} for lot,amount in op[2]],
              'outputs':[{'lot_id':lot,'grams':amount} for lot,amount in op[3]]}
    else:
        body={'type':'serve','lot_id':op[2],'grams':op[3],'venue':op[4]}
    return json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False)

def parse_roots(data):
    require(data,'roots')
    roots=data['roots']
    if not isinstance(roots,list):raise ValueError('expected a roots array')
    parsed=[];seen=set()
    for value in roots:
        root=ident(value)
        if root not in seen:seen.add(root);parsed.append(root)
    return parsed
