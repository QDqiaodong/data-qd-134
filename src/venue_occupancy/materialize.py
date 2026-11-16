"""Local segment maintenance.

`segments` holds maximal half-open runs of constant occupancy covering the whole
frozen horizon.  A changed booking rewrites only the runs containing its old and
new clipped intervals.  Runs between two far-apart intervals of the same booking
are never read: a move from one end of a large hall to the other causes two
small region rewrites, not one huge one.

A region is exactly the runs containing the affected intervals; each boundary
is then checked once for merging, so equal-occupancy runs that meet across a
region boundary are still folded into one maximal run.  This keeps runs
maximal while never reading or rewriting the empty stretches between two
far-apart affected intervals.

`venue_levels` is an occupancy histogram maintained by delta, so the peak after
a change is found with one descending primary-key seek (there are no zero-count
rows), and `venue_metrics` keeps the three aggregates so updates are O(1) in
venue size.  Aggregates and peak are never derived by scanning the venue.
Python integers are stored as text because booked_unit_ticks can exceed 64-bit
range.
"""
from collections import Counter,defaultdict
from .revisions import UPSERT

def _bump(stats,key,count=1):
    if stats is not None:stats[key]=stats.get(key,0)+count

def _select(conn,sql,params,stats,key):
    rows=conn.execute(sql,params).fetchall();_bump(stats,key,len(rows));return rows

def _runs_over(groups,events):
    """Overlay sorted (time,delta) events on sorted old runs (start,end,occ)."""
    times=sorted(events);new=[];index=0;cumulative=0
    while index<len(times) and times[index]<=groups[0][0]:
        cumulative+=events[times[index]];index+=1
    for a,b,occupancy in groups:
        position=a
        while index<len(times) and times[index]<b:
            at=times[index]
            if at>position:
                value=occupancy+cumulative
                if new and new[-1][2]==value:new[-1]=(new[-1][0],at,value)
                else:new.append((position,at,value))
                position=at
            cumulative+=events[at];index+=1
        value=occupancy+cumulative
        if new and new[-1][2]==value:new[-1]=(new[-1][0],b,value)
        else:new.append((position,b,value))
    return new

def _stats(runs,capacity):
    over_ticks=over_units=booked=0
    for s,e,o in runs:
        duration=e-s;excess=o-capacity
        booked+=o*duration
        if excess>0:over_ticks+=duration;over_units+=excess*duration
    return over_ticks,over_units,booked

def _write_levels(conn,vid,old,new,stats):
    """Move the stored histogram by the region's old->new difference."""
    for occupancy in set(old)|set(new):
        delta=new.get(occupancy,0)-old.get(occupancy,0)
        if delta==0:continue
        conn.execute('INSERT INTO venue_levels(venue,occupancy,pieces) VALUES(?,?,?) ON CONFLICT(venue,occupancy) DO UPDATE SET pieces=max(pieces+excluded.pieces,0)',(vid,occupancy,delta))
        _bump(stats,'levels_written',1)
    conn.execute('DELETE FROM venue_levels WHERE venue=? AND pieces<=0',(vid,))
    _bump(stats,'levels_pruned',1)

def _peak_after(conn,vid,previous,new_region_max,levels_changed,stats):
    if not levels_changed:return previous
    if new_region_max>previous:return new_region_max
    # Largest live occupancy at or below the previous peak; zero-count rows are
    # pruned, so this is one index seek, not a scan of the venue.
    row=conn.execute('SELECT occupancy FROM venue_levels WHERE venue=? AND occupancy<=? AND pieces>0 ORDER BY occupancy DESC LIMIT 1',(vid,previous)).fetchone()
    _bump(stats,'levels_read',1)
    return 0 if row is None else row[0]

def _read_metrics(conn,vid,stats):
    row=conn.execute('SELECT peak,over_capacity_ticks,excess_unit_ticks,booked_unit_ticks FROM venue_metrics WHERE venue=?',(vid,)).fetchone()
    _bump(stats,'metrics_read',1)
    if row is None:raise ValueError('missing venue metrics')
    return row[0],int(row[1]),int(row[2]),int(row[3])

