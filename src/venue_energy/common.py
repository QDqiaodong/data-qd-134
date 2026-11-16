"""Strict local JSON contract helpers and atomic report publication.

Deliberately independent of venue_occupancy.common: the energy pipeline owns
its own contract and must keep working if the other package changes.
"""
import hashlib,json,os,tempfile
from pathlib import Path

def ident(value,limit=128):
    """A non-blank identifier: stripped string, bounded length, no controls."""
    if not isinstance(value,str) or not value or value!=value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('expected a non-blank identifier of at most %d characters'%limit)
    return value

def load_json(path):
    """Load a small strict JSON document: duplicate keys and NaN/Inf rejected."""
    raw=Path(path).read_bytes()
    def unique(pairs):
        result={}
        for key,val in pairs:
            if key in result:raise ValueError('duplicate JSON key: '+str(key))
            result[key]=val
        return result
    def invalid(token):raise ValueError('non-finite JSON number: '+token)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)

def sha256_file(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1<<20),b''):
            digest.update(chunk)
    return digest.hexdigest()

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
