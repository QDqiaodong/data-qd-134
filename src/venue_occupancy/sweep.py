from collections import defaultdict

def audit(venues,bookings,start,end):
    # Group once, then sweep each venue independently.  The result is exactly the
    # same as the former per-venue rescan: half-open [start,end) pieces covering
    # the whole horizon, with adjacent equal-occupancy pieces merged.
    changes={vid:{start:0,end:0} for vid in venues}
    for b in bookings:
        current=changes.get(b['venue'])
        if current is None:continue
        a=max(start,b['start']);z=min(end,b['end'])
        if a<z:
            current[a]=current.get(a,0)+b['quantity']
            current[z]=current.get(z,0)-b['quantity']
    result=[]
    for vid,capacity in sorted(venues.items()):
        events=changes[vid]
        times=sorted(events);occupancy=peak=over_ticks=over_units=booked_units=0;segments=[]
        for i,t in enumerate(times[:-1]):
            occupancy+=events[t];next_time=times[i+1];duration=next_time-t;excess=max(0,occupancy-capacity)
            peak=max(peak,occupancy);over_ticks+=duration if excess else 0;over_units+=excess*duration;booked_units+=occupancy*duration
            if segments and segments[-1]['occupancy']==occupancy:segments[-1]['end']=next_time
            else:segments.append({'start':t,'end':next_time,'occupancy':occupancy,'excess':excess})
        result.append({'venue':vid,'capacity':capacity,'peak':peak,'over_capacity_ticks':over_ticks,'excess_unit_ticks':over_units,'booked_unit_ticks':booked_units,'segments':segments})
    return {'horizon':{'start':start,'end':end},'venues':result}
