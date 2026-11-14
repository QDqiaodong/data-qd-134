from collections import defaultdict

def audit(venues,bookings,start,end):
    result=[]
    for vid,capacity in sorted(venues.items()):
        changes=defaultdict(int);changes[start]=0;changes[end]=0
        for b in bookings:
            if b['venue']!=vid:continue
            a=max(start,b['start']);z=min(end,b['end'])
            if a<z:changes[a]+=b['quantity'];changes[z]-=b['quantity']
        times=sorted(changes);occupancy=peak=over_ticks=over_units=booked_units=0;segments=[]
        for i,t in enumerate(times[:-1]):
            occupancy+=changes[t];next_time=times[i+1];duration=next_time-t;excess=max(0,occupancy-capacity)
            peak=max(peak,occupancy);over_ticks+=duration if excess else 0;over_units+=excess*duration;booked_units+=occupancy*duration
            if segments and segments[-1]['occupancy']==occupancy:segments[-1]['end']=next_time
            else:segments.append({'start':t,'end':next_time,'occupancy':occupancy,'excess':excess})
        result.append({'venue':vid,'capacity':capacity,'peak':peak,'over_capacity_ticks':over_ticks,'excess_unit_ticks':over_units,'booked_unit_ticks':booked_units,'segments':segments})
    return {'horizon':{'start':start,'end':end},'venues':result}
