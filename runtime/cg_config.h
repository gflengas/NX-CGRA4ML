/* Build mode of the CGRA4ML NG-ULTRA runtime and its diagnostics.
 *
 * One switch, CG_MODE (Makefile: MODE=release|profile|debug):
 *   release  results and the total time only
 *   profile  release plus per-stage timers
 *   debug    debug log; golden validation (CG_VALIDATE, cg_workspace.h) when
 *            the generated model carries goldens
 * CG_LOG_LEVEL refines the debug log: 1 progress and timeout snapshots
 * (default), 2 launches, 3 raw output packets.
 *
 * Power measurement (release builds, Makefile: CG_POWER_MS, CG_POWER_IDLE_MS,
 * CG_POWER_WARMUP_MS, CG_POWER_CYCLES): after the first request's payload
 * arrives, the runtime repeats that inference for CG_POWER_WARMUP_MS (warm-up,
 * not measured), then alternates CG_POWER_CYCLES times an idle window of
 * CG_POWER_IDLE_MS and an active window of at least CG_POWER_MS (the inference
 * back to back), printing `power:` markers around every window, before the
 * reported run.
 *
 * Asserts stay on in every mode (the BSP's handler prints the failure and
 * halts).
 */
#ifndef CG_CONFIG_H
#define CG_CONFIG_H

#undef NDEBUG
#include <assert.h>
#include <stdint.h>

#include "nx_log.h"
#include "nx_platform.h"

#define CG_MODE_RELEASE 0
#define CG_MODE_PROFILE 1
#define CG_MODE_DEBUG 2
#ifndef CG_MODE
#define CG_MODE CG_MODE_RELEASE
#endif
#if CG_MODE != CG_MODE_RELEASE && CG_MODE != CG_MODE_PROFILE && CG_MODE != CG_MODE_DEBUG
#error "CG_MODE must be CG_MODE_RELEASE, CG_MODE_PROFILE or CG_MODE_DEBUG"
#endif
#define CG_PROFILE (CG_MODE == CG_MODE_PROFILE)
#define CG_DEBUG (CG_MODE == CG_MODE_DEBUG)
#define CG_VALIDATE (CG_DEBUG && MODEL_HAS_VALIDATION_DATA)

#ifndef CG_POWER_MS
#define CG_POWER_MS 0
#endif
#ifndef CG_POWER_IDLE_MS
#define CG_POWER_IDLE_MS 0
#endif
#ifndef CG_POWER_WARMUP_MS
#define CG_POWER_WARMUP_MS 0
#endif
#ifndef CG_POWER_CYCLES
#define CG_POWER_CYCLES 1
#endif
#if CG_POWER_WARMUP_MS < 0 || CG_POWER_WARMUP_MS > 3600000 || CG_POWER_CYCLES < 1                  \
    || CG_POWER_CYCLES > 100
#error "CG_POWER_WARMUP_MS is 0..3600000 ms and CG_POWER_CYCLES 1..100"
#endif
#if CG_POWER_MS < 0 || CG_POWER_MS > 3600000 || CG_POWER_IDLE_MS < 0 || CG_POWER_IDLE_MS > 3600000 \
    || (CG_POWER_IDLE_MS > 0 && CG_POWER_MS == 0)
#error "CG_POWER_MS and CG_POWER_IDLE_MS are 0..3600000 ms; idle needs an active window"
#endif
#if CG_POWER_MS > 0 && CG_MODE != CG_MODE_RELEASE
#error "power measurement needs a release build"
#endif

/* CG_PRINT_RESULT 0 (Makefile: CG_PRINT_RESULT=0) prints `result: not_printed`
 * instead of the result's values; the host still compares the result in DDR
 * with the golden. Power builds default to 0: the UART copy of a large
 * segmentation result takes minutes (a batch's, hours). */
#ifndef CG_PRINT_RESULT
#define CG_PRINT_RESULT (CG_POWER_MS == 0)
#endif

#ifndef CG_LOG_LEVEL
#define CG_LOG_LEVEL CG_DEBUG
#endif
#if CG_LOG_LEVEL < 0 || CG_LOG_LEVEL > 3 || (CG_LOG_LEVEL > 0 && !CG_DEBUG)
#error "CG_LOG_LEVEL is 0 outside debug builds and 1..3 in debug builds"
#endif

/* Prints `log: <name> a=0x... b=0x...` when the debug log reaches `level`. */
static void cg_log(int level, const char *name, uint32_t a, uint32_t b) {
    if (CG_LOG_LEVEL < level) {
        return;
    }
    nx_puts("log: ");
    nx_puts(name);
    nx_puts(" a=");
    nx_put_hex32(a);
    nx_puts(" b=");
    nx_put_hex32(b);
    nx_puts("\r\n");
}

/* Per-stage counter totals of one inference (profile builds). Stages do not
 * nest; the totals include the counter reads. */
enum {
    PROFILE_INIT,
    PROFILE_ZERO,
    PROFILE_CONTROL,
    PROFILE_WAIT,
    PROFILE_ACCUMULATE,
    PROFILE_FUSED,
    PROFILE_POSTPROCESS,
    PROFILE_POOL,
    PROFILE_FINISH,
    PROFILE_PACK,
    PROFILE_PACK_CONCAT,
    PROFILE_CLEAN,
    PROFILE_COUNT
};
static const char *profile_names[PROFILE_COUNT] = { "init_memset", "zero_buffers", "control",
    "wait_packet", "accumulate", "fused", "postprocess", "pool_copy", "finish_other", "pack",
    "pack_concat", "clean_intermediate" };
static uint64_t profile_ticks[PROFILE_COUNT];
static uint64_t profile_mark;

static void profile_start(void) {
    if (CG_PROFILE) {
        profile_mark = nx_ticks();
    }
}

static void profile_stop(int stage) {
    if (CG_PROFILE) {
        profile_ticks[stage] += nx_ticks() - profile_mark;
    }
}

#endif
