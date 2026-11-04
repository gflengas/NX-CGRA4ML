// NG-ULTRA runtime simulator: runs the canonical C runtime (cg_app_main)
// against the CGRA4ML RTL in Verilator, in order: shared state, the DDR
// slave for the accelerator's AXI masters, nx_platform for the runtime
// (MMIO over AXI-Lite, the clock), the host side of the DDR mailbox, main.
#include <atomic>
#include <cerrno>
#include <cinttypes>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
#include <sys/mman.h>
#include <vector>

#include "Vcgra4ml_axi2ram_tb.h"
#include "cg_host_link.h"
#include "nx_ddr_layout.h"
#include "nx_platform.h"
#include "verilated.h"

extern "C" int cg_app_main(void);

namespace sim {

constexpr uint32_t kDdrBase = NX_DDR_WORKSPACE_BASE;
constexpr size_t kDdrBytes = NX_DDR_RESERVED_END - kDdrBase;

/** AXI-Lite slave outputs sampled after the falling edge. */
struct Lite {
    bool awready;
    bool wready;
    bool bvalid;
    uint32_t bresp;
    bool arready;
    bool rvalid;
    uint32_t rresp;
    uint32_t rdata;
    uint32_t bid;
    uint32_t rid;
    bool rlast;
};

Vcgra4ml_axi2ram_tb *dut;
uint8_t *ddr;
uint64_t cycles;
uint64_t mmio_reads;
uint64_t mmio_writes;
uint64_t pixel_reads;
uint64_t weight_reads;
uint64_t output_writes;
uint64_t packets;
uint64_t output_bursts;
FILE *trace_file;

[[noreturn]] void fail(const char *what, uint64_t value) {
    std::fprintf(
        stderr, "ngultra-sim: %s 0x%" PRIx64 " at cycle %" PRIu64 "\n", what, value, cycles);
    std::abort();
}

uint8_t *device_address(uint32_t address, size_t bytes) {
    if (address < kDdrBase || bytes > kDdrBytes
        || static_cast<uint64_t>(address) + bytes > static_cast<uint64_t>(kDdrBase) + kDdrBytes)
        fail("DDR address outside backing memory", address);
    return ddr + (address - kDdrBase);
}

} // namespace sim