def _apply_region(conn,vid,capacity,region_start,region_end,events,stats):
    old=_select(conn,'SELECT start,end,occupancy FROM segments WHERE venue=? AND start>=? AND start<? ORDER BY start',
                (vid,region_start,region_end),stats,'segments_read')
    if not old or old[0][0]!=region_start or old[-1][1]!=region_end:raise ValueError('segment coverage gap in affected region')
    for left,right in zip(old,old[1:]):
        if left[1]!=right[0]:raise ValueError('segment coverage gap in affected region')
    old=[tuple(run) for run in old];new=_runs_over(old,events)
    if new==old:return
    old_totals=_stats(old,capacity);new_totals=_stats(new,capacity)
    old_levels=Counter(run[2] for run in old);new_levels=Counter(run[2] for run in new)
    levels_changed=old_levels!=new_levels
    if levels_changed:_write_levels(conn,vid,old_levels,new_levels,stats)
    peak,over_ticks,over_units,booked=_read_metrics(conn,vid,stats)
    over_ticks+=new_totals[0]-old_totals[0];over_units+=new_totals[1]-old_totals[1];booked+=new_totals[2]-old_totals[2]
    peak=_peak_after(conn,vid,peak,max(run[2] for run in new),levels_changed,stats)
    conn.execute('UPDATE venue_metrics SET peak=?,over_capacity_ticks=?,excess_unit_ticks=?,booked_unit_ticks=? WHERE venue=?',
                 (peak,str(over_ticks),str(over_units),str(booked),vid))
    _bump(stats,'metrics_written',1)
    conn.execute('DELETE FROM segments WHERE venue=? AND start>=? AND start<?',(vid,region_start,region_end))
    conn.executemany('INSERT INTO segments(venue,start,end,occupancy) VALUES(?,?,?,?)',((vid,s,e,o) for s,e,o in new))
    _bump(stats,'segments_written',len(old)+len(new))

def _clusters(intervals):
    spans=sorted((s,e) for s,e in intervals if s<e);merged=[]
    for s,e in spans:
        if merged and s<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],e)
        else:merged.append([s,e])
    return merged

def _regions(conn,vid,intervals,stats):
    """The existing runs containing each affected interval (never more).

    A booking's old and new intervals are clustered; each cluster maps to the
    run containing its start and the run containing its end, so the region is
    exactly the possibly-changed runs.  Distant empty stretches between two
    clusters are not part of any region.
    """
    regions=[]
    for s,e in _clusters([(s,e) for s,e,_ in intervals]):
        head=_select(conn,'SELECT start,end FROM segments WHERE venue=? AND start<=? ORDER BY start DESC LIMIT 1',
                     (vid,s),stats,'segments_read')
        if not head or head[0][1]<s:raise ValueError('segment coverage gap at affected start')
        tail=_select(conn,'SELECT start,end FROM segments WHERE venue=? AND start<=? ORDER BY start DESC LIMIT 1',
                     (vid,e),stats,'segments_read')
        if not tail or tail[0][1]<e:raise ValueError('segment coverage gap at affected end')
        regions.append([head[0][0],tail[0][1]])
    regions.sort();merged=[]
    for s,e in regions:
        if merged and s<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],e)
        else:merged.append([s,e])
    return merged

