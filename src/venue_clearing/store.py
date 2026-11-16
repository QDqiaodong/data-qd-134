"""SQLite storage for the clearing pipeline: schema, connections, accessors.

Tables
------
meta(key,value)                    schema version, generation, next component id
payments(payment_id,venue,currency,amount_minor,payload)
                                   every payment ever imported; payload is the
                                   canonical parsed JSON used for replay equality
credits(credit_id,venue,currency,amount_minor,payload)
                                   every credit ever imported
payment_alias(alias,payment_id)    inverted alias relation; the only way a
                                   credit discovers candidate payments
credit_reference(reference,credit_id)
                                   inverted remark relation
edges(payment_id,credit_id)        candidate edges, one row per (payment,credit)
                                   pair however many alias/reference items match
components(component_id,vertex_count,edge_count,max_remaining)
                                   one row per *raw* candidate connected
                                   component; max_remaining is the size of a
                                   maximum matching on the component after
                                   pinned endpoints are removed
comp_vertex(kind,vertex_id,component_id)
                                   membership; every vertex belongs to exactly
                                   one component (an isolated vertex is its own)
inference(kind,vertex_id,status,peers,can_be_unmatched)
                                   derived classification of every vertex,
                                   rewritten in the same transaction as the
                                   facts it derives from
bindings(payment_id,credit_id,decision_id)
                                   current manual bindings; payment and credit
                                   are each used at most once
decisions(decision_id,payload,op,payment_id,credit_id)
                                   append-only decision log; replay equality is
                                   decided on payload, which is what keeps a
                                   retried old pin from rebinding after unpin
imports(import_id,generation,payments_new,credits_new,edges_new)
                                   append-only source log: one row per batch
                                   that actually added facts, stamped with the
                                   generation it produced (pure replays add
                                   nothing and are not logged)

Every read in import/decide is a primary-key seek, an inverted-index probe, or
a bounded traversal of the affected component's vertices/edges.  Nothing walks
unrelated components.
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION=1
SCHEMA='''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE payments(
    payment_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE credits(
    credit_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE payment_alias(
    alias TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    PRIMARY KEY(alias,payment_id)
) WITHOUT ROWID;
CREATE TABLE credit_reference(
    reference TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    PRIMARY KEY(reference,credit_id)
) WITHOUT ROWID;
CREATE TABLE edges(
    payment_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    PRIMARY KEY(payment_id,credit_id)
) WITHOUT ROWID;
CREATE INDEX edges_credit ON edges(credit_id);
CREATE TABLE components(
    component_id INTEGER PRIMARY KEY,
    vertex_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    max_remaining INTEGER NOT NULL
);
CREATE TABLE comp_vertex(
    kind TEXT NOT NULL,
    vertex_id TEXT NOT NULL,
    component_id INTEGER NOT NULL,
    PRIMARY KEY(kind,vertex_id)
) WITHOUT ROWID;
CREATE INDEX comp_vertex_component ON comp_vertex(component_id);
CREATE TABLE inference(
    kind TEXT NOT NULL,
    vertex_id TEXT NOT NULL,
    status TEXT NOT NULL,
    peers TEXT NOT NULL,
    can_be_unmatched INTEGER NOT NULL,
    PRIMARY KEY(kind,vertex_id)
) WITHOUT ROWID;
CREATE TABLE bindings(
    payment_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL
);
CREATE TABLE decisions(
    decision_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    op TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    credit_id TEXT
);
CREATE TABLE imports(
    import_id INTEGER PRIMARY KEY,
    generation INTEGER NOT NULL,
    payments_new INTEGER NOT NULL,
    credits_new INTEGER NOT NULL,
    edges_new INTEGER NOT NULL
);
'''

def connect(path,readonly=False,trace=None,timeout=30.0):
    """Open the dataset.  `trace` exists for locality tests."""
    if readonly:
        uri=Path(path).resolve().as_uri()+'?mode=ro'
        conn=sqlite3.connect(uri,uri=True,isolation_level=None,timeout=timeout)
    else:
        conn=sqlite3.connect(str(path),isolation_level=None,timeout=timeout)
        # WAL lets a report hold a stable read snapshot while another process
        # commits.  The setting is persistent in the database file.
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA busy_timeout=%d'%int(timeout*1000))
    if trace is not None:conn.set_trace_callback(trace)
    return conn

def init_schema(conn):
    # executescript() would implicitly commit the caller's transaction, so the
    # DDL statements are issued one at a time inside it.
    for statement in SCHEMA.split(';'):
        if statement.strip():conn.execute(statement)
    conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)',
                     [('schema_version',str(SCHEMA_VERSION)),('generation','0'),
                      ('maximum_pairs','0'),('next_component','1')])

def initialized(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is not None

def meta(conn,key,default=None):
    row=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return default if row is None else row[0]

def set_meta(conn,key,value):
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