// DDR slave for the accelerator's pixel, weight and output AXI masters.
//
// Default: one shared memory port that serves one beat per cycle in
// round-robin order, as the SoC's single DDR port does. Optional realistic
// slave (CGRA4ML_SIM_AXI_SEED): several outstanding bursts, latency before
// data, random ready/valid gaps, delayed write responses and, with
// CGRA4ML_SIM_AXI_READ_ORDER, the SoC's in-order read return.
namespace sim {
namespace {
unsigned next_service_client;
uint64_t service_grants;
uint64_t last_service_cycle;

struct ReadBurst {
    bool active = false;
    bool valid = false;
    bool last = false;
    uint32_t address = 0;
    uint32_t id = 0;
    unsigned remaining = 0;
    uint32_t data[4] = {};
};

struct WriteBurst {
    bool active = false;
    bool buffered = false;
    bool response = false;
    bool last = false;
    uint32_t address = 0;
    uint32_t id = 0;
    unsigned remaining = 0;
    uint32_t data[4] = {};
    uint32_t strobe = 0;
};

ReadBurst pixel_burst;
ReadBurst weight_burst;
WriteBurst write_burst;

// The realistic slave (see the top of the file); default runs never enter it.
bool realistic;
// Read data for both read ports leaves DDR in address-acceptance order,
// one beat per cycle, into a per-port buffer of read_buffer beats; a full
// buffer blocks the shared return path for both ports (head-of-line), as a
// shared in-order SoC return path with finite per-port buffering would.
bool read_order;
unsigned read_buffer = 16;
uint64_t rng_state;
unsigned outstanding_limit = 4;
unsigned latency_min = 8;
unsigned latency_max = 64;
unsigned ready_percent = 50;
uint64_t transaction_sequence;

struct PendingRead {
    uint32_t address;
    uint32_t id;
    unsigned unsent;
    uint64_t ready_cycle;
    uint64_t sequence;
    unsigned total; // beats of the burst
    unsigned moved = 0; // read_order: beats moved from DDR into the port buffer
};

struct ReadChannel {
    std::deque<PendingRead> queue;
    bool arready = false;
    bool valid = false;
    bool last = false;
    uint32_t id = 0;
    uint32_t data[4] = {};
    unsigned buffered = 0; // read_order: beats in the port buffer, not yet accepted
};

struct PendingWrite {
    uint32_t address;
    uint32_t id;
    unsigned remaining;
};

struct PendingResponse {
    uint32_t id;
    uint64_t ready_cycle;
};

ReadChannel pixel_channel;
ReadChannel weight_channel;
std::deque<PendingWrite> write_queue;
std::deque<PendingResponse> response_queue;
bool awready;
bool wready;
bool bvalid;

void load_beat(uint32_t address, uint32_t *words) {
    std::memcpy(words, device_address(address, 16), 16);
}

uint32_t burst_start(uint32_t address, unsigned length, unsigned size, unsigned burst) {
    if (size != 4 || burst != 1 || length > 15)
        fail("unsupported AXI burst shape", (uint64_t(size) << 32) | length);
    const uint32_t aligned = address & ~uint32_t(15);
    if ((aligned & 4095u) + (length + 1u) * 16u > 4096u)
        fail("AXI burst crosses 4 KiB", address);
    device_address(aligned, (length + 1u) * 16u);
    return aligned;
}

uint32_t next_random() {
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return uint32_t(rng_state >> 32);
}

bool chance() {
    return next_random() % 100u < ready_percent;
}

uint64_t ready_after_latency() {
    return cycles + latency_min + next_random() % (latency_max - latency_min + 1u);
}

void drive_read_channel(const ReadChannel &channel, CData &arready, CData &rvalid, CData &rlast,
    CData &rid, CData &rresp, VlWide<4> &rdata) {
    arready = channel.arready && channel.queue.size() < outstanding_limit;
    rvalid = channel.valid;
    rlast = channel.last;
    rid = channel.id;
    rresp = 0;
    std::memcpy(rdata, channel.data, 16);
}

void drive_realistic() {
    drive_read_channel(pixel_channel, dut->host_pixel_arready, dut->host_pixel_rvalid,
        dut->host_pixel_rlast, dut->host_pixel_rid, dut->host_pixel_rresp, dut->host_pixel_rdata);
    drive_read_channel(weight_channel, dut->host_weight_arready, dut->host_weight_rvalid,
        dut->host_weight_rlast, dut->host_weight_rid, dut->host_weight_rresp,
        dut->host_weight_rdata);
    dut->host_output_awready = awready && write_queue.size() < outstanding_limit;
    dut->host_output_wready = wready && !write_queue.empty();
    dut->host_output_bvalid = bvalid;
    dut->host_output_bid = bvalid ? response_queue.front().id : 0;
    dut->host_output_bresp = 0;
}

// read_order: move one beat of the globally earliest unfinished burst from DDR
// into its port's buffer, unless that buffer is full (which blocks both ports).
void ddr_return_step() {
    PendingRead *head = nullptr;
    ReadChannel *owner = nullptr;
    for (ReadChannel *channel : { &pixel_channel, &weight_channel })
        for (auto &read : channel->queue)
            if (read.moved < read.total) {
                if (!head || read.sequence < head->sequence) {
                    head = &read;
                    owner = channel;
                }
                break;
            }
    if (!head || cycles < head->ready_cycle || owner->buffered >= read_buffer)
        return;
    ++head->moved;
    ++owner->buffered;
}

void consume_read_beat(ReadChannel &channel) {
    if (!channel.valid || channel.queue.empty())
        fail("read response without data", 0);
    channel.valid = false;
    if (read_order)
        --channel.buffered;
    if (channel.last) {
        if (channel.queue.front().unsent)
            fail("read last before final beat", channel.queue.front().unsent);
        channel.queue.pop_front();
    }
    ++service_grants;
}

void accept_realistic_read(ReadChannel &channel, uint32_t address, unsigned length, unsigned size,
    unsigned kind, uint32_t id) {
    channel.queue.push_back({ burst_start(address, length, size, kind), id, length + 1u,
        ready_after_latency(), ++transaction_sequence, length + 1u });
}

void present_read_beat(ReadChannel &channel, const char *name) {
    if (channel.valid || channel.queue.empty())
        return;
    auto &burst = channel.queue.front();
    if (!burst.unsent || cycles < burst.ready_cycle || !chance())
        return;
    if (read_order && burst.moved <= burst.total - burst.unsent)
        return; // not in the buffer yet
    load_beat(burst.address, channel.data);
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " mem_grant_%s %08x last=%u\n", cycles, name,
            burst.address, burst.unsent == 1u);
    channel.id = burst.id;
    channel.last = burst.unsent == 1u;
    --burst.unsent;
    burst.address += 16u;
    channel.valid = true;
}

void realistic_edge() {
    if (!dut->rstn)
        return;
    const bool pixel_ar = dut->host_pixel_arvalid && dut->host_pixel_arready;
    const bool weight_ar = dut->host_weight_arvalid && dut->host_weight_arready;
    const bool output_aw = dut->host_output_awvalid && dut->host_output_awready;
    const bool output_w = dut->host_output_wvalid && dut->host_output_wready;
    const bool pixel_r = dut->host_pixel_rvalid && dut->host_pixel_rready;
    const bool weight_r = dut->host_weight_rvalid && dut->host_weight_rready;
    const bool output_b = dut->host_output_bvalid && dut->host_output_bready;

    if (pixel_r) {
        consume_read_beat(pixel_channel);
        ++pixel_reads;
    }
    if (weight_r) {
        consume_read_beat(weight_channel);
        ++weight_reads;
    }
    if (output_b) {
        if (!bvalid || response_queue.empty())
            fail("write response without request", 0);
        bvalid = false;
        response_queue.pop_front();
    }
    if (pixel_ar)
        accept_realistic_read(pixel_channel, dut->host_pixel_araddr, dut->host_pixel_arlen,
            dut->host_pixel_arsize, dut->host_pixel_arburst, dut->host_pixel_arid);
    if (weight_ar)
        accept_realistic_read(weight_channel, dut->host_weight_araddr, dut->host_weight_arlen,
            dut->host_weight_arsize, dut->host_weight_arburst, dut->host_weight_arid);
    if (output_aw) {
        write_queue.push_back({ burst_start(dut->host_output_awaddr, dut->host_output_awlen,
                                    dut->host_output_awsize, dut->host_output_awburst),
            dut->host_output_awid, unsigned(dut->host_output_awlen) + 1u });
        ++transaction_sequence;
    }
    if (output_w) {
        if (write_queue.empty())
            fail("write data without address", cycles);
        auto &write = write_queue.front();
        if (bool(dut->host_output_wlast) != (write.remaining == 1u))
            fail("AXI WLAST does not match AWLEN", write.remaining);
        auto *target = device_address(write.address, 16);
        uint32_t words[4];
        std::memcpy(words, dut->host_output_wdata, 16);
        const auto *source = reinterpret_cast<const uint8_t *>(words);
        for (unsigned byte = 0; byte < 16; ++byte)
            if (dut->host_output_wstrb & (1u << byte))
                target[byte] = source[byte];
        if (trace_file)
            std::fprintf(trace_file, "%" PRIu64 " mem_grant_output %08x strb=%04x last=%u\n",
                cycles, write.address, unsigned(dut->host_output_wstrb),
                unsigned(dut->host_output_wlast));
        ++output_writes;
        ++service_grants;
        write.address += 16u;
        if (--write.remaining == 0u) {
            response_queue.push_back({ write.id, ready_after_latency() });
            write_queue.pop_front();
        }
    }

    if (read_order)
        ddr_return_step();
    present_read_beat(pixel_channel, "pixel");
    present_read_beat(weight_channel, "weight");
    if (!bvalid && !response_queue.empty() && cycles >= response_queue.front().ready_cycle
        && chance())
        bvalid = true;

    pixel_channel.arready = chance();
    weight_channel.arready = chance();
    awready = chance();
    wready = chance();
}

void drive_shared_memory() {
    if (realistic) {
        drive_realistic();
        return;
    }
    dut->host_pixel_arready = !pixel_burst.active && !pixel_burst.valid;
    dut->host_pixel_rid = pixel_burst.id;
    std::memcpy(dut->host_pixel_rdata, pixel_burst.data, 16);
    dut->host_pixel_rresp = 0;
    dut->host_pixel_rlast = pixel_burst.last;
    dut->host_pixel_rvalid = pixel_burst.valid;

    dut->host_weight_arready = !weight_burst.active && !weight_burst.valid;
    dut->host_weight_rid = weight_burst.id;
    std::memcpy(dut->host_weight_rdata, weight_burst.data, 16);
    dut->host_weight_rresp = 0;
    dut->host_weight_rlast = weight_burst.last;
    dut->host_weight_rvalid = weight_burst.valid;

    dut->host_output_awready = !write_burst.active && !write_burst.response;
    dut->host_output_wready = !write_burst.buffered;
    dut->host_output_bid = write_burst.id;
    dut->host_output_bresp = 0;
    dut->host_output_bvalid = write_burst.response;
}

void accept_read_address(ReadBurst &burst, uint32_t address, unsigned length, unsigned size,
    unsigned kind, uint32_t id) {
    if (burst.active || burst.valid)
        fail("read burst overlap", address);
    burst.address = burst_start(address, length, size, kind);
    burst.id = id;
    burst.remaining = length + 1u;
    burst.active = true;
}

void consume_read(ReadBurst &burst) {
    if (!burst.valid)
        fail("read response without data", 0);
    burst.valid = false;
    if (burst.last) {
        if (burst.remaining)
            fail("read last before final service", burst.remaining);
        burst.active = false;
    }
}

void service_read(ReadBurst &burst, const char *name) {
    if (!burst.active || burst.valid || !burst.remaining)
        fail("read service without request", burst.address);
    load_beat(burst.address, burst.data);
    burst.last = burst.remaining == 1u;
    --burst.remaining;
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " mem_grant_%s %08x last=%u\n", cycles, name,
            burst.address, burst.last);
    burst.address += 16u;
}

