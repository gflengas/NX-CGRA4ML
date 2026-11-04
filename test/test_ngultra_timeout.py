"""Exercise actual register waits, bounded overrides and timeout-only diagnostics."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
PLATFORM = ROOT / 'ngultra/c'

class TimeoutTests(unittest.TestCase):
    def build_run(self, limit, delay=1000, diagnostics=0, compile_only=False):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            source = d / 'test.c'
            source.write_text('''#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/resource.h>
static unsigned reads;
uint32_t nx_mmio_read(uint32_t address) {(void)address; return ++reads <= DELAY_READS;}
void nx_mmio_write(uint32_t address,uint32_t value) {(void)address;(void)value;_Exit(91);}
void nx_sim_cache_access(const void *address,size_t bytes) {(void)address;(void)bytes;_Exit(92);}
void nx_barrier(void) {}
void nx_sim_event(const char *name,uint32_t a,uint32_t b) {(void)name;(void)a;(void)b;}
void nx_putc(char c) {putchar(c);}
uint64_t nx_ticks(void) {return 0;}
#include "cg_device.h"
int main(void) {
 struct rlimit zero={0,0};setrlimit(RLIMIT_CORE,&zero);setvbuf(stdout,NULL,_IONBF,0);
 wait_reg(REG_DONE_READ0,0);printf("ready reads=%u\\n",reads);return 0;
}
''')
            result = subprocess.run(['gcc', '-std=c11', '-O2', '-DNX_SIM=1', f'-I{RUNTIME}', f'-I{PLATFORM}',
                                     f'-DCG_POLL_LIMIT={limit}', f'-DDELAY_READS={delay}',
                                     f'-DCG_MODE={2 if diagnostics else 0}',
                                     *([f'-DCG_LOG_LEVEL={diagnostics}'] if diagnostics else []),
                                     str(source),
                                     '-o', str(d / 'test')], capture_output=True, text=True)
            if compile_only:
                return result
            self.assertEqual(result.returncode, 0, result.stderr)
            return subprocess.run([str(d / 'test')], capture_output=True, text=True)

    def test_release_timeout_still_traps_without_log(self):
        result = self.build_run(3)
        self.assertLess(result.returncode, 0)
        self.assertIn('device: timeout reg=0x00000001', result.stdout)
        self.assertNotIn('log:', result.stdout)

    def test_longer_bound_accepts_delayed_handshake(self):
        # One register read per poll (LLPP slave).
        self.assertLess(self.build_run(6, delay=6).returncode, 0)
        result = self.build_run(7, delay=6)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'ready reads=7\n')

    def test_debug_snapshot_precedes_timeout_without_register_writes(self):
        result = self.build_run(3, diagnostics=1)
        self.assertLess(result.returncode, 0)
        self.assertIn('registers=' + ','.join(['0x00000001'] * 13), result.stdout)
        self.assertLess(result.stdout.index('log: register_snapshot'),
                        result.stdout.index('device: timeout'))

    def test_invalid_bounds_and_log_levels_rejected(self):
        for limit in [0, -1, 1000000001]:
            self.assertNotEqual(self.build_run(limit, compile_only=True).returncode, 0)
        self.assertNotEqual(self.build_run(3, diagnostics=4, compile_only=True).returncode, 0)

if __name__ == '__main__':
    unittest.main()
