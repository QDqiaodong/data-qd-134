"""Strict parsing of the three venue-clearing input documents.

import  : {"payments":[...], "credits":[...]}
decide  : {"decisions":[...]}

Parsed records are tuples with sorted alias/reference tuples, so the source
array order of an alias list never changes replay equality:

  payment = (payment_id, venue, currency, amount_minor, aliases)
  credit  = (credit_id,  venue, currency, amount_minor, references)

A decision is one of:

  ('pin',   decision_id, payment_id, credit_id)
  ('unpin', decision_id, payment_id)

`canonical_record` produces the exact text used for replay equality.  The
batch-level checks (global id replay/conflict, pin must be an existing
candidate edge, no double binding) live in dataset.py because they need the
stored state.
"""
import json
from .common import require,ident,currency,amount_minor,text_list

MAX_RECORDS=2000

def _payment(item):
    require(item,'payment_id venue currency amount_minor aliases')
    return (ident(item['payment_id']),ident(item['venue']),currency(item['currency']),
            amount_minor(item['amount_minor']),text_list(item['aliases'],'aliases'))

def _credit(item):
    require(item,'credit_id venue currency amount_minor references')
    return (ident(item['credit_id']),ident(item['venue']),currency(item['currency']),
            amount_minor(item['amount_minor']),text_list(item['references'],'references'))

def parse_import(data):
    require(data,'payments credits')
    payments=data['payments'];credits=data['credits']
    if not isinstance(payments,list) or not isinstance(credits,list):
        raise ValueError('payments and credits must be arrays')
    if len(payments)+len(credits)>MAX_RECORDS:
        raise ValueError('at most %d records per document'%MAX_RECORDS)
    return [_payment(item) for item in payments],[_credit(item) for item in credits]

def _decision(item):
    if not isinstance(item,dict):raise ValueError('expected decision object')
    op=item.get('op')
    if op=='pin':
        require(item,'decision_id op payment_id credit_id')
        return ('pin',ident(item['decision_id']),ident(item['payment_id']),ident(item['credit_id']))
    if op=='unpin':
        require(item,'decision_id op payment_id')
        return ('unpin',ident(item['decision_id']),ident(item['payment_id']))
    raise ValueError('unknown decision op')

def parse_decisions(data):
    require(data,'decisions')
    items=data['decisions']
    if not isinstance(items,list) or len(items)>MAX_RECORDS:
        raise ValueError('decisions must be an array of at most %d items'%MAX_RECORDS)
    return [_decision(item) for item in items]

def canonical_payment(record):
    return json.dumps({'payment_id':record[0],'venue':record[1],'currency':record[2],
                       'amount_minor':record[3],'aliases':list(record[4])},
                      sort_keys=True,separators=(',',':'),ensure_ascii=False)

def canonical_credit(record):
    return json.dumps({'credit_id':record[0],'venue':record[1],'currency':record[2],
                       'amount_minor':record[3],'references':list(record[4])},
                      sort_keys=True,separators=(',',':'),ensure_ascii=False)

def canonical_decision(decision):
    if decision[0]=='pin':
        body={'op':'pin','payment_id':decision[2],'credit_id':decision[3]}
    else:
        body={'op':'unpin','payment_id':decision[2]}
    return json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False)