void service_write() {
    if (!write_burst.active || !write_burst.buffered || !write_burst.remaining)
        fail("write service without request", write_burst.address);
    if (write_burst.last != (write_burst.remaining == 1u))
        fail("AXI WLAST does not match AWLEN", write_burst.remaining);
    auto *target = device_address(write_burst.address, 16);
    const auto *source = reinterpret_cast<const uint8_t *>(write_burst.data);
    for (unsigned byte = 0; byte < 16; ++byte)
        if (write_burst.strobe & (1u << byte))
            target[byte] = source[byte];
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " mem_grant_output %08x strb=%04x last=%u\n", cycles,
            write_burst.address, write_burst.strobe, write_burst.last);
    ++output_writes;
    write_burst.address += 16u;
    --write_burst.remaining;
    write_burst.buffered = false;
    if (write_burst.last) {
        write_burst.active = false;
        write_burst.response = true;
    }
}

void shared_edge() {
    if (realistic) {
        realistic_edge();
        return;
    }
    if (!dut->rstn)
        return;
    const bool pixel_ar = dut->host_pixel_arvalid && dut->host_pixel_arready;
    const bool weight_ar = dut->host_weight_arvalid && dut->host_weight_arready;
    const bool output_aw = dut->host_output_awvalid && dut->host_output_awready;
    const bool output_w = dut->host_output_wvalid && dut->host_output_wready;
    const bool pixel_r = dut->host_pixel_rvalid && dut->host_pixel_rready;
    const bool weight_r = dut->host_weight_rvalid && dut->host_weight_rready;
    const bool output_b = dut->host_output_bvalid && dut->host_output_bready;

    if (pixel_r)
        consume_read(pixel_burst);
    if (weight_r)
        consume_read(weight_burst);
    if (output_b)
        write_burst.response = false;
    if (pixel_ar)
        accept_read_address(pixel_burst, dut->host_pixel_araddr, dut->host_pixel_arlen,
            dut->host_pixel_arsize, dut->host_pixel_arburst, dut->host_pixel_arid);
    if (weight_ar)
        accept_read_address(weight_burst, dut->host_weight_araddr, dut->host_weight_arlen,
            dut->host_weight_arsize, dut->host_weight_arburst, dut->host_weight_arid);
    if (output_aw) {
        if (write_burst.active || write_burst.response)
            fail("write address overlap", dut->host_output_awaddr);
        write_burst.address = burst_start(dut->host_output_awaddr, dut->host_output_awlen,
            dut->host_output_awsize, dut->host_output_awburst);
        write_burst.id = dut->host_output_awid;
        write_burst.remaining = unsigned(dut->host_output_awlen) + 1u;
        write_burst.active = true;
    }
    if (output_w) {
        if (write_burst.buffered)
            fail("write buffer overflow", cycles);
        std::memcpy(write_burst.data, dut->host_output_wdata, 16);
        write_burst.strobe = dut->host_output_wstrb;
        write_burst.last = dut->host_output_wlast;
        write_burst.buffered = true;
    }

    const bool available[3] = { pixel_burst.active && !pixel_burst.valid && pixel_burst.remaining,
        weight_burst.active && !weight_burst.valid && weight_burst.remaining,
        write_burst.active && write_burst.buffered };
    const bool pending = available[0] || available[1] || available[2];
    if (!pending)
        return;
    for (unsigned offset = 0; offset < 3; ++offset) {
        const unsigned client = (next_service_client + offset) % 3u;
        if (!available[client])
            continue;
        if (last_service_cycle == cycles)
            fail("multiple DDR grants in one cycle", cycles);
        if (client == 0) {
            service_read(pixel_burst, "pixel");
            pixel_burst.valid = true;
            ++pixel_reads;
        } else if (client == 1) {
            service_read(weight_burst, "weight");
            weight_burst.valid = true;
            ++weight_reads;
        } else {
            service_write();
        }
        ++service_grants;
        last_service_cycle = cycles;
        next_service_client = (client + 1u) % 3u;
        return;
    }
    fail("pending service without eligible client", cycles);
}
} // namespace

