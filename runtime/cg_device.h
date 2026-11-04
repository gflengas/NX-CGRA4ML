/* CGRA4ML accelerator registers on NG-ULTRA (LLPP register slave, 4 KB
 * window). The launch protocol is in main.c. */
#ifndef CG_DEVICE_H
#define CG_DEVICE_H

#include <stdint.h>

#include "cg_config.h"
#include "nx_log.h"
#include "nx_platform.h"

#define CG_CONFIG_BASE 0x0d000000u

enum {
    REG_START = 0x0,
    REG_DONE_READ0 = 0x1,
    REG_DONE_READ1 = 0x2,
    REG_DONE_WRITE0 = 0x3,
    REG_DONE_WRITE1 = 0x4,
    REG_OCM_BASE0 = 0x5,
    REG_OCM_BASE1 = 0x6,
    REG_WEIGHTS_BASE = 0x7,
    REG_BUNDLE_DONE = 0x8,
    REG_BUNDLE_COUNT = 0x9,
    REG_WEIGHTS_DONE = 0xa,
    REG_PIXELS_DONE = 0xb,
    REG_OUTPUT_DONE = 0xc,
    /* Eight words per bundle up to the end of the window. */
    REG_BUNDLE_TABLE = 0x10,
    REG_WINDOW_WORDS = 0x400,
};

/* Polls of one register before a wait traps. Board: about 1e8 MMIO reads
 * (seconds); debug builds wait ten times longer to reach the snapshot of a
 * slow stall. Simulation polls advance the RTL, so the bound is in cycles. */
#ifndef CG_POLL_LIMIT
#if defined(NX_SIM)
#define CG_POLL_LIMIT 100000u
#elif CG_DEBUG
#define CG_POLL_LIMIT 1000000000u
#else
#define CG_POLL_LIMIT 100000000u
#endif
#endif
#if CG_POLL_LIMIT < 1 || CG_POLL_LIMIT > 1000000000
#error "CG_POLL_LIMIT must be 1..1000000000"
#endif

/* One read between barriers: the register slave returns the current value. */
static uint32_t read_reg(uint32_t word) {
    nx_barrier();
    uint32_t value = nx_mmio_read(CG_CONFIG_BASE + word * 4);
    nx_barrier();
    return value;
}

static void write_reg(uint32_t word, uint32_t value) {
    nx_mmio_write(CG_CONFIG_BASE + word * 4, value);
    nx_barrier();
}

static void wait_reg(uint32_t word, uint32_t value) {
    for (uint32_t poll = 0; poll < CG_POLL_LIMIT; ++poll) {
        if (read_reg(word) == value) {
            return;
        }
    }
    if (CG_LOG_LEVEL >= 1) {
        nx_puts("log: register_snapshot registers=");
        for (uint32_t reg = REG_START; reg <= REG_OUTPUT_DONE; ++reg) {
            nx_puts(reg == REG_START ? "" : ",");
            nx_put_hex32(read_reg(reg));
        }
        nx_puts("\r\n");
    }
    nx_puts("device: timeout reg=");
    nx_put_hex32(word);
    nx_puts(" expected=");
    nx_put_hex32(value);
    nx_puts(" actual=");
    nx_put_hex32(read_reg(word));
    nx_puts("\r\n");
    __builtin_trap();
}

#endif
