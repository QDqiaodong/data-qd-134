"""SQLite storage: schema, connections and small accessors.

Tables
------
meta(key,value)            frozen horizon, schema version and generation
venues(id,capacity)        frozen venue catalogue (at most 100 rows)
facts(id,rev,op,...)       every revision ever accepted, including superseded ones
effective(id,rev,op,...)   pointer to the single effective revision per id
segments(venue,start,end,occupancy)
                           maximal half-open runs covering the whole horizon
venue_levels(venue,occupancy,pieces)
                           occupancy histogram used for an O(1) peak lookup
venue_metrics(venue,...)   peak and the three aggregate metrics

Only `facts` and `effective` grow with history.  Segments, levels and metrics
are derived caches.  Every lookup made by apply is a primary-key seek:
facts by (id,rev), effective by id, segments by (venue,start), levels by
(venue,occupancy), metrics by venue.  The single secondary index on
effective(venue) is used only by the full per-venue rebuild at init time and by
audits; apply never scans it.
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION=1
SCHEMA='''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE venues(id TEXT PRIMARY KEY, capacity INTEGER NOT NULL);
CREATE TABLE facts(
    id TEXT NOT NULL, rev INTEGER NOT NULL, op INTEGER NOT NULL,
    venue TEXT, start INTEGER, end INTEGER, quantity INTEGER,
    PRIMARY KEY(id,rev)
) WITHOUT ROWID;
CREATE TABLE effective(
    id TEXT PRIMARY KEY, rev INTEGER NOT NULL, op INTEGER NOT NULL,
    venue TEXT, start INTEGER, end INTEGER, quantity INTEGER
) WITHOUT ROWID;
CREATE INDEX effective_venue ON effective(venue,op);
CREATE TABLE segments(
    venue TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    occupancy INTEGER NOT NULL,
    PRIMARY KEY(venue,start)
) WITHOUT ROWID;
CREATE TABLE venue_levels(
    venue TEXT NOT NULL, occupancy INTEGER NOT NULL, pieces INTEGER NOT NULL,
    PRIMARY KEY(venue,occupancy)
) WITHOUT ROWID;
CREATE TABLE venue_metrics(
    venue TEXT PRIMARY KEY,
    peak INTEGER NOT NULL,
    over_capacity_ticks TEXT NOT NULL,
    excess_unit_ticks TEXT NOT NULL,
    booked_unit_ticks TEXT NOT NULL
) WITHOUT ROWID;
'''

def connect(path,readonly=False,trace=None,progress=None,timeout=30.0):
    """Open the dataset.  `trace` and `progress` exist for observation tests."""
    if readonly:
        uri=Path(path).resolve().as_uri()+'?mode=ro'
        conn=sqlite3.connect(uri,uri=True,isolation_level=None,timeout=timeout)
    else:
        conn=sqlite3.connect(str(path),isolation_level=None,timeout=timeout)
        # WAL lets export hold a stable read snapshot while another process
        # commits.  The setting is persistent in the database file.
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA busy_timeout=%d'%int(timeout*1000))
    if trace is not None:conn.set_trace_callback(trace)
    if progress is not None:conn.set_progress_handler(progress,1000)
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