int axi_memory_configure() {
    if (const char *seed = std::getenv("CGRA4ML_SIM_AXI_SEED")) {
        auto parse = [](const char *name, unsigned minimum, unsigned maximum, unsigned fallback) {
            const char *text = std::getenv(name);
            if (!text)
                return fallback;
            char *end = nullptr;
            const unsigned long value = std::strtoul(text, &end, 10);
            if (*end || value < minimum || value > maximum) {
                std::fprintf(stderr, "ngultra-sim: %s must be %u..%u\n", name, minimum, maximum);
                std::exit(2);
            }
            return unsigned(value);
        };
        char *end = nullptr;
        const unsigned long long parsed = std::strtoull(seed, &end, 10);
        if (*end || !parsed) {
            std::fprintf(stderr, "ngultra-sim: CGRA4ML_SIM_AXI_SEED must be a nonzero integer\n");
            return 2;
        }
        realistic = true;
        rng_state = parsed;
        outstanding_limit = parse("CGRA4ML_SIM_AXI_OUTSTANDING", 1, 64, outstanding_limit);
        latency_min = parse("CGRA4ML_SIM_AXI_LATENCY_MIN", 0, 10000, latency_min);
        latency_max = parse("CGRA4ML_SIM_AXI_LATENCY_MAX", latency_min, 10000, latency_max);
        ready_percent = parse("CGRA4ML_SIM_AXI_READY_PERCENT", 1, 100, ready_percent);
        read_order = parse("CGRA4ML_SIM_AXI_READ_ORDER", 0, 1, 0) != 0;
        read_buffer = parse("CGRA4ML_SIM_AXI_READ_BUFFER", 1, 4096, read_buffer);
        std::fprintf(stderr,
            "ngultra-sim: realistic AXI seed=%llu outstanding=%u latency=%u..%u "
            "ready=%u%% read_order=%d read_buffer=%u\n",
            parsed, outstanding_limit, latency_min, latency_max, ready_percent, int(read_order),
            read_buffer);
    }
    return 0;
}

