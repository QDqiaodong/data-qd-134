"""External merge-sort over fixed-layout binary spill files.

A reading record is the tuple ``(meter_idx, observed_at, register_wh,
read_id)``; on disk it is a 17-byte big-endian header (uint32 meter index,
uint32 epoch second, uint64 register value, uint8 read_id byte length)
followed by the UTF-8 read_id (identifiers are capped at 128 bytes).  Both
sort orders used by the pipeline reuse the same layout and only differ in
their key function:

* read-id order  ``(read_id, meter_idx, observed_at, register_wh)`` detects
  duplicate/conflicting read_ids before anything is settled;
* meter order    ``(meter_idx, observed_at, register_wh, read_id)`` is the
  deterministic per-meter chronological scan order.

Memory is bounded by one in-memory sort buffer plus, per merge, a fixed-size
read chunk per input stream and one heap entry per stream.  Crucially run
files are *streamed* in fixed chunks: a second merge level reads multi-MB
runs with the same small footprint as the first, so peak memory never tracks
run length or input row count.  Run count is reduced in fan-in levels so the
merge heap itself cannot grow with the input either.
"""
import heapq,os,struct,tempfile

_HEADER=struct.Struct('>IIQB')
READ_ID_LIMIT=128
CHUNK_SIZE=1<<20           # 1 MiB refill blocks for merge readers

def pack_record(record):
    meter_idx,observed_at,register_wh,read_id=record
    payload=read_id.encode('utf-8')
    if len(payload)>READ_ID_LIMIT:raise ValueError('read_id exceeds %d bytes'%READ_ID_LIMIT)
    return _HEADER.pack(meter_idx,observed_at,register_wh,len(payload))+payload

def unpack_record(buffer,pos):
    meter_idx,observed_at,register_wh,length=_HEADER.unpack_from(buffer,pos)
    start=pos+_HEADER.size
    read_id=buffer[start:start+length].decode('utf-8')
    return (meter_idx,observed_at,register_wh,read_id),start+length

def write_run(path,records,key):
    """Sort an in-memory chunk and stream it to one binary run file."""
    ordered=sorted(records,key=key)
    with open(path,'wb') as stream:
        for record in ordered:
            stream.write(pack_record(record))

class RunReader:
    """Streaming record reader with a fixed-size refill buffer.

    Records can straddle chunk boundaries; the small unconsumed tail is kept
    and extended on the next refill, so resident memory is O(CHUNK_SIZE),
    independent of the run file's total size.
    """

    def __init__(self,path,chunk_size=CHUNK_SIZE):
        self._stream=open(path,'rb');self._chunk=chunk_size
        self._buffer=bytearray();self._pos=0

    def _take(self,size):
        while len(self._buffer)-self._pos<size:
            if self._pos:
                del self._buffer[:self._pos];self._pos=0
            block=self._stream.read(self._chunk)
            if not block:raise EOFError('truncated run file: '+self._stream.name)
            self._buffer.extend(block)
        data=bytes(self._buffer[self._pos:self._pos+size]);self._pos+=size
        return data

    def __iter__(self):return self

    def close(self):
        """Idempotent close; safe in finally blocks when a merge is abandoned."""
        if not self._stream.closed:self._stream.close()

    def __next__(self):
        if self._pos>=len(self._buffer):
            block=self._stream.read(self._chunk)
            if not block:
                self._stream.close();raise StopIteration
            self._buffer=bytearray(block);self._pos=0
        header=self._take(_HEADER.size)
        meter_idx,observed_at,register_wh,length=_HEADER.unpack(header)
        read_id=self._take(length).decode('utf-8')
        return meter_idx,observed_at,register_wh,read_id

def iter_run(path):
    """Stream records from one run file without loading it wholesale."""
    return RunReader(path)

def _merge(paths,key):
    """k-way merge generator that always closes every reader it opened."""
    readers=[RunReader(path) for path in paths]
    try:
        yield from heapq.merge(*readers,key=key)
    finally:
        for reader in readers:reader.close()

def _merge_to_file(paths,out_path,key):
    with open(out_path,'wb') as stream:
        for record in _merge(paths,key):
            stream.write(pack_record(record))

def reduce_runs(runs,workdir,key,fan_in,prefix):
    """Collapse runs in fan-in-sized merge levels until a single k-way merge.

    Returns the final list of run paths (length <= fan_in).  Intermediate
    files are deleted as soon as the level that consumed them is on disk.
    """
    current=list(runs)
    while len(current)>fan_in:
        next_level=[]
        for offset in range(0,len(current),fan_in):
            batch=current[offset:offset+fan_in]
            fd,out_path=tempfile.mkstemp(prefix=prefix,suffix='.run',dir=workdir)
            os.close(fd)
            try:
                _merge_to_file(batch,out_path,key)
            except BaseException:
                if os.path.exists(out_path):os.unlink(out_path)
                raise
            next_level.append(out_path)
        for consumed in current:
            if os.path.exists(consumed):os.unlink(consumed)
        current=next_level
    return current

def merged_stream(runs,key):
    """Final k-way merge as a streaming iterator (nothing is written)."""
    if not runs:return iter(())
    return _merge(runs,key)

class Spiller:
    """Bounded-memory collector: flushes sorted runs when a buffer fills."""

    def __init__(self,workdir,prefix,max_bytes,max_records,key):
        self.workdir=workdir;self.prefix=prefix;self.max_bytes=max_bytes
        self.max_records=max_records;self.key=key
        self.buffer=[];self.bytes=0;self.runs=[]

    def add(self,record):
        packed=pack_record(record)
        self.buffer.append(record);self.bytes+=len(packed)
        if self.bytes>=self.max_bytes or len(self.buffer)>=self.max_records:
            self.flush()

    def flush(self):
        if not self.buffer:return
        fd,path=tempfile.mkstemp(prefix=self.prefix,suffix='.run',dir=self.workdir)
        os.close(fd)
        try:
            write_run(path,self.buffer,self.key)
        except BaseException:
            if os.path.exists(path):os.unlink(path)
            raise
        self.runs.append(path)
        self.buffer=[];self.bytes=0
