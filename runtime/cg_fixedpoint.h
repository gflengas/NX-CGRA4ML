/* Exact QONNX fixed-point postprocess (quantize, leaky ReLU, quantize), its
 * compiled form and upstream's activation, scalar and on four lanes. Each
 * fast form is bit-exact with the reference; test/ checks every one. The
 * helpers of the per-value loops are forced inline, as GCC does not always
 * inline them into the fused pass. */
#ifndef CG_FIXEDPOINT_H
#define CG_FIXEDPOINT_H

#include <stdint.h>
#include <string.h>

#include "cg_fixed_types.h"

/* Rescale the magnitude exactly, round ties to even, then saturate; a left
 * shift that would overflow saturates. */
static inline int32_t cg_fixed_quantize(
    int64_t value, int32_t source_frac, uint32_t bits, int32_t target_frac, uint32_t narrow) {
    uint64_t magnitude = value < 0 ? (uint64_t) - (value + 1) + 1 : (uint64_t)value;
    uint64_t limit = (UINT64_C(1) << (bits - 1)) - (value < 0 ? narrow : 1);
    int32_t shift = source_frac - target_frac;
    if (shift >= 64) {
        magnitude = 0;
    } else if (shift > 0) {
        uint64_t quotient = magnitude >> shift;
        uint64_t remainder = magnitude & ((UINT64_C(1) << shift) - 1);
        uint64_t half = UINT64_C(1) << (shift - 1);
        magnitude = quotient + (remainder > half || (remainder == half && (quotient & 1)));
    } else if (shift < 0 && magnitude != 0) {
        uint32_t left = -shift;
        magnitude = left >= 64 || magnitude > limit >> left ? limit : magnitude << left;
    }
    magnitude = magnitude > limit ? limit : magnitude;
    return value < 0 ? -(int64_t)magnitude : (int64_t)magnitude;
}

/* The reference plan: no operation, one quantize, or quantize, leaky ReLU,
 * quantize. */
static inline int32_t cg_fixed_postprocess(int32_t input, const cg_fixed_postprocess_t *plan) {
    const cg_fixed_operation_t *op = plan->operations;
    int64_t value = input;
    if (plan->count == 0) {
        return input;
    }
    value = cg_fixed_quantize(value, plan->input_frac, op[0].bits, op[0].frac, op[0].narrow);
    if (plan->count == 1) {
        return value;
    }
    /* The first quantize leaves at most 32-bit codes: the product fits int64. */
    value *= value < 0 ? op[1].numerator : UINT32_C(1) << op[1].shift;
    return cg_fixed_quantize(value, op[0].frac + op[1].shift, op[2].bits, op[2].frac, op[2].narrow);
}

/* A step that shifts right by 1..31 bits with round half to even, then clamps:
 * q = floor(v / 2^s) and remainder r; q + 1 when r + (q & 1) > half. */
static inline __attribute__((always_inline)) int32_t cg_fixed_step_right(
    int32_t value, const cg_fixed_step_t *step) {
    int32_t quotient = value >> step->shift;
    uint32_t remainder = (uint32_t)value & (((uint32_t)step->half_minus_one << 1) | 1);
    value = quotient + (remainder + ((uint32_t)quotient & 1) > (uint32_t)step->half_minus_one + 1);
    value = value < step->low ? step->low : value;
    return value > step->high ? step->high : value;
}

static inline __attribute__((always_inline)) int32_t cg_fixed_step(
    int32_t value, const cg_fixed_step_t *step) {
    if (step->shift >= 32) {
        return 0;
    }
    if (step->shift > 0) {
        return cg_fixed_step_right(value, step);
    }
    if (step->shift < 0) {
        if (value > step->left_high) {
            return step->high;
        }
        if (value < step->left_low) {
            return step->low;
        }
        /* In range; zero when the shift is 32 or more (both bounds 0). */
        return step->shift <= -32 ? 0 : (int32_t)((uint32_t)value << -step->shift);
    }
    value = value < step->low ? step->low : value;
    return value > step->high ? step->high : value;
}

static inline __attribute__((always_inline)) int32_t cg_fixed_apply(
    int32_t value, const cg_fixed_compiled_t *compiled) {
    if (compiled->count == 0) {
        return value;
    }
    value = cg_fixed_step(value, &compiled->first);
    if (compiled->count == 1) {
        return value;
    }
    if (compiled->table != NULL) {
        return compiled->table[value + 128];
    }
    value = value < 0 ? value * compiled->numerator
                      : (int32_t)((uint32_t)value << compiled->leaky_shift);
    return cg_fixed_step(value, &compiled->last);
}

/* Upstream's shifted bias (value * 2^value_shift + bias * 2^bias_shift, trap
 * outside int32) without 64-bit arithmetic per value: per channel the scaled
 * bias and the inputs whose sum fits int32. */
typedef struct {
    int32_t bias;
    int32_t low, high;
} cg_bias_bound_t;

/* Fills `bound`; returns 0 when the scaled bias itself does not fit int32.
 * Shifts at most 30. */
static inline int cg_bias_bound(
    int32_t bias, uint32_t value_shift, uint32_t bias_shift, cg_bias_bound_t *bound) {
    int64_t scaled = (int64_t)bias * (INT64_C(1) << bias_shift);
    if (scaled < INT32_MIN || scaled > INT32_MAX) {
        return 0;
    }
    /* floor((INT32_MAX - b) / 2^s) and ceil((INT32_MIN - b) / 2^s). */
    int64_t high = (INT32_MAX - scaled) >> value_shift;
    int64_t low = -((scaled - INT32_MIN) >> value_shift);
    *bound = (cg_bias_bound_t){
        .bias = scaled,
        .low = low < INT32_MIN ? INT32_MIN : low,
        .high = high > INT32_MAX ? INT32_MAX : high,
    };
    return 1;
}