void axi_memory_drive() {
    drive_shared_memory();
}

void axi_memory_edge() {
    shared_edge();
}

void axi_memory_report() {
    std::printf("ngultra-sim: shared_service_grants=%" PRIu64 "\n", service_grants);
    if (realistic)
        std::printf("ngultra-sim: realistic AXI transactions=%" PRIu64 "\n", transaction_sequence);
    if (service_grants != pixel_reads + weight_reads + output_writes)
        fail("shared grant accounting mismatch", service_grants);
}

} // namespace sim

// nx_platform for the runtime in simulation: MMIO reads and writes become
// LLPP transactions on the testbench, each poll advances the clock, and the
// tick counter is the cycle count.
// The LLPP slave (rtl/ngultra_llpp_regs.v) sees the CPU address and returns a
// changing transaction ID on B and R, both checked here.
namespace sim {
namespace {
constexpr uint32_t kConfigBase = 0x0d000000u;
// The LLPP slave (rtl/ngultra_llpp_regs.v) sees the CPU address and returns a
// changing transaction ID on B and R, both checked here.
uint32_t next_id = 0;
#ifndef CGRA4ML_SIM_CYCLE_LIMIT
#define CGRA4ML_SIM_CYCLE_LIMIT 20000000u
#endif
static_assert(CGRA4ML_SIM_CYCLE_LIMIT > 0 && CGRA4ML_SIM_CYCLE_LIMIT <= 1000000000u,
    "simulation cycle limit must be 1..1000000000");
constexpr uint64_t kCycleLimit = CGRA4ML_SIM_CYCLE_LIMIT;

void check_mmio(uint32_t address) {
    if (address < kConfigBase || address >= kConfigBase + 0x1000u || (address & 3u))
        fail("invalid MMIO address", address);
}
} // namespace

Lite tick() {
    if (++cycles > kCycleLimit)
        fail("simulation cycle watchdog", cycles);
    if (cycles == 100000u || cycles == 1000000u || cycles == 10000000u)
        std::fprintf(stderr,
            "ngultra-sim: progress cycle=%" PRIu64 " mmio_read=%" PRIu64 " mmio_write=%" PRIu64
            "\n",
            cycles, mmio_reads, mmio_writes);
    axi_memory_drive();
    dut->clk = 0;
    dut->eval();
    Lite lite{ bool(dut->s_llpp_awready), bool(dut->s_llpp_wready), bool(dut->s_llpp_bvalid),
        uint32_t(dut->s_llpp_bresp), bool(dut->s_llpp_arready), bool(dut->s_llpp_rvalid),
        uint32_t(dut->s_llpp_rresp), uint32_t(dut->s_llpp_rdata), uint32_t(dut->s_llpp_bid),
        uint32_t(dut->s_llpp_rid), bool(dut->s_llpp_rlast) };

    if (dut->dbg_pixel_ar && trace_file)
        std::fprintf(trace_file, "%" PRIu64 " pixel_ar %08x len=%u\n", cycles,
            dut->dbg_pixel_araddr, dut->dbg_pixel_arlen);
    if (dut->dbg_weight_ar && trace_file)
        std::fprintf(trace_file, "%" PRIu64 " weight_ar %08x len=%u\n", cycles,
            dut->dbg_weight_araddr, dut->dbg_weight_arlen);
    if (dut->dbg_output_aw && trace_file)
        std::fprintf(trace_file, "%" PRIu64 " output_aw %08x len=%u\n", cycles,
            dut->dbg_output_awaddr, dut->dbg_output_awlen);
    if (dut->dbg_pixel_r) {
        if (dut->dbg_pixel_rresp)
            fail("pixel AXI read response", dut->dbg_pixel_rresp);
        if (trace_file)
            std::fprintf(trace_file, "%" PRIu64 " pixel_r last=%u\n", cycles, dut->dbg_pixel_rlast);
    }
    if (dut->dbg_weight_r) {
        if (dut->dbg_weight_rresp)
            fail("weight AXI read response", dut->dbg_weight_rresp);
        if (trace_file)
            std::fprintf(
                trace_file, "%" PRIu64 " weight_r last=%u\n", cycles, dut->dbg_weight_rlast);
    }
    if (dut->dbg_output_w) {
        if (dut->dbg_output_wlast)
            ++output_bursts;
        if (trace_file)
            std::fprintf(
                trace_file, "%" PRIu64 " output_w last=%u\n", cycles, dut->dbg_output_wlast);
    }
    if (dut->dbg_output_b) {
        if (dut->dbg_output_bresp)
            fail("output AXI write response", dut->dbg_output_bresp);
        if (trace_file)
            std::fprintf(
                trace_file, "%" PRIu64 " output_b resp=%u\n", cycles, dut->dbg_output_bresp);
    }

    axi_memory_edge();
    dut->clk = 1;
    dut->eval();
    dut->eval();
    return lite;
}

} // namespace sim

