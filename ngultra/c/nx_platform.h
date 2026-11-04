/* NG-ULTRA platform layer: MMIO, barriers, cache maintenance, counter, UART.
 *
 * The board build maps every function onto the BSP as an inline wrapper. The
 * simulation build (NX_SIM) only declares them; the Verilator harness
 * implements them. Runtime code therefore never checks which one it runs on.
 */
#ifndef NX_PLATFORM_H
#define NX_PLATFORM_H

#include <stddef.h>
#include <stdint.h>

#ifdef NX_SIM
#ifdef __cplusplus
extern "C" {
#endif
uint32_t nx_mmio_read(uint32_t address);
void nx_mmio_write(uint32_t address, uint32_t value);
void nx_barrier(void);
/** Enables the FPU / Advanced SIMD for this core (no-op in simulation). */
void nx_simd_enable(void);
void nx_putc(char value);
uint64_t nx_ticks(void);
void nx_counter_reset(void);
void nx_counter_start(void);
void nx_counter_stop(void);
/** Reports a cache maintenance range to the harness. */
void nx_sim_cache_access(const void *address, size_t bytes);
/** Records an accelerator ownership event in the harness trace. */
void nx_sim_event(const char *name, uint32_t first, uint32_t second);
/** Advances the RTL and the emulated host by one mailbox poll. */
void nx_sim_external_poll(uint32_t poll);
#ifdef __cplusplus
}
#endif
#else
#include <arm/CortexR52.h>
#include "arm_read_counter.h"
#include "system_counter.h"
#include "uart/uart.h"

#if !defined(NGULTRA_CACHE)
#error "NG-ULTRA runtime requires the data cache maintenance API"
#endif

/* Exported by the BSP cache assembly but missing from CortexR52.h. */
extern void Memory_DCacheInvalidateMVA(uint32_t start, size_t len);

static inline uint32_t nx_mmio_read(uint32_t address) {
    return *(volatile uint32_t *)(uintptr_t)address;
}
static inline void nx_mmio_write(uint32_t address, uint32_t value) {
    *(volatile uint32_t *)(uintptr_t)address = value;
}
static inline void nx_barrier(void) {
    __asm__ volatile("dsb sy\nisb sy" ::: "memory");
}
static inline void nx_putc(char value) {
    uart_put_char(value);
}
/** Enables the FPU / Advanced SIMD: the app has no BSP C
 * runtime start (crt0's CortexR52_initFPU does not run), so at EL2: clear the
 * HCPTR cp10/cp11 traps, give CPACR full cp10/cp11 access, set FPEXC.EN. */
static inline void nx_simd_enable(void) {
    uint32_t value;
    __asm__ volatile("mrc p15, 4, %0, c1, c1, 2" : "=r"(value));
    value &= ~(UINT32_C(3) << 10);
    __asm__ volatile("mcr p15, 4, %0, c1, c1, 2\n isb" ::"r"(value) : "memory");
    __asm__ volatile("mrc p15, 0, %0, c1, c0, 2" : "=r"(value));
    value |= UINT32_C(0xF) << 20;
    __asm__ volatile("mcr p15, 0, %0, c1, c0, 2\n isb" ::"r"(value) : "memory");
    value = UINT32_C(0x40000000);
    __asm__ volatile("vmsr fpexc, %0\n isb" ::"r"(value) : "memory");
}
/** Returns the 100 MHz system counter. */
static inline uint64_t nx_ticks(void) {
    return arm_read_cntpct();
}
static inline void nx_counter_reset(void) {
    sys_cnt_reset();
}
static inline void nx_counter_start(void) {
    sys_cnt_start();
}
static inline void nx_counter_stop(void) {
    sys_cnt_stop();
}
static inline void nx_sim_event(const char *name, uint32_t first, uint32_t second) {
    (void)name;
    (void)first;
    (void)second;
}
#endif

/** Writes CPU data back so the device reads it (cache clean). */
static inline void nx_cache_clean(const void *address, size_t bytes) {
#ifdef NX_SIM
    nx_sim_cache_access(address, bytes);
#else
    Memory_DCacheCleanMVA((uint32_t)(uintptr_t)address, bytes);
#endif
    nx_barrier();
}

/** Cleans and drops lines before the device writes the range. */
static inline void nx_cache_clean_invalidate(void *address, size_t bytes) {
#ifdef NX_SIM
    nx_sim_cache_access(address, bytes);
#else
    Memory_DCacheCleanAndInvalidateMVA((uint32_t)(uintptr_t)address, bytes);
#endif
    nx_barrier();
}

/** Drops lines so the CPU reads what the device wrote. */
static inline void nx_cache_invalidate(void *address, size_t bytes) {
#ifdef NX_SIM
    nx_sim_cache_access(address, bytes);
#else
    Memory_DCacheInvalidateMVA((uint32_t)(uintptr_t)address, bytes);
#endif
    nx_barrier();
}

/** Makes a CPU-written buffer visible to the device; traps on an empty range. */
static inline void nx_dma_to_device(const void *address, size_t bytes) {
    if (address == NULL || bytes == 0u)
        __builtin_trap();
    nx_cache_clean(address, bytes);
}

/** Prepares a buffer that the device or host will write. */
static inline void nx_dma_prepare_from_device(void *address, size_t bytes) {
    if (address == NULL || bytes == 0u)
        __builtin_trap();
    nx_cache_clean_invalidate(address, bytes);
}

/** Makes device- or host-written data visible to the CPU. */
static inline void nx_dma_from_device(void *address, size_t bytes) {
    if (address == NULL || bytes == 0u)
        __builtin_trap();
    nx_cache_invalidate(address, bytes);
}

/**
 * Bounded mailbox wait used while the host transfers data.
 *
 * Board: expires 120 s after start on the 100 MHz counter, plus the time the
 * host needs to transfer `bytes`. Simulation: expires after 10,000 polls;
 * every poll advances the RTL and the host.
 */
static inline int nx_host_wait_expired(uint64_t start, uint32_t poll, uint32_t bytes) {
#ifdef NX_SIM
    (void)start;
    (void)bytes;
    return poll == 10000u;
#else
    /* 120 s, plus the host's DAP-AXI transfer of `bytes` at 32 KiB/s (a
   * batch's result is tens of MB). */
    (void)poll;
    return nx_ticks() - start >= UINT64_C(12000000000) + (uint64_t)bytes * 100000000u / 32768u;
#endif
}

static inline void nx_host_wait_step(uint32_t poll) {
#ifdef NX_SIM
    nx_sim_external_poll(poll);
#else
    (void)poll;
#endif
}

/** Stops the CPU after the session (simulation returns to the harness). */
static inline void nx_halt(void) {
#ifndef NX_SIM
    for (;;) {
        __asm__ volatile("wfi" ::: "memory");
    }
#endif
}

#endif
