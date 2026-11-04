/* UART text output without printf: strings and numbers. */
#ifndef NX_LOG_H
#define NX_LOG_H

#include <stdint.h>

#include "nx_platform.h"

static inline void nx_puts(const char *text) {
    while (*text != '\0') {
        nx_putc(*text++);
    }
}

/* 0x followed by eight hex digits. */
static inline void nx_put_hex32(uint32_t value) {
    nx_puts("0x");
    for (int shift = 28; shift >= 0; shift -= 4) {
        nx_putc("0123456789abcdef"[(value >> shift) & 0xf]);
    }
}

static inline void nx_put_u64(uint64_t value) {
    char digits[20];
    int count = 0;
    do {
        digits[count++] = '0' + value % 10;
        value /= 10;
    } while (value != 0);
    while (count != 0) {
        nx_putc(digits[--count]);
    }
}

static inline void nx_put_i32(int32_t value) {
    if (value < 0) {
        nx_putc('-');
    }
    nx_put_u64(value < 0 ? -(int64_t)value : value);
}

/* Prints `fraction` (< scale) as the digits after the point. */
static inline void nx_put_fraction(uint64_t fraction, uint64_t scale) {
    for (scale /= 10; scale != 0; scale /= 10) {
        nx_putc('0' + fraction / scale);
        fraction %= scale;
    }
}

/* A 100 MHz counter interval as milliseconds with six decimals. */
static inline void nx_put_ms(uint64_t ticks) {
    nx_put_u64(ticks / 100000);
    nx_putc('.');
    nx_put_fraction(ticks % 100000 * 10, 1000000);
}

/* A float rounded to six decimals. */
static inline void nx_put_float6(float value) {
    if (value < 0.0f) {
        nx_putc('-');
        value = -value;
    }
    uint32_t scaled = value * 1000000.0f + 0.5f;
    nx_put_u64(scaled / 1000000);
    nx_putc('.');
    nx_put_fraction(scaled % 1000000, 1000000);
}

#endif