using namespace sim;

extern "C" uint32_t nx_mmio_read(uint32_t address) {
    check_mmio(address);
    ++mmio_reads;
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " mmio_read %08x\n", cycles, address);
    const uint32_t id = (next_id = (next_id * 1103515245u + 12345u)) >> 20;
    dut->s_llpp_araddr = address;
    dut->s_llpp_arid = id;
    dut->s_llpp_arlen = 0;
    dut->s_llpp_arvalid = 1;
    dut->s_llpp_rready = 1;
    bool accepted = false;
    for (unsigned budget = 0; budget < 100000; ++budget) {
        Lite state = tick();
        if (state.arready && !accepted) {
            accepted = true;
            dut->s_llpp_arvalid = 0;
        }
        if (state.rvalid) {
            if (!accepted || state.rresp)
                fail("LLPP read response", state.rresp);
            if (state.rid != id || !state.rlast)
                fail("LLPP read ID/last", state.rid);
            dut->s_llpp_rready = 0;
            if (trace_file)
                std::fprintf(
                    trace_file, "%" PRIu64 " mmio_value %08x=%08x\n", cycles, address, state.rdata);
            return state.rdata;
        }
    }
    fail("LLPP read timeout", address);
}

extern "C" void nx_mmio_write(uint32_t address, uint32_t value) {
    check_mmio(address);
    ++mmio_writes;
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " mmio_write %08x=%08x\n", cycles, address, value);
    const uint32_t id = (next_id = (next_id * 1103515245u + 12345u)) >> 20;
    dut->s_llpp_awaddr = address;
    dut->s_llpp_awid = id;
    dut->s_llpp_awlen = 0;
    dut->s_llpp_wlast = 1;
    dut->s_llpp_awvalid = 1;
    dut->s_llpp_wdata = value;
    dut->s_llpp_wstrb = 15;
    dut->s_llpp_wvalid = 1;
    dut->s_llpp_bready = 1;
    bool address_done = false;
    bool data_done = false;
    for (unsigned budget = 0; budget < 100000; ++budget) {
        Lite state = tick();
        if (state.awready && !address_done) {
            address_done = true;
            dut->s_llpp_awvalid = 0;
        }
        if (state.wready && !data_done) {
            data_done = true;
            dut->s_llpp_wvalid = 0;
        }
        if (state.bvalid) {
            if (!address_done || !data_done || state.bresp)
                fail("LLPP write response", state.bresp);
            if (state.bid != id)
                fail("LLPP write ID", state.bid);
            dut->s_llpp_bready = 0;
            return;
        }
    }
    fail("LLPP write timeout", address);
}

