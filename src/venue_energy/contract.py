"""Strict input contracts for meters.json, readings.csv and tariffs.json.

Everything is validated eagerly; ValueError carries the first offending item.
Bounds are deliberately explicit so binary records on the spill files can use
fixed-width unsigned integer fields safely:

* observed_at is an epoch second in [2000-01-01, 2100-01-01) -> fits uint32,
* register_wh is in [0, modulus_wh), modulus_wh is a positive uint32,
* meter list order is frozen into a small uint32 index used by the sort key.
"""
import csv
from .common import ident,load_json

EPOCH_MIN=946684800          # 2000-01-01T00:00:00Z
EPOCH_MAX_EXCLUSIVE=4102444800  # 2100-01-01T00:00:00Z
READING_HEADER=('meter_id','read_id','observed_at','register_wh')
MAX_IDENT=128
MAX_RATE=10**12              # micro currency units or mg per kWh

def require_fields(obj,fields,where):
    if not isinstance(obj,dict) or set(obj)!=set(fields):
        raise ValueError('%s must be an object with exactly the fields %s'%(where,','.join(fields)))

def unsigned(value,maximum,what):
    if type(value) is not int or isinstance(value,bool) or not 0<=value<=maximum:
        raise ValueError('%s must be an integer in [0,%d]'%(what,maximum))
    return value

def load_meters(path):
    """Return (order, by_id): meter_id -> (modulus_wh, max_watts)."""
    data=load_json(path)
    require_fields(data,('meters',),'meters document')
    entries=data['meters']
    if not isinstance(entries,list):raise ValueError('meters must be an array')
    order=[];by_id={}
    for index,entry in enumerate(entries):
        require_fields(entry,('meter_id','venue','modulus_wh','max_watts'),'meter entry')
        meter_id=ident(entry['meter_id'],MAX_IDENT)
        venue=ident(entry['venue'],MAX_IDENT)
        modulus=unsigned(entry['modulus_wh'],2**32-1,'modulus_wh')
        if modulus==0:raise ValueError('modulus_wh must be positive')
        max_watts=unsigned(entry['max_watts'],2**32-1,'max_watts')
        if meter_id in by_id:raise ValueError('duplicate meter_id: '+meter_id)
        order.append(meter_id)
        by_id[meter_id]=(venue,modulus,max_watts)
    return order,by_id

def load_tariffs(path,known_venues):
    """Return venue -> sorted list of (start,end,cost_micro_per_kwh,carbon_mg_per_kwh).

    Shape and non-overlap are validated here; full coverage of the actual
    reading span is checked by the engine against each interpreted interval.
    """
    data=load_json(path)
    require_fields(data,('tariffs',),'tariffs document')
    entries=data['tariffs']
    if not isinstance(entries,list):raise ValueError('tariffs must be an array')
    grouped={}
    for entry in entries:
        require_fields(entry,('venue','start','end','cost_micro_per_kwh','carbon_mg_per_kwh'),'tariff entry')
        venue=ident(entry['venue'],MAX_IDENT)
        if venue not in known_venues:raise ValueError('tariff references unknown venue: '+venue)
        start=unsigned(entry['start'],EPOCH_MAX_EXCLUSIVE-1,'tariff start')
        end=unsigned(entry['end'],EPOCH_MAX_EXCLUSIVE,'tariff end')
        if end<=start:raise ValueError('tariff interval must have positive duration')
        cost=unsigned(entry['cost_micro_per_kwh'],MAX_RATE,'cost_micro_per_kwh')
        carbon=unsigned(entry['carbon_mg_per_kwh'],MAX_RATE,'carbon_mg_per_kwh')
        grouped.setdefault(venue,[]).append((start,end,cost,carbon))
    result={}
    for venue,ranges in grouped.items():
        ranges.sort()
        for left,right in zip(ranges,ranges[1:]):
            if right[0]<left[1]:raise ValueError('overlapping tariff intervals for venue: '+venue)
        result[venue]=ranges
    return result

def _strict_int(text,what):
    if not text or not text.isascii() or not all('0'<=c<='9' for c in text):
        raise ValueError('%s must be a base-10 non-negative integer'%what)
    return int(text)

def iter_reading_rows(path):
    """Yield (meter_id, read_id, observed_at, register_wh) tuples from the CSV.

    The fixed header is required verbatim (a UTF-8 BOM is rejected), blank
    lines are rejected, every row must have exactly four fields and both
    integer fields must be plain decimal numerals.  The caller owns range and
    referential checks once the meter catalogue is known.
    """
    with open(path,'rb') as raw:
        if raw.read(3)==b'\xef\xbb\xbf':raise ValueError('readings CSV must not start with a UTF-8 BOM')
    with open(path,'r',encoding='utf-8',newline='') as stream:
        reader=csv.reader(stream)
        try:
            header=next(reader)
        except StopIteration:raise ValueError('readings CSV is empty')
        if header!=list(READING_HEADER):raise ValueError('readings CSV header must be exactly: '+','.join(READING_HEADER))
        for line_no,row in enumerate(reader,start=2):
            if len(row)!=4:raise ValueError('line %d: expected exactly 4 CSV fields'%line_no)
            meter_id,read_id,time_text,register_text=row
            ident(meter_id,MAX_IDENT);ident(read_id,MAX_IDENT)
            observed_at=_strict_int(time_text,'observed_at')
            register_wh=_strict_int(register_text,'register_wh')
            yield meter_id,read_id,observed_at,register_wh
