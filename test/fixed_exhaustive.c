/* Every-input check of the runtime's fast fixed-point arithmetic for one
 * model's tables. Compiled with the model's
 * generated model.c by test/check_fixed_exhaustive.py.
 *
 * Every bundle's convolution and pool record compiled at export must equal
 * the C reference compiler's (fixed_compile_reference.h). For every distinct
 * compiled plan, cg_fixed_apply of the emitted record, and the specialized
 * fast form of its shape (the right-shift step, then the table), must equal the
 * reference cg_fixed_postprocess for all 2^32 int32 inputs, and so must the
 * four-lane version of the shape (cg_v4_step_right); for every concat transition
 * quantizer, the compiled step (cg_pack.h) must equal the reference
 * cg_fixed_quantize for all int32 inputs. */
#include <stdio.h>
#include <string.h>

#include "fixed_compile_reference.h"
#include "model.h"

enum { MAX_PLANS = 2 * MODEL_BUNDLE_COUNT, MAX_STEPS = 3 * MODEL_BUNDLE_COUNT, CHUNKS = 64 };

typedef struct { int32_t source_frac, frac; uint32_t bits; } transition_t;

static cg_fixed_postprocess_t plans[MAX_PLANS];
static const cg_fixed_compiled_t *emitted[MAX_PLANS];
static transition_t steps[MAX_STEPS];
static int plan_count, step_count;

static int same_step(const cg_fixed_step_t *a, const cg_fixed_step_t *b) {
  return a->shift == b->shift && a->half_minus_one == b->half_minus_one &&
         a->left_low == b->left_low && a->left_high == b->left_high &&
         a->low == b->low && a->high == b->high;
}

/* The record emitted at export must equal the C reference compiler's,
 * field by field and table entry by table entry. */
static long check_record(uint32_t bundle, const cg_fixed_postprocess_t *plan,
                         const cg_fixed_compiled_t *record) {
  cg_fixed_compiled_t reference;
  int32_t table[CG_FIXED_TABLE_CODES];
  cg_fixed_reference(plan, &reference, table);
  int same = record->mode == reference.mode && record->kind == reference.kind &&
             record->count == reference.count && record->numerator == reference.numerator &&
             record->leaky_shift == reference.leaky_shift &&
             same_step(&record->first, &reference.first) &&
             same_step(&record->last, &reference.last) &&
             (record->table == NULL) == (reference.table == NULL);
  for (int i = 0; same && record->table != NULL && i < CG_FIXED_TABLE_CODES; ++i)
    same = record->table[i] == table[i];
  if (!same) printf("RECORD MISMATCH bundle %u\n", bundle);
  return !same;
}

/* A consumer's join quantizers emitted at export must equal the
 * reference compile of the plan's fractions and widths. */
static cg_fixed_step_t join_step(int32_t source_frac, uint32_t bits, int32_t frac) {
  const cg_fixed_operation_t op = {.kind = CG_FIXED_QUANT, .bits = bits, .frac = frac};
  return cg_fixed_compile_step(source_frac - frac, &op);
}

static long check_join(uint32_t consumer, const model_concat_plan_t *c) {
  const cg_transition_quant_t *d = &model_concat_decoder_quant[consumer];
  const cg_transition_quant_t *s = &model_concat_skip_quant[consumer];
  const int32_t source = c->resize_bits != 0u ? c->resize_frac : c->decoder_frac;
  int same = d->has_resize == (c->resize_bits != 0u) && s->has_resize == 0u;
  if (c->resize_bits != 0u) {
    const cg_fixed_step_t r = join_step(c->decoder_frac, c->resize_bits, c->resize_frac);
    same = same && same_step(&d->resize, &r);
  }
  {
    const cg_fixed_step_t dj = join_step(source, c->join_bits, c->join_frac);
    const cg_fixed_step_t sj = join_step(c->skip_frac, c->join_bits, c->join_frac);
    same = same && same_step(&d->join, &dj) && same_step(&s->join, &sj);
  }
  if (!same) printf("JOIN RECORD MISMATCH consumer %u\n", consumer);
  return !same;
}

static void add_plan(const cg_fixed_postprocess_t *plan, const cg_fixed_compiled_t *record) {
  if (record->mode != CG_FIXED_MODE_COMPILED) return;
  for (int i = 0; i < plan_count; ++i)
    if (memcmp(&plans[i], plan, sizeof(*plan)) == 0) return;
  emitted[plan_count] = record;
  plans[plan_count++] = *plan;
}