extern "C" void nx_sim_cache_access(const void *address, size_t bytes) {
    const auto numeric = reinterpret_cast<uintptr_t>(address);
    if (numeric > UINT32_MAX)
        fail("host pointer exceeds device address", numeric);
    device_address(uint32_t(numeric), bytes);
    // CPU and DMA share one coherent backing store (no cache model): a cache
    // operation only checks that its range is inside DDR.
}

extern "C" void nx_barrier(void) {
    std::atomic_signal_fence(std::memory_order_seq_cst);
}

extern "C" void nx_simd_enable(void) {
}

extern "C" void nx_sim_event(const char *name, uint32_t first, uint32_t second) {
    if (!std::strcmp(name, "acquire"))
        ++packets;
    if (trace_file)
        std::fprintf(trace_file, "%" PRIu64 " runtime_%s %08x %08x\n", cycles, name, first, second);
}

extern "C" void nx_putc(char value) {
    std::putchar(value);
}
extern "C" void nx_counter_reset(void) {
}
extern "C" void nx_counter_start(void) {
}
extern "C" void nx_counter_stop(void) {
}
extern "C" uint64_t nx_ticks(void) {
    return cycles;
}

// Host side of the DDR mailbox protocol, as cgtools load plays it on the
// board: BEGIN after a ready status, the payload (W/B and input, later only the
// input) with its CRC, and for each result: save it, run the result command
// (cgtools load --saved-result, which compares the golden), acknowledge.
// Protocol faults are covered by test/test_ngultra_host_link.py.
// Mailbox polls before the emulated host answers (idle before BEGIN, transfer).
using namespace sim;

namespace {
// Mailbox polls before the emulated host answers (idle before BEGIN, transfer).
constexpr unsigned long kHostDelayPolls = 2;
} // namespace

extern "C" void nx_sim_external_poll(uint32_t poll) {
    static std::vector<uint8_t> parameters;
    // Host transport only: the runtime accepts or rejects the requests.
    tick();
    auto *mailbox =
        reinterpret_cast<cg_mailbox_t *>(device_address(NX_DDR_MAILBOX_BASE, sizeof(cg_mailbox_t)));
    if (mailbox->status.state == CG_LOAD_READY) {
        if (poll != kHostDelayPolls)
            return;
        const char *count_text = std::getenv("CGRA4ML_SIM_REQUEST_COUNT");
        if (!count_text)
            fail("host request count missing", 0);
        const unsigned long count = std::strtoul(count_text, nullptr, 10);
        if (!count || count > UINT32_MAX)
            fail("host request count invalid", count);
        cg_load_request_t begin = {};
        begin.command = CG_LOAD_BEGIN;
        begin.sequence = mailbox->status.sequence;
        begin.address = mailbox->status.address;
        begin.bytes = mailbox->status.bytes;
        begin.session_requests = count;
        std::memcpy(begin.identity, mailbox->status.identity, sizeof(begin.identity));
        mailbox->request = begin;
        nx_sim_event("external_begin", begin.sequence, begin.session_requests);
        return;
    }
    if (poll != kHostDelayPolls)
        return;
    if (mailbox->status.state == CG_LOAD_RESULT) {
        const auto &status = mailbox->status;
        if (parameters.size() != status.parameter_bytes
            || std::memcmp(parameters.data(),
                device_address(NX_DDR_PAYLOAD_BASE, parameters.size()), parameters.size()))
            fail("resident parameters changed", status.sequence);
        const size_t count = size_t(status.result_words) * sizeof(uint32_t);
        const uint8_t *result = device_address(status.result_address, count);
        const char *directory = std::getenv("CGRA4ML_SIM_RESULT_DIR");
        if (!directory)
            fail("result directory missing", 0);
        const std::string path =
            std::string(directory) + "/result-" + std::to_string(status.sequence) + ".bin";
        FILE *output = std::fopen(path.c_str(), "wb");
        if (!output || std::fwrite(result, 1, count, output) != count)
            fail("cannot save external result", status.sequence);
        if (std::fclose(output))
            fail("cannot close external result", status.sequence);
        nx_sim_event("external_result", status.sequence, status.result_words);
        const std::string variable =
            "CGRA4ML_SIM_RESULT_COMMAND_" + std::to_string(status.sequence);
        const char *command = std::getenv(variable.c_str());
        if (!command || std::system(command) != 0)
            fail("host result consumption failed", status.sequence);
        cg_load_request_t ack = {};
        ack.sequence = status.sequence;
        ack.session_requests = status.session_requests;
        std::memcpy(ack.identity, status.identity, sizeof(ack.identity));
        mailbox->request = ack;
        mailbox->request.command = CG_RESULT_CONSUMED;
        nx_sim_event("external_consumed", ack.sequence, mailbox->request.command);
        return;
    }
    if (mailbox->status.state != CG_LOAD_RECEIVING)
        fail("host write without released input", mailbox->status.state);
    const char *path = std::getenv("CGRA4ML_SIM_EXTERNAL_PAYLOAD");
    if (mailbox->status.sequence > 1u) {
        const std::string variable =
            "CGRA4ML_SIM_INPUT_" + std::to_string(mailbox->status.sequence);
        path = std::getenv(variable.c_str());
    }
    if (!path)
        fail("external payload file missing", 0);
    FILE *file = std::fopen(path, "rb");
    if (!file)
        fail("cannot open external payload", 0);
    if (std::fseek(file, 0, SEEK_END))
        fail("payload seek failed", 0);
    const long length = std::ftell(file);
    if (length <= 0 || static_cast<unsigned long>(length) > NX_DDR_PAYLOAD_CAPACITY)
        fail("external file outside reservation", length);
    std::rewind(file);
    std::vector<uint8_t> bytes(static_cast<size_t>(length));
    if (std::fread(bytes.data(), 1, bytes.size(), file) != bytes.size())
        fail("payload file read failed", bytes.size());
    std::fclose(file);
    if (mailbox->status.sequence == 1u) {
        if (mailbox->status.parameter_bytes > bytes.size())
            fail("parameter range exceeds file", 0);
        parameters.assign(bytes.begin(), bytes.begin() + mailbox->status.parameter_bytes);
    }
    cg_load_request_t request = {};
    request.sequence = mailbox->status.sequence;
    request.session_requests = mailbox->status.session_requests;
    request.address = mailbox->status.address;
    request.bytes = bytes.size();
    request.crc32 = cg_crc32(bytes.data(), bytes.size());
    std::memcpy(request.identity, mailbox->status.identity, sizeof(request.identity));
    std::memcpy(device_address(mailbox->status.address, bytes.size()), bytes.data(), bytes.size());
    nx_sim_event("external_write", mailbox->status.address, bytes.size());
    mailbox->request = request;
    mailbox->request.command = CG_LOAD_COMPLETE;
    nx_sim_event("external_complete", request.sequence, request.bytes);
}

