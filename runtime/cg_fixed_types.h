/* Fixed-point postprocess plans and their compiled form, as generated into
 * the model tables. */
#ifndef CG_FIXED_TYPES_H
#define CG_FIXED_TYPES_H

#include <stdint.h>

enum { CG_FIXED_QUANT = 1, CG_FIXED_LEAKY = 2 };
typedef struct {
    uint32_t kind, bits, narrow, numerator, shift;
    int32_t frac;
} cg_fixed_operation_t;
typedef struct {
    uint32_t enabled, count;
    int32_t input_frac;
    cg_fixed_operation_t operations[3];
} cg_fixed_postprocess_t;

/* A plan compiled at export (cgtools/tables.py). Each quantize is a signed round-half-even right
 * shift, or a saturating left shift, then a clamp; the leaky step is a
 * select. When the first quantize produces codes of at most 8 bits, the rest
 * of the plan (leaky and the last quantize) is a table of every code's
 * result, computed with the reference arithmetic (upstream's LUT idea). Equal
 * to cg_fixed_postprocess for every int32 input of a compiled-mode plan. */
typedef struct {
    int32_t shift; /* > 0 right, < 0 left, 0 none; right >= 32 is zero */
    int32_t half_minus_one; /* right: 2^(shift-1) - 1 */
    int32_t left_low, left_high; /* left: inputs outside saturate */
    int32_t low, high; /* clamp: [-(2^(bits-1)) + narrow, 2^(bits-1) - 1] */
} cg_fixed_step_t;

enum { CG_FIXED_TABLE_CODES = 256 };
/* How a bundle runs its plan: none (upstream's activation, cg_activate),
 * the checked reference cg_fixed_postprocess (leaky products wider than 31
 * bits), or the compiled constants. */
enum { CG_FIXED_MODE_NONE = 0, CG_FIXED_MODE_CHECKED = 1, CG_FIXED_MODE_COMPILED = 2 };
/* Compiled plan shapes with a loop of their own: one right-shift
 * quantize, or a right-shift quantize to codes of at most 8 bits followed by
 * the table; the others use cg_fixed_apply. */
enum { CG_FIXED_GENERAL = 0, CG_FIXED_QUANT_RIGHT = 1, CG_FIXED_QUANT_TABLE = 2 };

typedef struct {
    uint32_t mode; /* CG_FIXED_MODE_* */
    uint32_t kind; /* CG_FIXED_* shape; compiled mode only */
    uint32_t count; /* 0, 1 or 3 operations */
    int32_t numerator; /* leaky: negative values times numerator */
    uint32_t leaky_shift; /* leaky: non-negative values shifted left */
    cg_fixed_step_t first, last;
    /* count 3, first codes of at most 8 bits: the result of code c at
   * table[c + 128] (CG_FIXED_TABLE_CODES words of model_fixed_tables); NULL
   * otherwise. Small enough to copy into locals in a hot loop, where stores
   * to the output cannot alias it. */
    const int32_t *table;
} cg_fixed_compiled_t;

/* A concat join's quantizers, compiled at export: an optional resize
 * step, then the join step (cg_transition_apply). */
typedef struct {
    uint32_t has_resize;
    cg_fixed_step_t resize, join;
} cg_transition_quant_t;

#endif