static void add_step(int32_t source_frac, uint32_t bits, int32_t frac) {
  for (int i = 0; i < step_count; ++i)
    if (steps[i].source_frac == source_frac && steps[i].bits == bits && steps[i].frac == frac)
      return;
  steps[step_count++] = (transition_t){source_frac, frac, bits};
}

int main(void) {
  long failed = 0;
  for (uint32_t b = 0; b < MODEL_BUNDLE_COUNT; ++b) {
    const model_concat_plan_t *c = &model_concat_plans[b];
    failed += check_record(b, &model_fixed_postprocess[b], &model_fixed_compiled[b]);
    if (c->skip_bundle != -1) failed += check_join(b, c);
    failed += check_record(b, &model_pool_postprocess[b], &model_pool_compiled[b]);
    add_plan(&model_fixed_postprocess[b], &model_fixed_compiled[b]);
    add_plan(&model_pool_postprocess[b], &model_pool_compiled[b]);
    if (c->skip_bundle == -1) continue;
    if (c->resize_bits != 0u) {
      add_step(c->decoder_frac, c->resize_bits, c->resize_frac);
      add_step(c->resize_frac, c->join_bits, c->join_frac);
    } else {
      add_step(c->decoder_frac, c->join_bits, c->join_frac);
    }
    add_step(c->skip_frac, c->join_bits, c->join_frac);
  }
  /* Each task checks one 2^26-input chunk of one plan or step. */
  #pragma omp parallel for schedule(dynamic) reduction(+:failed)
  for (int task = 0; task < (plan_count + step_count) * CHUNKS; ++task) {
    const int item = task / CHUNKS;
    const uint64_t first = (uint64_t)(task % CHUNKS) << 26, end = first + (UINT64_C(1) << 26);
    if (item < plan_count) {
      const cg_fixed_compiled_t compiled = *emitted[item];
      const uint32_t kind = compiled.kind;
      const cg_v4_step_t lanes_step = cg_v4_step(&compiled.first);
      for (uint64_t u = first; u < end; ++u) {
        const int32_t v = (int32_t)(uint32_t)u;
        const int32_t reference = cg_fixed_postprocess(v, &plans[item]);
        /* The specialized function of the plan's shape too. */
        const int32_t special = kind == CG_FIXED_QUANT_RIGHT ? cg_fixed_step_right(v, &compiled.first)
            : kind == CG_FIXED_QUANT_TABLE ? compiled.table[cg_fixed_step_right(v, &compiled.first) + 128] : reference;
        if (cg_fixed_apply(v, &compiled) != reference || special != reference) {
          printf("MISMATCH plan %d kind %u input %d\n", item, kind, v);
          failed++;
          break;
        }
        /* Four lanes, each input once: lanes u .. u + 3. */
        if (kind != CG_FIXED_GENERAL && (u & 3u) == 0u) {
          const cg_v4i lanes = {v, (int32_t)(uint32_t)(u + 1u), (int32_t)(uint32_t)(u + 2u),
                                (int32_t)(uint32_t)(u + 3u)};
          cg_v4i q = cg_v4_step_right(lanes, &lanes_step);
          int bad = 0;
          for (int l = 0; l < 4; ++l) {
            const int32_t lane = kind == CG_FIXED_QUANT_TABLE ? compiled.table[q[l] + 128] : q[l];
            bad |= lane != cg_fixed_postprocess(lanes[l], &plans[item]);
          }
          if (bad) {
            printf("VECTOR MISMATCH plan %d kind %u input %d\n", item, kind, v);
            failed++;
            break;
          }
        }
      }
    } else {
      const transition_t *t = &steps[item - plan_count];
      const cg_fixed_operation_t op = {.kind = CG_FIXED_QUANT, .bits = t->bits, .frac = t->frac};
      const cg_fixed_step_t step = cg_fixed_compile_step(t->source_frac - t->frac, &op);
      for (uint64_t u = first; u < end; ++u) {
        const int32_t v = (int32_t)(uint32_t)u;
        if (cg_fixed_step(v, &step) != cg_fixed_quantize(v, t->source_frac, t->bits, t->frac, 0u)) {
          printf("MISMATCH transition %d input %d\n", item - plan_count, v);
          failed++;
          break;
        }
      }
    }
  }
  printf("%s plans=%d transitions=%d inputs_each=4294967296\n", failed ? "FAIL" : "PASS",
         plan_count, step_count);
  return failed != 0;
}