using namespace sim;

int main(int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    if (const int status = axi_memory_configure())
        return status;
    if (const char *path = std::getenv("CGRA4ML_SIM_TRACE_FILE")) {
        trace_file = std::fopen(path, "w");
        if (!trace_file) {
            std::fprintf(stderr, "ngultra-sim: cannot open trace: %s\n", path);
            return 2;
        }
        if (std::setvbuf(trace_file, nullptr, _IOLBF, 0) != 0) {
            std::fprintf(stderr, "ngultra-sim: cannot line-buffer trace: %s\n", path);
            std::fclose(trace_file);
            return 2;
        }
    }
    void *mapped = mmap(reinterpret_cast<void *>(uintptr_t(kDdrBase)), kDdrBytes,
        PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (mapped == MAP_FAILED || uintptr_t(mapped) != kDdrBase) {
        std::fprintf(stderr, "ngultra-sim: DDR mmap failed: %s\n", std::strerror(errno));
        return 2;
    }
    ddr = static_cast<uint8_t *>(mapped);
    dut = new Vcgra4ml_axi2ram_tb;
    dut->rstn = 0;
    dut->s_llpp_awvalid = 0;
    dut->s_llpp_wvalid = 0;
    dut->s_llpp_bready = 0;
    dut->s_llpp_arvalid = 0;
    dut->s_llpp_rready = 0;
    dut->s_llpp_awid = 0;
    dut->s_llpp_awlen = 0;
    dut->s_llpp_wlast = 1;
    dut->s_llpp_arid = 0;
    dut->s_llpp_arlen = 0;
    for (unsigned i = 0; i < 4; ++i)
        tick();
    dut->rstn = 1;
    for (unsigned i = 0; i < 4; ++i)
        tick();
    const int result = cg_app_main();
    std::printf("ngultra-sim: cycles=%" PRIu64 " mmio_read=%" PRIu64 " mmio_write=%" PRIu64
                " pixel_beats=%" PRIu64 " weight_beats=%" PRIu64 " output_beats=%" PRIu64
                " packets=%" PRIu64 " output_bursts=%" PRIu64 "\n",
        cycles, mmio_reads, mmio_writes, pixel_reads, weight_reads, output_writes, packets,
        output_bursts);
    axi_memory_report();
    if (trace_file)
        std::fclose(trace_file);
    dut->final();
    delete dut;
    munmap(mapped, kDdrBytes);
    return result;
}
