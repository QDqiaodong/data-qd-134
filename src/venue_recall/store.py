"""SQLite storage for the recall pipeline: schema, connections and accessors.

Tables
------
meta(key,value)                 schema version
venues(id)                      frozen venue catalogue (at most 100 rows)
ops(op_id,type,payload)         every accepted operation; payload is the
                                canonical parsed JSON used for replay equality
lots(lot_id,kind,op_id,initial_grams,remaining_grams,quarantined)
                                every lot ever created, even when exhausted, so
                                a lot id can never be recreated; remaining_grams
                                is updated in the same transaction as the
                                consumption that moves it
consumption(op_id,lot_id,grams) every stock deduction (blend inputs and serves),
                                linking each consumption to the op whose outputs
                                it fed; remaining = initial - sum(consumption)
deliveries(op_id,lot_id,venue,grams)
                                serve attribution; kept forever because recalls
                                are retroactive over these rows
composition(lot_id,root_id,num,den)
                                exact rational composition of every lot over the
                                raw lots it descends from; num/den are decimal
                                text because denominators exceed 64 bits; only
                                positive entries are stored
recalls(root_id)                the permanent recall set

All lookups are primary-key seeks except recall quarantine and report, which
use the composition(root_id) index, and report's ordered streaming scans of
deliveries and lots, which are full exports by definition.
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION=1
SCHEMA='''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE venues(id TEXT PRIMARY KEY);
CREATE TABLE ops(op_id TEXT PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE lots(
    lot_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    op_id TEXT NOT NULL,
    initial_grams INTEGER NOT NULL,
    remaining_grams INTEGER NOT NULL,
    quarantined INTEGER NOT NULL
);
CREATE TABLE consumption(
    op_id TEXT NOT NULL, lot_id TEXT NOT NULL, grams INTEGER NOT NULL,
    PRIMARY KEY(op_id,lot_id)
) WITHOUT ROWID;
CREATE TABLE deliveries(
    op_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, venue TEXT NOT NULL, grams INTEGER NOT NULL
);
CREATE TABLE composition(
    lot_id TEXT NOT NULL, root_id TEXT NOT NULL, num TEXT NOT NULL, den TEXT NOT NULL,
    PRIMARY KEY(lot_id,root_id)
) WITHOUT ROWID;
CREATE INDEX composition_root ON composition(root_id);
CREATE TABLE recalls(root_id TEXT PRIMARY KEY);
'''

def connect(path,readonly=False,trace=None,timeout=30.0):
    """Open the dataset.  `trace` exists for observation tests."""
    if readonly:
        uri=Path(path).resolve().as_uri()+'?mode=ro'
        conn=sqlite3.connect(uri,uri=True,isolation_level=None,timeout=timeout)
    else:
        conn=sqlite3.connect(str(path),isolation_level=None,timeout=timeout)
        # WAL lets report hold a stable read snapshot while another process
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
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",(str(SCHEMA_VERSION),))

def initialized(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is not None

def meta(conn,key,default=None):
    row=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return default if row is None else row[0]

def set_meta(conn,key,value):
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))
