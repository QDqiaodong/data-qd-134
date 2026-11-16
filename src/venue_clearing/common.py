"""Strict local JSON contract helpers and atomic report publication.

Deliberately independent of venue_occupancy.common, venue_energy.common and
venue_recall.common: the clearing pipeline owns its own contract and must keep
working if the other packages change.  The parsing rules mirror the repository
convention (non-blank bounded identifiers, exact integers, duplicate JSON keys
and non-finite numbers rejected, 8 MB source limit).
"""
import json,os,tempfile
from pathlib import Path

MAX_SOURCE_BYTES=8_000_000

def require(obj,keys):
    if not isinstance(obj,dict) or set(obj)!=set(keys.split()):
        raise ValueError('expected object fields: '+keys)

def ident(value,limit=128):
    """A non-blank identifier: stripped string, bounded length, no controls."""
    if not isinstance(value,str) or not value or value!=value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('expected a non-blank identifier of at most %d characters'%limit)
    return value

def currency(value):
    """An ISO-style currency code: exactly three uppercase ASCII letters."""
    if (not isinstance(value,str) or len(value)!=3
            or any(not('A'<=c<='Z') for c in value)):
        raise ValueError('currency must be exactly three uppercase ASCII letters')
    return value

def amount_minor(value):
    """A positive minor-unit amount: an exact integer in 1..10^12, never a bool."""
    if type(value) is not int or not 1<=value<=10**12:
        raise ValueError('amount_minor must be an integer in 1..10^12')
    return value

def text_list(value,role,limit=100):
    """1..limit distinct identifiers; source array order carries no meaning."""
    if not isinstance(value,list) or not 1<=len(value)<=limit:
        raise ValueError('%s must be an array of 1..%d identifiers'%(role,limit))
    parsed=[ident(item) for item in value]
    if len(set(parsed))!=len(parsed):raise ValueError('%s must not repeat an identifier'%role)
    return tuple(sorted(parsed))

def read(path):
    """Load a small strict JSON document: <=8 MB, duplicate keys and NaN/Inf rejected."""
    with Path(path).open('rb') as stream:raw=stream.read(MAX_SOURCE_BYTES+1)
    if len(raw)>MAX_SOURCE_BYTES:raise ValueError('JSON exceeds 8 MB')
    def unique(pairs):
        result={}
        for key,val in pairs:
            if key in result:raise ValueError('duplicate JSON key: '+str(key))
            result[key]=val
        return result
    def invalid(token):raise ValueError('non-finite JSON number: '+token)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)

def save_stream(path,write):
    """Publish a document atomically; a failure never removes the old file."""
    dest=Path(path)
    if dest.is_symlink():raise ValueError('output cannot be a symlink')
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=dest.parent,delete=False) as stream:
            temp=stream.name
            write(stream)
            stream.write('\n')
        os.replace(temp,dest)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)