def _merge_boundary(conn,vid,boundary,stats):
    """Merge two equal-occupancy runs that touch at `boundary`, keeping runs maximal.

    Both probes are primary-key seeks: the latest run starting before the
    boundary and the run starting exactly at it.  No venue-wide scan is needed
    to tell whether they are adjacent.
    """
    left=_select(conn,'SELECT start,end,occupancy FROM segments WHERE venue=? AND start<? ORDER BY start DESC LIMIT 1',
                 (vid,boundary),stats,'segments_read')
    if not left or left[0][1]!=boundary:return
    right=_select(conn,'SELECT end,occupancy FROM segments WHERE venue=? AND start=?',
                  (vid,boundary),stats,'segments_read')
    if not right or left[0][2]!=right[0][1]:return
    conn.execute('DELETE FROM segments WHERE venue=? AND start IN (?,?)',(vid,left[0][0],boundary))
    conn.execute('INSERT INTO segments(venue,start,end,occupancy) VALUES(?,?,?,?)',
                 (vid,left[0][0],right[0][0],left[0][2]))
    # Two runs of this occupancy became one, so the histogram count drops by one.
    conn.execute('INSERT INTO venue_levels(venue,occupancy,pieces) VALUES(?,?,?) '
                 'ON CONFLICT(venue,occupancy) DO UPDATE SET pieces=max(pieces+excluded.pieces,0)',
                 (vid,left[0][2],-1))
    conn.execute('DELETE FROM venue_levels WHERE venue=? AND occupancy=? AND pieces<=0',(vid,left[0][2]))
    _bump(stats,'levels_written',2);_bump(stats,'segments_written',3)

def apply_intervals(conn,vid,capacity,start,end,intervals,stats=None):
    """Apply clipped (start,end,delta) changes to one venue.

    `intervals` is the union of the old and new effective intervals of the
    changed booking(s): +quantity for arrivals, -quantity for departures.
    """
    if not intervals:return
    for region_start,region_end in _regions(conn,vid,intervals,stats):
        events=defaultdict(int)
        for s,e,delta in intervals:
            if region_start<=s<region_end:events[s]+=delta
            if region_start<e<region_end:events[e]-=delta
        _apply_region(conn,vid,capacity,region_start,region_end,events,stats)
        if start<region_start:_merge_boundary(conn,vid,region_start,stats)
        if region_end<end:_merge_boundary(conn,vid,region_end,stats)

def rebuild_venue(conn,vid,capacity,start,end,stats=None):
    """Recompute segments/metrics for one venue from effective facts (init)."""
    changes=defaultdict(int);changes[start]+=0;changes[end]+=0
    for a,z,q in _select(conn,'SELECT start,end,quantity FROM effective WHERE venue=? AND op=?',(vid,UPSERT),stats,'effective_read'):
        a=max(start,a);z=min(end,z)
        if a<z:changes[a]+=q;changes[z]-=q
    times=sorted(changes);occupancy=peak=over_ticks=over_units=booked=0;segments=[]
    for i,t in enumerate(times[:-1]):
        occupancy+=changes[t];nxt=times[i+1];duration=nxt-t;excess=max(0,occupancy-capacity)
        peak=max(peak,occupancy);over_ticks+=duration if excess else 0;over_units+=excess*duration;booked+=occupancy*duration
        if segments and segments[-1][2]==occupancy:segments[-1]=(segments[-1][0],nxt,occupancy)
        else:segments.append((t,nxt,occupancy))
    conn.execute('DELETE FROM segments WHERE venue=?',(vid,))
    conn.executemany('INSERT INTO segments(venue,start,end,occupancy) VALUES(?,?,?,?)',((vid,s,e,o) for s,e,o in segments))
    conn.execute('DELETE FROM venue_levels WHERE venue=?',(vid,))
    conn.executemany('INSERT INTO venue_levels(venue,occupancy,pieces) VALUES(?,?,?)',((vid,o,c) for o,c in Counter(run[2] for run in segments).items()))
    conn.execute('INSERT INTO venue_metrics(venue,peak,over_capacity_ticks,excess_unit_ticks,booked_unit_ticks) VALUES(?,?,?,?,?) ON CONFLICT(venue) DO UPDATE SET peak=excluded.peak,over_capacity_ticks=excluded.over_capacity_ticks,excess_unit_ticks=excluded.excess_unit_ticks,booked_unit_ticks=excluded.booked_unit_ticks',
                 (vid,peak,str(over_ticks),str(over_units),str(booked)))
    _bump(stats,'segments_written',2*len(segments));_bump(stats,'levels_written',2*len(set(run[2] for run in segments)))
