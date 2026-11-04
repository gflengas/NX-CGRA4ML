/* The C reference of the plan compiler, for tests
 * only: the generator (cgtools/tables.py) compiles every plan at
 * export and the runtime reads the result. test_fixed_compile_export.py
 * checks the generator against these functions field by field. */
#ifndef FIXED_COMPILE_REFERENCE_H
#define FIXED_COMPILE_REFERENCE_H

#include "cg_fixedpoint.h"

/* One quantize as a shift and a clamp (the generator's _compile_step). */
static inline cg_fixed_step_t cg_fixed_compile_step(int32_t shift, const cg_fixed_operation_t *op) {
  const int64_t positive = (INT64_C(1) << (op->bits - 1u)) - 1;
  const int64_t negative = (INT64_C(1) << (op->bits - 1u)) - op->narrow;
  cg_fixed_step_t step = {0};
  step.shift = shift;
  step.low = (int32_t)-negative;
  step.high = (int32_t)positive;
  if (shift > 0 && shift < 32) {
    step.half_minus_one = (INT32_C(1) << (shift - 1)) - 1;
  } else if (shift < 0) {
    const uint32_t left = (uint32_t)-shift;
    step.left_high = left >= 32u ? 0 : (int32_t)(positive >> left);
    step.left_low = left >= 32u ? 0 : (int32_t)-(negative >> left);
  }
  return step;
}

/* Checks a plan with the same traps as cg_fixed_postprocess. Returns 1 when
 * the compiled plan (cg_fixed_compile) is exact for every int32 input: each
 * leaky product of a preceding quantized code fits in 31 bits. */
static inline int cg_fixed_prepare(const cg_fixed_postprocess_t *plan) {
  int32_t frac = plan->input_frac;
  uint32_t code_bits = 0u;
  int fast = 1;
  if (plan->enabled != 1u || (plan->count != 0u && plan->count != 1u && plan->count != 3u) ||
      frac < -62 || frac > 62) __builtin_trap();
  for (uint32_t i = 0u; i < plan->count; ++i) {
    const cg_fixed_operation_t *op = &plan->operations[i];
    if (op->kind == CG_FIXED_QUANT && (i == 0u || i == 2u)) {
      if (op->bits < 2u || op->bits > 32u || op->narrow > 1u ||
          frac < -62 || frac > 78 || op->frac < -31 || op->frac > 31) __builtin_trap();
      frac = op->frac;
      code_bits = op->bits;
    } else if (op->kind == CG_FIXED_LEAKY && i == 1u && plan->count == 3u) {
      if (op->shift > 16u || op->numerator > (UINT32_C(1) << op->shift)) __builtin_trap();
      if (code_bits - 1u + op->shift > 30u) fast = 0;
      frac += (int32_t)op->shift;
    } else {
      __builtin_trap();
    }
  }
  return fast;
}

/** Compiles a plan that cg_fixed_prepare returned 1 for; `table` holds
 * CG_FIXED_TABLE_CODES words and must outlive the compiled plan. */
static inline void cg_fixed_compile(const cg_fixed_postprocess_t *plan,
                             cg_fixed_compiled_t *compiled, int32_t *table) {
  int32_t frac = plan->input_frac;
  *compiled = (cg_fixed_compiled_t){0};
  compiled->count = plan->count;
  for (uint32_t i = 0u; i < plan->count; ++i) {
    const cg_fixed_operation_t *op = &plan->operations[i];
    if (op->kind == CG_FIXED_QUANT) {
      if (i == 0u) compiled->first = cg_fixed_compile_step(frac - op->frac, op);
      else compiled->last = cg_fixed_compile_step(frac - op->frac, op);
      frac = op->frac;
    } else {
      compiled->numerator = (int32_t)op->numerator;
      compiled->leaky_shift = op->shift;
      frac += (int32_t)op->shift;
    }
  }
  if (plan->count == 3u && plan->operations[0].bits <= 8u) {
    const cg_fixed_operation_t *leaky = &plan->operations[1];
    const cg_fixed_operation_t *last = &plan->operations[2];
    const int32_t code_frac = plan->operations[0].frac + (int32_t)leaky->shift;
    for (int32_t code = -128; code < 128; ++code) {
      const int64_t scaled = (int64_t)code *
          (code < 0 ? (int64_t)leaky->numerator : (INT64_C(1) << leaky->shift));
      table[code + 128] =
          cg_fixed_quantize(scaled, code_frac, last->bits, last->frac, last->narrow);
    }
    compiled->table = table;
  }
}

/* The shape of a compiled plan (CG_FIXED_*). */
static inline int cg_fixed_kind(const cg_fixed_compiled_t *compiled) {
  const int right = compiled->first.shift > 0 && compiled->first.shift < 32;
  if (compiled->count == 1u && right) return CG_FIXED_QUANT_RIGHT;
  if (compiled->count == 3u && compiled->table != NULL && right) return CG_FIXED_QUANT_TABLE;
  return CG_FIXED_GENERAL;
}

/** The full record the generator emits for a plan: mode, then for compiled
 * plans the constants, the table (in `table`) and the shape. */
static inline void cg_fixed_reference(const cg_fixed_postprocess_t *plan,
                                      cg_fixed_compiled_t *compiled, int32_t *table) {
  *compiled = (cg_fixed_compiled_t){0};
  if (plan->enabled == 0u) return;
  if (!cg_fixed_prepare(plan)) {
    compiled->mode = CG_FIXED_MODE_CHECKED;
    return;
  }
  cg_fixed_compile(plan, compiled, table);
  compiled->mode = CG_FIXED_MODE_COMPILED;
  compiled->kind = (uint32_t)cg_fixed_kind(compiled);
}

#endif
