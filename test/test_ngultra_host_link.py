"""Host-only test of the DDR mailbox protocol in runtime/main.c.

A scripted host (the loader's role) drives the real C code through a clean
two-request session and through 14 protocol faults; each fault must trap with
its own message instead of running inference on bad data. No Verilator or
accelerator is involved. Uses the example ResNet (ngultra/example/resnet) for a
real model header and payload.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cgtools.host_link import assemble_input  # noqa: E402
from cgtools.fixture import generate  # noqa: E402

FIXTURE = ROOT / "ngultra/example/resnet"
FAULTS = {
    "missing": "load timeout",
    "corrupt": "payload CRC mismatch",
    "truncated": "invalid payload range or length",
    "identity": "identity mismatch",
    "parameters": "parameter CRC mismatch",
    "overlap": "invalid payload range or length",
    "sequence": "invalid command or sequence",
    "ack-sequence": "invalid command or sequence",
    "duplicate-start": "invalid command or sequence",
    "missing-ack": "result timeout",
    "invalid-count": "invalid session count",
    "session-count": "session count mismatch",
    "early-finish": "invalid command or sequence",
    "duplicate-begin": "invalid command or sequence",
}

HOST_C = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>

#include "main.c"

static const char *fault;
static unsigned char *payload[3];
static size_t payload_bytes[3];
static unsigned char parameters[MODEL_WB_BYTES];

uint32_t nx_mmio_read(uint32_t address) { (void)address; _Exit(91); }
void nx_mmio_write(uint32_t address, uint32_t value) { (void)address; (void)value; _Exit(91); }
void nx_barrier(void) {}
void nx_simd_enable(void) {}
void nx_putc(char value) { putchar(value); }
uint64_t nx_ticks(void) { return 0; }
void nx_counter_reset(void) {}
void nx_counter_start(void) {}
void nx_counter_stop(void) {}
void nx_sim_cache_access(const void *address, size_t bytes) { (void)address; (void)bytes; }
void nx_sim_event(const char *name, uint32_t first, uint32_t second) {
  (void)name; (void)first; (void)second;
}

static int is(const char *name) { return strcmp(fault, name) == 0; }

static uint32_t crc32(const unsigned char *data, size_t bytes) {
  uint32_t crc = UINT32_MAX;
  for (size_t i = 0; i < bytes; ++i) {
    crc ^= data[i];
    for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
  }
  return ~crc;
}

/* The loader's side of the protocol, two polls after each state change. */
void nx_sim_external_poll(uint32_t poll) {
  cg_mailbox_t *mailbox = (cg_mailbox_t *)(uintptr_t)NX_DDR_MAILBOX_BASE;
  cg_load_status_t *status = &mailbox->status;
  cg_load_request_t request;
  memset(&request, 0, sizeof(request));
  request.sequence = status->sequence;
  memcpy(request.identity, status->identity, sizeof(request.identity));
  if (status->state == CG_LOAD_READY) {
    if (poll != 2u) return;
    request.command = CG_LOAD_BEGIN;
    request.address = status->address;
    request.bytes = status->bytes;
    request.session_requests = 2u;
    if (is("invalid-count")) request.session_requests = 0u;
    if (is("session-count") && request.sequence > 1u) ++request.session_requests;
    mailbox->request = request;
    return;
  }
  if (is("missing") && status->state == CG_LOAD_RECEIVING) return;
  if (poll != 2u) return;
  if (status->state == CG_LOAD_RESULT) {
    if (memcmp(parameters, cg_payload, MODEL_WB_BYTES) != 0) _Exit(93);
    if (is("missing-ack")) return;
    request.session_requests = status->session_requests;
    if (is("ack-sequence")) --request.sequence;
    request.command = is("duplicate-start") ? CG_LOAD_COMPLETE : CG_RESULT_CONSUMED;
    if (is("early-finish")) request.command = CG_SESSION_COMPLETE;
    mailbox->request = request;
    return;
  }
  if (status->state != CG_LOAD_RECEIVING || status->sequence > 2u) _Exit(94);
  size_t bytes = payload_bytes[status->sequence];
  unsigned char *data = malloc(bytes);
  memcpy(data, payload[status->sequence], bytes);
  if (is("parameters")) data[0] ^= 1u;
  request.session_requests = status->session_requests;
  request.address = status->address;
  request.bytes = (uint32_t)bytes;
  request.crc32 = crc32(data, bytes);
  if (is("corrupt")) data[bytes - 1u] ^= 1u;
  if (is("truncated")) { --bytes; --request.bytes; }
  if (is("identity")) request.identity[0] ^= 1u;
  if (is("overlap")) request.address = NX_DDR_WORKSPACE_BASE;
  if (is("sequence")) --request.sequence;
  memcpy((void *)(uintptr_t)status->address, data, bytes);
  free(data);
  if (status->sequence == 1u) memcpy(parameters, payload[1], MODEL_WB_BYTES);
  request.command = is("duplicate-begin") ? CG_LOAD_BEGIN : CG_LOAD_COMPLETE;
  mailbox->request = request;
}

static unsigned char *read_file(const char *path, size_t *bytes) {
  FILE *file = fopen(path, "rb");
  if (!file || fseek(file, 0, SEEK_END)) _Exit(95);
  *bytes = (size_t)ftell(file);
  rewind(file);
  unsigned char *data = malloc(*bytes);
  if (fread(data, 1, *bytes, file) != *bytes) _Exit(95);
  fclose(file);
  return data;
}

int main(int argc, char **argv) {
  struct rlimit zero = {0, 0};
  setrlimit(RLIMIT_CORE, &zero);
  setvbuf(stdout, NULL, _IONBF, 0);
  if (argc != 4) return 2;
  fault = argv[1];
  payload[1] = read_file(argv[2], &payload_bytes[1]);
  payload[2] = read_file(argv[3], &payload_bytes[2]);
  if (mmap((void *)(uintptr_t)NX_DDR_WORKSPACE_BASE,
           NX_DDR_RESERVED_END - NX_DDR_WORKSPACE_BASE, PROT_READ | PROT_WRITE,
           MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0) == MAP_FAILED) return 3;
  session_requests = 0;
  for (uint32_t sequence = 1u; ; ++sequence) {
    cg_host_receive_payload(sequence);
    cg_host_publish_result(sequence);
    if (cg_host_session_complete(sequence)) break;
  }
  puts("SESSION OK");
  return 0;
}
'''


class HostLinkProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="ngultra-host-link-")
        work = Path(cls.directory.name)
        generate(FIXTURE / "export/vectors", work / "model.h", FIXTURE / "export/hardware.json",
                 work / "scheduled_wb.bin")
        manifest = json.loads((work / "scheduled_wb.bin.json").read_text())
        parameters = (work / "scheduled_wb.bin").read_bytes()
        (work / "request-1.bin").write_bytes(
            assemble_input(parameters, manifest, FIXTURE / "inputs/C.bin", first=True))
        (work / "request-2.bin").write_bytes(
            assemble_input(parameters, manifest, FIXTURE / "inputs/D.bin", first=False))
        (work / "host.c").write_text(HOST_C)
        subprocess.run(["gcc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-DNX_SIM=1",
                        "-I", str(work), "-I", str(ROOT / "runtime"), "-I", str(ROOT / "ngultra/c"),
                        str(work / "host.c"), str(work / "model.c"), "-lm",
                        "-o", str(work / "host")], check=True)
        cls.work = work

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_host(self, fault):
        return subprocess.run([str(self.work / "host"), fault, str(self.work / "request-1.bin"),
                               str(self.work / "request-2.bin")],
                              capture_output=True, text=True, timeout=60)

    def test_clean_session_completes(self):
        result = self.run_host("none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("external: session complete requests=2", result.stdout)
        self.assertIn("SESSION OK", result.stdout)

    def test_each_fault_traps_with_its_message(self):
        for fault, message in FAULTS.items():
            with self.subTest(fault=fault):
                result = self.run_host(fault)
                self.assertEqual(result.returncode, -4, result.stdout[-400:])
                self.assertIn(f"external: {message}", result.stdout)


if __name__ == "__main__":
    unittest.main()