/* value * 2^value_shift + bias; the sum fits int32 inside the bound. */
static inline __attribute__((always_inline)) int32_t cg_bias_shifted(
    int32_t value, uint32_t value_shift, const cg_bias_bound_t *bound) {
    if (value < bound->low || value > bound->high) {
        __builtin_trap();
    }
    return ((uint32_t)value << value_shift) + (uint32_t)bound->bias;
}

/* Upstream quant_lrelu: optional leaky scale, shift with round half to even,
 * clamp to `bits` (pl_scale < bits, shift < 31). */
static inline int32_t cg_activate_bits(
    int32_t value, uint32_t nzero, uint32_t shift, uint32_t pl_scale, uint32_t bits) {
    int32_t minimum = -(INT32_C(1) << (bits - pl_scale - 1));
    int32_t maximum = (INT32_C(1) << (bits - 1)) - 1;
    if (value < 0 && nzero == 0) {
        value = 0;
    } else if (value >= 0) {
        value *= INT32_C(1) << pl_scale;
    }
    if (shift != 0) {
        value = (value + ((INT32_C(1) << (shift - 1)) - (~(value >> shift) & 1))) >> shift;
    }
    return value < minimum ? minimum : value > maximum ? maximum : value;
}

/* Four lanes: GCC vector extensions, NEON on the board, SSE on the host; lane
 * by lane the same integer operations as the scalar functions. */
typedef int32_t cg_v4i __attribute__((vector_size(16)));
typedef uint32_t cg_v4u __attribute__((vector_size(16)));

static inline __attribute__((always_inline)) cg_v4i cg_v4_splat(int32_t value) {
    return (cg_v4i){ 0, 0, 0, 0 } + value;
}

static inline __attribute__((always_inline)) int cg_v4_any(cg_v4i mask) {
    return (mask[0] | mask[1] | mask[2] | mask[3]) != 0;
}

static inline __attribute__((always_inline)) cg_v4i cg_v4_clamp(cg_v4i v, cg_v4i low, cg_v4i high) {
    cg_v4i m = v < low;
    v = (v & ~m) | (low & m);
    m = v > high;
    return (v & ~m) | (high & m);
}

/* A right-shift step's constants as lanes. */
typedef struct {
    cg_v4u mask, threshold;
    cg_v4i low, high;
    int32_t shift;
} cg_v4_step_t;

static inline cg_v4_step_t cg_v4_step(const cg_fixed_step_t *step) {
    return (cg_v4_step_t){
        .mask = (cg_v4u){ 0, 0, 0, 0 } + (((uint32_t)step->half_minus_one << 1) | 1),
        .threshold = (cg_v4u){ 0, 0, 0, 0 } + ((uint32_t)step->half_minus_one + 1),
        .low = cg_v4_splat(step->low),
        .high = cg_v4_splat(step->high),
        .shift = step->shift,
    };
}

/* cg_fixed_step_right on four lanes; the comparison is -1 where the scalar
 * adds one. */
static inline __attribute__((always_inline)) cg_v4i cg_v4_step_right(
    cg_v4i value, const cg_v4_step_t *s) {
    cg_v4i quotient = value >> s->shift;
    cg_v4u remainder = ((cg_v4u)value & s->mask) + ((cg_v4u)quotient & 1);
    return cg_v4_clamp(quotient - (cg_v4i)(remainder > s->threshold), s->low, s->high);
}

/* Lanes (-1) where an int32 add overflows: operands of one sign, the sum of
 * the other. */
static inline __attribute__((always_inline)) cg_v4i cg_v4_add_overflow_lanes(
    cg_v4i a, cg_v4i b, cg_v4i sum) {
    return ((a ^ sum) & (b ^ sum)) < cg_v4_splat(0);
}

/* Lanes where cg_bias_shifted traps. */
static inline __attribute__((always_inline)) cg_v4i cg_v4_outside(
    cg_v4i value, const cg_bias_bound_t *bound) {
    return (value < cg_v4_splat(bound->low)) | (value > cg_v4_splat(bound->high));
}

/* cg_bias_shifted on four lanes without the range check (cg_v4_outside). */
static inline __attribute__((always_inline)) cg_v4i cg_v4_bias_shift_lanes(
    cg_v4i value, uint32_t value_shift, const cg_bias_bound_t *bound) {
    return (cg_v4i)(((cg_v4u)value << value_shift) + (uint32_t)bound->bias);
}

/* cg_activate_bits on four lanes. Where the scalar code overflows int32 (the
 * leaky scale or the rounding add: undefined behaviour) the lanes wrap;
 * elsewhere they are identical. */
static inline __attribute__((always_inline)) cg_v4i cg_v4_activate_bits(
    cg_v4i value, uint32_t nzero, uint32_t shift, uint32_t pl_scale, uint32_t bits) {
    cg_v4i negative = value < cg_v4_splat(0);
    cg_v4i scaled = (cg_v4i)((cg_v4u)value << pl_scale);
    /* Negative: 0 without nzero, unchanged with it; else scaled. */
    value = (scaled & ~negative) | (nzero != 0 ? value & negative : cg_v4_splat(0));
    if (shift != 0) {
        cg_v4i correction = cg_v4_splat(INT32_C(1) << (shift - 1)) - (~(value >> shift) & 1);
        value = (cg_v4i)((cg_v4u)value + (cg_v4u)correction) >> shift;
    }
    return cg_v4_clamp(value, cg_v4_splat(-(INT32_C(1) << (bits - pl_scale - 1))),
        cg_v4_splat((INT32_C(1) << (bits - 1)) - 1));
}

#endif
