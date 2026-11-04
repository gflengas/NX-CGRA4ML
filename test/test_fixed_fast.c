/* Exhaustive-ish equivalence of the compiled fixed-point plan
 * (cg_fixed_compile, cg_fixed_apply) with the reference cg_fixed_postprocess. */
#include <stdio.h>
#include <stdlib.h>
#include "fixed_compile_reference.h"
static uint64_t state = 88172645463325252ull;
static uint32_t rnd(void) { state ^= state << 13; state ^= state >> 7; state ^= state << 17; return (uint32_t)state; }
int main(void) {
  unsigned long checked = 0, plans = 0, specialized = 0, vector_checked = 0;
  for (int iter = 0; iter < 200000; ++iter) {
    cg_fixed_postprocess_t p = {0};
    p.enabled = 1; p.count = (uint32_t[]){0,1,3}[rnd() % 3];
    p.input_frac = (int32_t)(rnd() % 125) - 62;
    int32_t frac = p.input_frac;
    for (uint32_t i = 0; i < p.count; ++i) {
      cg_fixed_operation_t *op = &p.operations[i];
      if (i == 1) { op->kind = CG_FIXED_LEAKY; op->shift = rnd() % 17; op->numerator = rnd() % ((1u << op->shift) + 1); frac += op->shift; }
      else { op->kind = CG_FIXED_QUANT; op->bits = 2 + rnd() % 31; op->narrow = rnd() % 2; op->frac = (int32_t)(rnd() % 63) - 31; frac = op->frac; }
    }
    if (frac < -1000) return 2;
    /* skip plans the reference would trap on (second quant source frac range) */
    int32_t f = p.input_frac, ok = 1;
    for (uint32_t i = 0; i < p.count; ++i) { if (i != 1) { if (f < -62 || f > 78) ok = 0; f = p.operations[i].frac; } else f += p.operations[i].shift; }
    if (!ok) continue;
    if (!cg_fixed_prepare(&p)) continue;
    cg_fixed_compiled_t compiled; int32_t table[CG_FIXED_TABLE_CODES]; cg_fixed_compile(&p, &compiled, table);
    ++plans;
    int32_t specials[] = {0, 1, -1, 2, -2, INT32_MAX, INT32_MIN, INT32_MIN + 1, 127, -128, 128, -129, 255, -256};
    for (int k = 0; k < 64; ++k) {
      int32_t v = k < 14 ? specials[k] : (k & 1 ? (int32_t)rnd() : (int32_t)(rnd() >> (rnd() % 31)) * (rnd() & 1 ? 1 : -1));
      int32_t a = cg_fixed_postprocess(v, &p), c = cg_fixed_apply(v, &compiled);
      if (a != c) { printf("MISMATCH v=%d a=%d c=%d count=%u in=%d\n", v, a, c, p.count, p.input_frac); return 1; }
      /* The specialized function of the plan's shape. */
      const int kind = cg_fixed_kind(&compiled);
      if (kind == CG_FIXED_QUANT_RIGHT) c = cg_fixed_step_right(v, &compiled.first), ++specialized;
      if (kind == CG_FIXED_QUANT_TABLE) c = compiled.table[cg_fixed_step_right(v, &compiled.first) + 128], ++specialized;
      if (a != c) { printf("SPECIAL MISMATCH kind=%d v=%d a=%d c=%d\n", kind, v, a, c); return 1; }
      if (kind != CG_FIXED_GENERAL) {
        /* Four lanes: v and three neighbours, against the scalar step. */
        const cg_v4_step_t vs4 = cg_v4_step(&compiled.first);
        const cg_v4i lanes = {v, (int32_t)((uint32_t)v + 1u), (int32_t)((uint32_t)v ^ 0x80000000u), (int32_t)rnd()};
        const cg_v4i q = cg_v4_step_right(lanes, &vs4);
        for (int l = 0; l < 4; ++l) {
          if (q[l] != cg_fixed_step_right(lanes[l], &compiled.first)) { printf("VECTOR STEP MISMATCH\n"); return 1; }
        }
        ++vector_checked;
      }
      ++checked;
    }
  }
  /* 8-bit plans, dense input sweep */
  for (int32_t in = 9; in <= 19; ++in) for (int32_t f1 = 2; f1 <= 12; ++f1) for (int32_t f2 = 2; f2 <= 12; ++f2) {
    cg_fixed_postprocess_t p = {.enabled = 1, .count = 3, .input_frac = in,
      .operations = {{.kind = CG_FIXED_QUANT, .bits = 8, .frac = f1}, {.kind = CG_FIXED_LEAKY, .numerator = 13, .shift = 7}, {.kind = CG_FIXED_QUANT, .bits = 8, .frac = f2}}};
    if (!cg_fixed_prepare(&p)) return 3;
    cg_fixed_compiled_t compiled; int32_t table[CG_FIXED_TABLE_CODES]; cg_fixed_compile(&p, &compiled, table);
    for (int32_t v = -300000; v <= 300000; v += 7) {
      const int32_t a = cg_fixed_postprocess(v, &p);
      if (a != cg_fixed_apply(v, &compiled)) { printf("PLAN MISMATCH v=%d\n", v); return 1; }
      if (cg_fixed_kind(&compiled) == CG_FIXED_QUANT_TABLE && a != compiled.table[cg_fixed_step_right(v, &compiled.first) + 128]) {
        printf("PLAN SPECIAL MISMATCH v=%d\n", v); return 1;
      }
      ++specialized;
      ++checked;
    }
  }
  /* Concat transition quantizers (cg_pack.h): one compiled step per
   * (bits 2..8, source frac, target frac in -15..15), against the reference. */
  for (uint32_t bits = 2; bits <= 8; ++bits) for (int32_t sf = -15; sf <= 15; ++sf)
    for (int32_t tf = -15; tf <= 15; ++tf) {
      const cg_fixed_operation_t op = {.kind = CG_FIXED_QUANT, .bits = bits, .frac = tf};
      const cg_fixed_step_t step = cg_fixed_compile_step(sf - tf, &op);
      for (int k = 0; k < 2000; ++k) {
        int32_t v = k < 14 ? (int32_t[]){0, 1, -1, 2, -2, INT32_MAX, INT32_MIN, INT32_MIN + 1,
                                         127, -128, 128, -129, 255, -256}[k]
                           : (k & 1 ? (int32_t)rnd() : (int32_t)(rnd() % 4096u) - 2048);
        if (cg_fixed_quantize(v, sf, bits, tf, 0u) != cg_fixed_step(v, &step)) {
          printf("TRANSITION MISMATCH bits=%u sf=%d tf=%d v=%d\n", bits, sf, tf, v); return 1;
        }
        ++checked;
      }
    }
  /* Shifted bias without 64-bit arithmetic: for every shift pair and
   * edge and random biases, the bounds decide fit exactly as the 64-bit sum
   * does, and the 32-bit result equals it, at both ends of each range. */
  unsigned long bias_checked = 0;
  for (uint32_t vs = 0; vs <= 30; ++vs) for (uint32_t bs = 0; bs <= 30; ++bs) {
    const int32_t edge[] = {0, 1, -1, 2, -2, 127, -128, INT32_MAX, INT32_MIN, INT32_MAX >> bs,
                            -(INT32_MAX >> bs) - 1, (INT32_MAX >> bs) + 1};
    for (int k = 0; k < 40; ++k) {
      const int32_t bias = k < 12 ? edge[k] : (int32_t)rnd() >> (rnd() % 32);
      cg_bias_bound_t bound;
      const int64_t scaled = (int64_t)bias * (INT64_C(1) << bs);
      const int ok = cg_bias_bound(bias, vs, bs, &bound);
      if (ok != (scaled >= INT32_MIN && scaled <= INT32_MAX)) { printf("BIAS FIT MISMATCH\n"); return 1; }
      if (!ok) continue;
      const int64_t probes[] = {(int64_t)bound.low - 1, bound.low, (int64_t)bound.low + 1,
                                (int64_t)bound.high - 1, bound.high, (int64_t)bound.high + 1,
                                0, 1, -1, INT32_MIN, INT32_MAX, (int32_t)rnd(), (int32_t)rnd() >> 12};
      for (int q = 0; q < 13; ++q) {
        if (probes[q] < INT32_MIN || probes[q] > INT32_MAX) continue;
        const int32_t v = (int32_t)probes[q];
        const int64_t sum = (int64_t)v * (INT64_C(1) << vs) + scaled;
        const int fits = sum >= INT32_MIN && sum <= INT32_MAX;
        const int inside = v >= bound.low && v <= bound.high;
        if (fits != inside) { printf("BIAS RANGE MISMATCH vs=%u bs=%u b=%d v=%d\n", vs, bs, bias, v); return 1; }
        if (fits && cg_bias_shifted(v, vs, &bound) != (int32_t)sum) { printf("BIAS VALUE MISMATCH\n"); return 1; }
        {
          /* Four lanes: the trap mask and, where it fits, the value. */
          const cg_v4i lane = cg_v4_splat(v);
          if ((cg_v4_outside(lane, &bound)[0] != 0) != !fits) { printf("VECTOR BIAS RANGE MISMATCH\n"); return 1; }
          if (fits && cg_v4_bias_shift_lanes(lane, vs, &bound)[2] != (int32_t)sum) { printf("VECTOR BIAS VALUE MISMATCH\n"); return 1; }
        }
        ++bias_checked;
      }
    }
  }
  /* Four-lane int32 add with the overflow trap, against the builtin. */
  unsigned long add_checked = 0;
  for (int k = 0; k < 2000000; ++k) {
    const int32_t edge[] = {0, 1, -1, INT32_MAX, INT32_MIN, INT32_MAX - 1, INT32_MIN + 1};
    const int32_t a = k < 49 ? edge[k % 7] : (int32_t)rnd(), b = k < 49 ? edge[k / 7] : (int32_t)rnd() >> (rnd() % 31);
    int32_t sum;
    const int overflow = __builtin_add_overflow(a, b, &sum);
    const cg_v4i va = cg_v4_splat(a), vb = cg_v4_splat(b);
    const cg_v4i vsum = (cg_v4i)((cg_v4u)va + (cg_v4u)vb);
    if (cg_v4_any(cg_v4_add_overflow_lanes(va, vb, vsum)) != overflow || (!overflow && vsum[1] != sum)) {
      printf("VECTOR ADD MISMATCH a=%d b=%d\n", a, b); return 1;
    }
    ++add_checked;
  }
  /* Upstream's activation on four lanes: every bits, pl_scale, shift
   * and nzero; inputs where the scalar code overflows int32 (undefined) are
   * skipped. */
  unsigned long activate_checked = 0;
  for (uint32_t bits = 2; bits <= 8; ++bits) for (uint32_t pl = 0; pl < bits; ++pl)
    for (uint32_t shift = 0; shift <= 30; ++shift) for (uint32_t nzero = 0; nzero <= 1; ++nzero)
      for (int k = 0; k < 400; ++k) {
        const int32_t edge[] = {0, 1, -1, INT32_MAX, INT32_MIN, 127, -128,
                                (int32_t)(UINT32_C(1) << shift), -(int32_t)(UINT32_C(1) << shift)};
        const int32_t v = k < 9 ? edge[k] : (int32_t)rnd() >> (rnd() % 32);
        const int64_t scaled = v >= 0 ? (int64_t)v << pl : v;
        const int64_t rounded = scaled + (shift ? (INT64_C(1) << (shift - 1)) : 0);
        if (scaled > INT32_MAX || rounded > INT32_MAX) continue;
        const int32_t a = cg_activate_bits(v, nzero, shift, pl, bits);
        const cg_v4i q = cg_v4_activate_bits(cg_v4_splat(v), nzero, shift, pl, bits);
        if (q[3] != a) { printf("VECTOR ACTIVATE MISMATCH v=%d bits=%u pl=%u shift=%u nzero=%u\n", v, bits, pl, shift, nzero); return 1; }
        ++activate_checked;
      }
  printf("PASS plans=%lu values=%lu specialized=%lu bias=%lu vector=%lu add=%lu activate=%lu\n",
         plans, checked, specialized, bias_checked, vector_checked, add_checked, activate_checked);
  return 0;
}
