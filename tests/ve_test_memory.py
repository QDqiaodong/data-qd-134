"""Resource-bound tests driven by the standalone memory probe.

The probe runs in its own process so ru_maxrss measures the pipeline end to
end (generator included, and the generator itself streams rather than
materializing rows).  The default checked scale keeps the suite fast while
still forcing many external-sort levels; VE_MEMORY_FULL_SCALE=1 runs the
contractual 5,000,000-row case.
"""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PEAK_LIMIT_MIB=128

class MemoryBoundTests(unittest.TestCase):
    def _probe(self,rows,buffer_bytes=2_000_000,buffer_records=10_000,fan_in=5):
        with tempfile.TemporaryDirectory() as workdir:
            result=subprocess.run(
                [sys.executable,str(ROOT/'tests'/'ve_memory_probe.py'),
                 '--rows',str(rows),'--workdir',workdir,
                 '--buffer-bytes',str(buffer_bytes),
                 '--buffer-records',str(buffer_records),'--fan-in',str(fan_in)],
                capture_output=True,text=True,cwd=ROOT)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_peak_memory_under_limit_and_flat_across_scales(self):
        small=self._probe(60_000)
        large=self._probe(300_000)
        self.assertLess(small['peak_rss_mib'],PEAK_LIMIT_MIB)
        self.assertLess(large['peak_rss_mib'],PEAK_LIMIT_MIB)
        # Five times the rows with identical buffers: resident memory must not
        # grow proportionally.  A generous allowance covers allocator drift.
        self.assertLess(large['peak_rss_mib'],small['peak_rss_mib']*2+24)
        self.assertEqual(large['leftover_run_files'],0)

    @unittest.skipUnless(os.environ.get('VE_MEMORY_FULL_SCALE')=='1',
                         'set VE_MEMORY_FULL_SCALE=1 for the 5M-row contractual run')
    def test_full_scale_five_million_rows(self):
        stats=self._probe(5_000_000,buffer_bytes=8_000_000,buffer_records=50_000,fan_in=8)
        self.assertLess(stats['peak_rss_mib'],PEAK_LIMIT_MIB)
        self.assertEqual(stats['leftover_run_files'],0)
        self.assertGreaterEqual(stats['rows_written'],5_000_000)

if __name__=='__main__':unittest.main()
