/* Bundle output processing: bias, activation, residual add, pooling, softmax
 * and skip retention, either after all packets arrived (cg_finish_bundle) or
 * per packet straight into the next input (the fused pass). */
#ifndef CG_POSTPROCESS_H
#define CG_POSTPROCESS_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cg_config.h"
#include "cg_fixedpoint.h"
#include "cg_pack.h"
#include "cg_workspace.h"
#include "model.h"

/* Upper bound of a bundle's output channels per transfer (coe). */
enum { CG_MAX_COE = 64 };

#define cg_biases ((const model_bias_word_t *)(const void *)&cg_payload[MODEL_W_BYTES])

/* Adds a shifted bias to a shifted sum; traps on int32 overflow. */
static int32_t cg_apply_bias(
    int32_t value, uint32_t value_shift, int32_t bias, uint32_t bias_shift) {
    int64_t result =
        (int64_t)value * (INT64_C(1) << value_shift) + (int64_t)bias * (INT64_C(1) << bias_shift);
    if (result < INT32_MIN || result > INT32_MAX) {
        __builtin_trap();
    }
    return result;
}

/* cg_apply_bias with both shifts zero (the common case). */
static int32_t cg_add_bias(int32_t value, int32_t bias) {
    int32_t result;
    if (__builtin_add_overflow(value, bias, &result)) {
        __builtin_trap();
    }
    return result;
}

/* A biased value through the bundle's plan (CG_FIXED_MODE_*): upstream's
 * activation, the checked reference or the compiled constants. */
static int32_t apply_plan(int32_t value, uint32_t mode, const cg_fixed_compiled_t *compiled,
    const cg_fixed_postprocess_t *fixed, uint32_t nzero, uint32_t shift, uint32_t pl_scale) {
    if (mode == CG_FIXED_MODE_COMPILED) {
        return cg_fixed_apply(value, compiled);
    }
    if (mode == CG_FIXED_MODE_CHECKED) {
        return cg_fixed_postprocess(value, fixed);
    }
    return cg_activate_bits(value, nzero, shift, pl_scale, MODEL_X_BITS);
}

#if CG_VALIDATE
static void cg_check_residual(uint32_t slot) {
    cg_check_equal("residual", slot, cg_workspace->residual[cg_batch_image][slot],
        &model_expected_residual[slot][(size_t)cg_batch_image * MODEL_RESIDUAL_WORDS],
        MODEL_RESIDUAL_WORDS, 1);
}
#endif

/* Turns the current image's accumulated raw sums of bundle `bundle_index`
 * into its output tensor, then keeps what later bundles need: the result,
 * softmax, a concat skip or a residual. */
static void cg_finish_bundle(size_t bundle_index) {
    const model_bundle_t *bundle = &model_bundles[bundle_index];
    const model_concat_save_t *save = &model_concat_saves[bundle_index];
    /* Local copies: stores to the output cannot alias them, so their fields
     * stay in registers (the output is int32, the records uint32). */
    cg_fixed_postprocess_t fixed = model_fixed_postprocess[bundle_index];
    cg_fixed_compiled_t compiled = model_fixed_compiled[bundle_index];
    cg_fixed_compiled_t pool_compiled = model_pool_compiled[bundle_index];
    uint32_t co = bundle->co;
    uint32_t ca_nzero = bundle->ca_nzero;
    uint32_t ca_shift = bundle->ca_shift;
    uint32_t ca_pl_scale = bundle->ca_pl_scale;
    uint32_t aa_nzero = bundle->aa_nzero;
    uint32_t aa_shift = bundle->aa_shift;
    uint32_t aa_pl_scale = bundle->aa_pl_scale;
    uint32_t value_shift = bundle->b_val_shift;
    uint32_t bias_shift = bundle->b_bias_shift;
    int has_bias = bundle->is_bias != 0;
    int bias_shifted = value_shift != 0 || bias_shift != 0;
    int unit_stride =
        bundle->csh == 1 && bundle->csw == 1 && bundle->csh_shift == 0 && bundle->csw_shift == 0;
    const int8_t *residual = bundle->add_in_buffer_idx >= 0
        ? cg_workspace->residual[cg_batch_image][bundle->add_in_buffer_idx]
        : NULL;
    /* Unit-stride bundles without a pool write their output directly; the
     * rest write the convolution buffer, then pool or copy it. */
    int32_t *target =
        unit_stride && bundle->pool == 0 ? cg_workspace->output : cg_workspace->convolution;

    cg_log(2, "postprocess", bundle_index, bundle->output_words);
    /* The loops write every position that has a source row and column; clear
     * only when some have none. */
    if (!unit_stride || bundle->h != bundle->ch || bundle->w != bundle->cw) {
        profile_start();
        memset(target, 0, (size_t)bundle->ch * bundle->cw * co * sizeof(target[0]));
        profile_stop(PROFILE_ZERO);
    }
    profile_start();

    if (unit_stride) {
        /* Output (h, w, c) comes from raw word (c, w, h) of its packet. Stores
         * go to the NHWC output in pixel order with the channels innermost,
         * so consecutive stores fill one cache line: stores that alternate
         * between lines cost one DDR write each (~220 ns). The small packet
         * stays in the cache while its words are read with a stride. */
        int32_t bias[CG_MAX_COE];
        cg_bias_bound_t bound[CG_MAX_COE];
        /* Upstream's activation after a shifted bias: a loop of its own. */
        int shifted_activate = compiled.mode == CG_FIXED_MODE_NONE && has_bias && bias_shifted;
        for (uint32_t it = 0; it < bundle->t && bundle->coe * it < co; ++it) {
            uint32_t channels = co - bundle->coe * it;
            channels = channels < bundle->coe ? channels : bundle->coe;
            for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                bias[icoe] =
                    has_bias ? cg_biases[bundle->bias_offset + bundle->coe * it + icoe] : 0;
                if (shifted_activate
                    && !cg_bias_bound(bias[icoe], value_shift, bias_shift, &bound[icoe])) {
                    shifted_activate = 0;
                }
            }
            for (uint32_t il = 0; il < bundle->l && il * MODEL_PE_ROWS < bundle->h; ++il) {
                uint32_t rows = bundle->h - il * MODEL_PE_ROWS;
                rows = rows < MODEL_PE_ROWS ? rows : MODEL_PE_ROWS;
                for (uint32_t iw_kw2 = 0; iw_kw2 < bundle->w_kw2; ++iw_kw2) {
                    const int32_t *accumulation = cg_accumulation_packet(
                        bundle, ((size_t)it * bundle->l + il) * bundle->w_kw2 + iw_kw2);
                    uint32_t w_last = iw_kw2 == bundle->w_kw2 - 1 ? bundle->kw / 2 + 1 : 1;
                    size_t channel_stride = (size_t)w_last * MODEL_PE_ROWS;
                    for (uint32_t iw_last = 0; iw_last < w_last && iw_kw2 + iw_last < bundle->w;
                         ++iw_last) {
                        for (uint32_t ir = 0; ir < rows; ++ir) {
                            const int32_t *raw = &accumulation[iw_last * MODEL_PE_ROWS + ir];
                            size_t index =
                                ((size_t)(il * MODEL_PE_ROWS + ir) * bundle->cw + iw_kw2 + iw_last)
                                    * co
                                + bundle->coe * it;
                            if (shifted_activate) {
                                for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                                    int32_t value =
                                        cg_activate_bits(cg_bias_shifted(raw[icoe * channel_stride],
                                                             value_shift, &bound[icoe]),
                                            ca_nzero, ca_shift, ca_pl_scale, MODEL_X_BITS);
                                    if (residual != NULL) {
                                        value = cg_activate_bits(value + residual[index + icoe],
                                            aa_nzero, aa_shift, aa_pl_scale, MODEL_X_BITS);
                                    }
                                    target[index + icoe] = value;
                                }
                                continue;
                            }
                            for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                                int32_t value = raw[icoe * channel_stride];
                                if (has_bias) {
                                    value = bias_shifted
                                        ? cg_apply_bias(value, value_shift, bias[icoe], bias_shift)
                                        : cg_add_bias(value, bias[icoe]);
                                }
                                value = apply_plan(value, compiled.mode, &compiled, &fixed,
                                    ca_nzero, ca_shift, ca_pl_scale);
                                if (residual != NULL) {
                                    value = cg_activate_bits(value + residual[index + icoe],
                                        aa_nzero, aa_shift, aa_pl_scale, MODEL_X_BITS);
                                }
                                target[index + icoe] = value;
                            }
                        }
                    }
                }
            }
        }
    } else {
        /* Strided: output (y_h, y_w) is convolution ((y_h - shift) / stride,
         * (y_w - shift) / stride) where both divide exactly. */
        for (uint32_t it = 0; it < bundle->t; ++it) {
            for (uint32_t il = 0; il < bundle->l; ++il) {
                for (uint32_t iw_kw2 = 0; iw_kw2 < bundle->w_kw2; ++iw_kw2) {
                    const int32_t *accumulation = cg_accumulation_packet(
                        bundle, ((size_t)it * bundle->l + il) * bundle->w_kw2 + iw_kw2);
                    uint32_t w_last = iw_kw2 == bundle->w_kw2 - 1 ? bundle->kw / 2 + 1 : 1;
                    size_t raw_word = 0;
                    for (uint32_t icoe = 0; icoe < bundle->coe; ++icoe) {
                        for (uint32_t iw_last = 0; iw_last < w_last; ++iw_last) {
                            for (uint32_t ir = 0; ir < MODEL_PE_ROWS; ++ir, ++raw_word) {
                                uint32_t y_h = il * MODEL_PE_ROWS + ir;
                                uint32_t y_w = iw_kw2 + iw_last;
                                uint32_t y_c = bundle->coe * it + icoe;
                                if (y_h >= bundle->h || y_w >= bundle->w || y_c >= co
                                    || y_h < bundle->csh_shift || y_w < bundle->csw_shift
                                    || (y_h - bundle->csh_shift) % bundle->csh != 0
                                    || (y_w - bundle->csw_shift) % bundle->csw != 0) {
                                    continue;
                                }
                                int32_t value = accumulation[raw_word];
                                if (has_bias) {
                                    value = cg_apply_bias(value, value_shift,
                                        cg_biases[bundle->bias_offset + y_c], bias_shift);
                                }
                                value = apply_plan(value, compiled.mode, &compiled, &fixed,
                                    ca_nzero, ca_shift, ca_pl_scale);
                                size_t index = cg_convolution_index(bundle,
                                    (y_h - bundle->csh_shift) / bundle->csh,
                                    (y_w - bundle->csw_shift) / bundle->csw, y_c);
                                if (residual != NULL) {
                                    value = cg_activate_bits(value + residual[index], aa_nzero,
                                        aa_shift, aa_pl_scale, MODEL_X_BITS);
                                }
                                target[index] = value;
                            }
                        }
                    }
                }
            }
        }
    }
    profile_stop(PROFILE_POSTPROCESS);

    profile_start();
    if (bundle->pool == 0 && target != cg_workspace->output) {
        memcpy(cg_workspace->output, target, bundle->output_words * sizeof(target[0]));
    }
    for (uint32_t pool_h = 0; bundle->pool != 0 && pool_h < bundle->ph; ++pool_h) {
        for (uint32_t pool_w = 0; pool_w < bundle->pw; ++pool_w) {
            for (uint32_t c = 0; c < co; ++c) {
                int32_t maximum = INT32_MIN;
                for (uint32_t kh = 0; kh < bundle->pkh; ++kh) {
                    for (uint32_t kw = 0; kw < bundle->pkw; ++kw) {
                        int32_t value = target[cg_convolution_index(
                            bundle, pool_h * bundle->psh + kh, pool_w * bundle->psw + kw, c)];
                        maximum = value > maximum ? value : maximum;
                    }
                }
                if (pool_compiled.mode == CG_FIXED_MODE_COMPILED) {
                    maximum = cg_fixed_apply(maximum, &pool_compiled);
                } else if (pool_compiled.mode == CG_FIXED_MODE_CHECKED) {
                    maximum = cg_fixed_postprocess(maximum, &model_pool_postprocess[bundle_index]);
                }
                cg_workspace->output[cg_output_index(bundle, pool_h, pool_w, c)] = maximum;
            }
        }
    }
    profile_stop(PROFILE_POOL);

    profile_start();
#if CG_VALIDATE
    cg_check_equal("output", bundle_index, cg_workspace->output,
        &model_expected_output[bundle_index][(size_t)cg_batch_image * MODEL_MAX_OUTPUT_WORDS],
        bundle->output_words, sizeof(model_expected_output[0][0]));
#endif
    if (bundle_index + 1 == MODEL_BUNDLE_COUNT) {
        /* The item's result (an image, or every image as the rows of a
         * batched dense layer); the output buffer is the next item's scratch. */
        memcpy(
            (int32_t *)(void *)cg_workspace->result + (size_t)cg_batch_image * bundle->output_words,
            cg_workspace->output, bundle->output_words * sizeof(int32_t));
    }

    if (bundle->is_softmax) {
        /* Upstream's softmax: over the channels of every position, so per
         * image (a batched dense layer has the images as rows). */
        float *softmax =
            (float *)(void *)cg_workspace->softmax + (size_t)cg_batch_image * bundle->output_words;
        float scale = (float)(1u << bundle->softmax_frac);
        for (size_t position = 0; position < bundle->output_words; position += co) {
            int32_t *output = &cg_workspace->output[position];
            float *probability = &softmax[position];
            int32_t maximum = output[0];
            float sum = 0.0f;
            for (size_t word = 1; word < co; ++word) {
                maximum = output[word] > maximum ? output[word] : maximum;
            }
            for (size_t word = 0; word < co; ++word) {
                /* Subtract before the float conversion; widen against overflow. */
                probability[word] = expf((float)((int64_t)output[word] - maximum) / scale);
                sum += probability[word];
            }
            for (size_t word = 0; word < co; ++word) {
                probability[word] /= sum;
#if CG_VALIDATE
                size_t expected = (size_t)cg_batch_image * bundle->output_words + position + word;
                if (!(fabsf(probability[word] - model_expected_softmax[expected])
                        <= MODEL_SOFTMAX_TOLERANCE)) {
                    uint32_t actual_bits;
                    uint32_t expected_bits;
                    memcpy(&actual_bits, &probability[word], sizeof(actual_bits));
                    memcpy(
                        &expected_bits, &model_expected_softmax[expected], sizeof(expected_bits));
                    cg_report_mismatch(
                        "softmax", cg_batch_image, position + word, actual_bits, expected_bits);
                }
#endif
            }
        }
        cg_log(2, "softmax", bundle->output_words, 0);
    }

    /* Keep the int8 output (or the prepool convolution) for a later concat
     * join. */
    const int32_t *retained = save->prepool ? cg_workspace->convolution : cg_workspace->output;
    for (size_t word = 0; save->slot != -1 && word < save->words; ++word) {
        if (retained[word] < INT8_MIN || retained[word] > INT8_MAX) {
            __builtin_trap();
        }
        cg_workspace->concat_values[cg_batch_image][save->offset + word] = retained[word];
    }

    if (bundle->add_out_buffer_idx != -1) {
        int8_t *slot = cg_workspace->residual[cg_batch_image][bundle->add_out_buffer_idx];
        for (size_t word = 0; word < bundle->output_words; ++word) {
            if (cg_workspace->output[word] < INT8_MIN || cg_workspace->output[word] > INT8_MAX) {
                __builtin_trap();
            }
            slot[word] = cg_workspace->output[word];
        }
        cg_log(2, "save_residual", bundle->add_out_buffer_idx, bundle->output_words);
#if CG_VALIDATE
        cg_check_residual(bundle->add_out_buffer_idx);
#endif
    }
    profile_stop(PROFILE_FINISH);
}

/* The fused pass. A fused bundle is postprocessed per packet straight into
 * the consumer's packed input (or the final output): no accumulation copy, no
 * int32 output and no separate pack. Four kinds (cgtools/fixture.py fuses()
 * mirrors them):
 *   plain    one pass, unit stride, no pool, residual, softmax, flatten or
 *            upsample, not a concat skip source, consumer not a concat join,
 *            same image and tiling as the consumer;
 *   pool     the same with a 2x2 stride-2 max pool (the consumer has half the
 *            height and width) and, when the bundle is a concat skip source,
 *            its prepool values kept for the decoder join;
 *   decoder  plain, but the consumer is a concat join: the values get the
 *            join's decoder quantizers and are written 2x2 upsampled at the
 *            decoder's channel offset (the pack adds the skip);
 *   last     the model's last bundle (no pool, softmax or upsample): the
 *            int32 NHWC result is written directly.
 * The arithmetic per value is cg_finish_bundle's; a value lands where the pack
 * would put it, in its tile and, for the first x_pad rows of a tile, in the
 * bottom padding of the tile above. A packet's column is computed first, then
 * stored run by run (stores that alternate between cache lines cost one DDR
 * write each). */
enum {
    CG_FUSED_CHANNELS = 1024,
    CG_FUSED_PLAIN = 1,
    CG_FUSED_POOL = 2,
    CG_FUSED_DECODER = 3,
    CG_FUSED_LAST = 4
};
/* Per-value arithmetic shapes with a loop of their own, chosen once per
 * bundle; any other shape runs apply_plan. */
enum {
    CG_ARITH_GENERAL = 0,
    CG_ARITH_QUANT = 1,
    CG_ARITH_BIAS_TABLE = 2,
    CG_ARITH_BIAS = 3,
    CG_ARITH_SHIFTED_ACTIVATE = 4
};
/* The fused pass's state and its per-packet working set, at fixed L1 cache
 * sets: the block is aligned to the 8 KB way, the cold bound array comes
 * first, and the per-packet scratch and the bundle's quantization table sit
 * at set offsets 0x1000-0x1bff, apart from the output banks (offset 0 of the
 * workspace) and from each other. With the scratch on the stack its sets
 * moved with every frame-size change; this placement measured 2.4%
 * faster on the board (stores that miss L1 go to the bus). */
static struct {
    cg_bias_bound_t bound[CG_FUSED_CHANNELS]; /* CG_ARITH_SHIFTED_ACTIVATE */
    int32_t values[CG_MAX_COE][MODEL_PE_ROWS]; /* a packet's column, per channel */
    int32_t table[CG_FIXED_TABLE_CODES]; /* copy of the bundle's table */
    /* decoder: the column upsampled, consumer tiles 2 il and 2 il + 1 */
    int32_t upsampled_low[CG_MAX_COE][MODEL_PE_ROWS];
    int32_t upsampled_high[CG_MAX_COE][MODEL_PE_ROWS];
    const model_bundle_t *bundle;
    const cg_fixed_postprocess_t *fixed;
    const cg_fixed_postprocess_t *pool_fixed;
    uint8_t *buffer; /* the current image's packed consumer input */
    int8_t *retain; /* pool: the current image's prepool values, or NULL */
    int kind;
    int bias_shifted;
    int arith;
    uint32_t x_pad; /* the consumer's: rows a tile repeats from below */
    uint32_t destination_w; /* the consumer's width */
    cg_fixed_compiled_t compiled;
    cg_v4_step_t vector_first; /* compiled.first as lanes */
    cg_fixed_compiled_t pool_compiled;
    cg_transition_quant_t decoder_quant;
    int32_t bias[CG_FUSED_CHANNELS];
    size_t base[CG_FUSED_CHANNELS]; /* element of channel c, tile 0, row 0 */
    size_t stride[CG_FUSED_CHANNELS]; /* elements per tile */
    /* pool: row-pair maxima of the last even column, per channel */
    int32_t carry[CG_FUSED_CHANNELS][MODEL_PE_ROWS / 2];
} fused __attribute__((aligned(8192)));
typedef char cg_fused_values_sets[offsetof(__typeof__(fused), values) % 8192 == 0x1000 ? 1 : -1];
typedef char cg_fused_table_sets[offsetof(__typeof__(fused), table) % 8192 == 0x1800 ? 1 : -1];

/* Prepares the fused pass when bundle `bundle_index` qualifies; returns 0
 * otherwise. A fused bundle's packets go to cg_fused_packet; neither
 * cg_finish_bundle nor the pack's own output half runs for it. */
static int cg_fused_begin(size_t bundle_index) {
    const model_bundle_t *bundle = &model_bundles[bundle_index];
    const model_concat_save_t *save = &model_concat_saves[bundle_index];
    const model_bundle_t *destination = &model_bundles[bundle_index + 1];
    const model_concat_plan_t *plan = &model_concat_plans[bundle_index + 1];
    int upsampled = bundle->upsample_h != 1 || bundle->upsample_w != 1;
    int kind = 0;

    if (bundle->p != 1 || bundle->csh != 1 || bundle->csw != 1 || bundle->csh_shift != 0
        || bundle->csw_shift != 0 || bundle->ch != bundle->h || bundle->cw != bundle->w
        || bundle->add_in_buffer_idx != -1 || bundle->add_out_buffer_idx != -1 || bundle->is_softmax
        || bundle->is_flatten || bundle->co > CG_FUSED_CHANNELS || bundle->coe > CG_MAX_COE) {
        kind = 0;
    } else if (bundle_index + 1 == MODEL_BUNDLE_COUNT) {
        if (bundle->pool == 0 && !upsampled && save->slot == -1
            && bundle->output_words == bundle->h * bundle->w * bundle->co) {
            kind = CG_FUSED_LAST;
        }
    } else if (plan->skip_bundle != -1) {
        if (bundle->pool == 0 && save->slot == -1 && bundle->upsample_h == 2
            && bundle->upsample_w == 2 && plan->decoder_channel_offset <= destination->ci
            && bundle->co <= destination->ci - plan->decoder_channel_offset
            && destination->h == 2 * bundle->h && destination->w == 2 * bundle->w
            && destination->l == (2 * bundle->h + MODEL_PE_ROWS - 1) / MODEL_PE_ROWS) {
            kind = CG_FUSED_DECODER;
        }
    } else if (upsampled || destination->ci != bundle->co) {
        kind = 0;
    } else if (bundle->pool == 0) {
        if (save->slot == -1 && destination->h == bundle->h && destination->w == bundle->w
            && destination->l == bundle->l) {
            kind = CG_FUSED_PLAIN;
        }
    } else if (bundle->pool == 1 && bundle->pkh == 2 && bundle->pkw == 2 && bundle->psh == 2
        && bundle->psw == 2 && bundle->h % 2 == 0 && bundle->w % 2 == 0
        && bundle->ph * 2 == bundle->h && bundle->pw * 2 == bundle->w
        && destination->h == bundle->ph && destination->w == bundle->pw
        && destination->l == (bundle->ph + MODEL_PE_ROWS - 1) / MODEL_PE_ROWS
        && (save->slot == -1 || save->prepool == 1)) {
        kind = CG_FUSED_POOL;
    }
    if (kind == 0) {
        return 0;
    }

    profile_start();
    fused.kind = kind;
    fused.bundle = bundle;
    fused.fixed = &model_fixed_postprocess[bundle_index];
    fused.compiled = model_fixed_compiled[bundle_index];
    if (fused.compiled.table != NULL) {
        memcpy(fused.table, fused.compiled.table, sizeof(fused.table));
        fused.compiled.table = fused.table;
    }
    fused.vector_first = cg_v4_step(&fused.compiled.first);
    fused.pool_fixed = &model_pool_postprocess[bundle_index];
    fused.pool_compiled = model_pool_compiled[bundle_index];
    /* Non-NULL: every image keeps its prepool values (set per image). */
    fused.retain =
        kind == CG_FUSED_POOL && save->slot != -1 ? cg_workspace->concat_values[0] : NULL;
    fused.bias_shifted = bundle->b_val_shift != 0 || bundle->b_bias_shift != 0;
    fused.arith = CG_ARITH_GENERAL;
    if (fused.compiled.mode == CG_FIXED_MODE_COMPILED && !bundle->is_bias
        && fused.compiled.kind == CG_FIXED_QUANT_RIGHT) {
        fused.arith = CG_ARITH_QUANT;
    }
    if (fused.compiled.mode == CG_FIXED_MODE_COMPILED && bundle->is_bias && !fused.bias_shifted) {
        if (fused.compiled.kind == CG_FIXED_QUANT_TABLE) {
            fused.arith = CG_ARITH_BIAS_TABLE;
        }
        if (fused.compiled.count == 0) {
            fused.arith = CG_ARITH_BIAS;
        }
    }
    if (fused.compiled.mode == CG_FIXED_MODE_NONE && bundle->is_bias && fused.bias_shifted) {
        /* Upstream's activation after a shifted bias. */
        int bounded = 1;
        for (uint32_t c = 0; c < bundle->co && bounded; ++c) {
            bounded = cg_bias_bound(cg_biases[bundle->bias_offset + c], bundle->b_val_shift,
                bundle->b_bias_shift, &fused.bound[c]);
        }
        if (bounded) {
            fused.arith = CG_ARITH_SHIFTED_ACTIVATE;
        }
    }
    cg_log(2, "fused_arith", bundle_index, fused.arith);
    for (uint32_t c = 0; c < bundle->co; ++c) {
        fused.bias[c] = bundle->is_bias ? cg_biases[bundle->bias_offset + c] : 0;
    }
    if (kind == CG_FUSED_LAST) {
        profile_stop(PROFILE_FUSED);
        return 1;
    }

    fused.x_pad = destination->x_pad;
    fused.destination_w = destination->w;
    uint32_t first_channel = 0;
    if (kind == CG_FUSED_DECODER) {
        fused.decoder_quant = model_concat_decoder_quant[bundle_index + 1];
        first_channel = plan->decoder_channel_offset;
    }
    uint32_t rows = MODEL_PE_ROWS + destination->x_pad;
    for (uint32_t c = 0; c < bundle->co; ++c) {
        size_t pass_offset;
        uint32_t xcm;
        uint32_t channel;
        cg_pack_channel_location(destination, first_channel + c, &pass_offset, &xcm, &channel);
        fused.base[c] = pass_offset + (size_t)channel * rows;
        fused.stride[c] = (size_t)xcm * rows;
    }
    /* Every destination channel is written, by this pass or the skip's pack:
     * only the padding needs the clear. */
    uint32_t written = bundle->co;
    if (kind == CG_FUSED_DECODER) {
        written += model_bundles[plan->skip_bundle].co;
    }
    for (uint32_t image = 0; image < bundle->n; ++image) {
        cg_batch_image = image;
        if (written == destination->ci) {
            cg_pack_clear_padding(cg_intermediate(bundle_index + 1), destination);
        } else {
            cg_pack_clear(cg_intermediate(bundle_index + 1), destination);
        }
    }
    profile_stop(PROFILE_FUSED);
    return 1;
}

/* Stores one column of the consumer's tile values (`count` rows from row
 * `row` of tile `tile_l`, column `x` of every channel), then the copies the
 * tile above keeps as its bottom padding. */
static void fused_store_column(int32_t values[][MODEL_PE_ROWS], uint32_t channel_first,
    uint32_t channels, uint32_t tile_l, uint32_t row, uint32_t count, uint32_t x) {
    uint8_t *buffer = fused.buffer;
    size_t tile = (size_t)tile_l * fused.destination_w + x;
    /* Rows of this tile below x_pad are also the padding of the tile above. */
    uint32_t pad_end = 0;
    if (tile_l != 0 && row < fused.x_pad) {
        pad_end = fused.x_pad - row < count ? fused.x_pad - row : count;
    }
    for (uint32_t icoe = 0; icoe < channels; ++icoe) {
        uint32_t c = channel_first + icoe;
        size_t flat = fused.base[c] + tile * fused.stride[c] + row;
        for (uint32_t r = 0; r < count; ++r) {
            cg_pack_store(buffer, flat + r, values[icoe][r]);
        }
    }
    for (uint32_t icoe = 0; icoe < channels && pad_end != 0; ++icoe) {
        uint32_t c = channel_first + icoe;
        size_t above =
            fused.base[c] + (tile - fused.destination_w) * fused.stride[c] + MODEL_PE_ROWS + row;
        for (uint32_t r = 0; r < pad_end; ++r) {
            cg_pack_store(buffer, above + r, values[icoe][r]);
        }
    }
}

/* Postprocesses one raw packet of the fused bundle (output iteration `it`,
 * row block `il`, column packet `iw_kw2`) into the consumer's packed input. */
static void cg_fused_packet(
    const model_ocm_word_t *raw, uint32_t it, uint32_t il, uint32_t iw_kw2) {
    /* Locals: stores to the packed input cannot alias them. */
    const model_bundle_t *bundle = fused.bundle;
    cg_fixed_compiled_t compiled = fused.compiled;
    const cg_fixed_postprocess_t *fixed = fused.fixed;
    int has_bias = bundle->is_bias != 0;
    int bias_shifted = fused.bias_shifted;
    int arith = fused.arith;
    cg_v4_step_t vector_first = fused.vector_first;
    /* Lanes that overflowed (bias add, shifted bias), checked once per packet. */
    cg_v4i overflow = cg_v4_splat(0);
    uint32_t value_shift = bundle->b_val_shift;
    uint32_t bias_shift = bundle->b_bias_shift;
    uint32_t ca_nzero = bundle->ca_nzero;
    uint32_t ca_shift = bundle->ca_shift;
    uint32_t ca_pl_scale = bundle->ca_pl_scale;
    uint32_t width = bundle->w;
    uint32_t w_last = iw_kw2 == bundle->w_kw2 - 1 ? bundle->kw / 2 + 1 : 1;
    uint32_t channel_first = bundle->coe * it;
    int32_t(*values)[MODEL_PE_ROWS] = fused.values;
    int32_t(*upsampled_low)[MODEL_PE_ROWS] = fused.upsampled_low;
    int32_t(*upsampled_high)[MODEL_PE_ROWS] = fused.upsampled_high;

    if (channel_first >= bundle->co) {
        return;
    }
    uint32_t channels = bundle->co - channel_first;
    channels = channels < bundle->coe ? channels : bundle->coe;
    /* The packet's lines were just invalidated: start all their fills now
     * (PLD) rather than one miss at a time in the loop. */
    for (uint32_t word = 0; word < channels * w_last * MODEL_PE_ROWS;
         word += 64 / sizeof(model_ocm_word_t)) {
        __builtin_prefetch(&raw[word]);
    }
    uint32_t rows = bundle->h - il * MODEL_PE_ROWS;
    rows = rows < MODEL_PE_ROWS ? rows : MODEL_PE_ROWS;
    for (uint32_t iw_last = 0; iw_last < w_last && iw_kw2 + iw_last < width; ++iw_last) {
        uint32_t x = iw_kw2 + iw_last;
        for (uint32_t icoe = 0; icoe < channels; ++icoe) {
            const model_ocm_word_t *source =
                raw + ((size_t)icoe * w_last + iw_last) * MODEL_PE_ROWS;
            int32_t bias = fused.bias[channel_first + icoe];
            int32_t *out = values[icoe];
            /* A full column, four lanes at a time: one loop per shape, its
             * constants hoisted so they stay in registers (a shared loop
             * reloaded them from the stack: 2% slower on the board). */
            int full = rows == MODEL_PE_ROWS && MODEL_PE_ROWS % 4 == 0;
            if (full && arith == CG_ARITH_QUANT) {
                for (uint32_t ir = 0; ir < MODEL_PE_ROWS; ir += 4) {
                    cg_v4i v;
                    memcpy(&v, &source[ir], sizeof(v));
                    v = cg_v4_step_right(v, &vector_first);
                    memcpy(&out[ir], &v, sizeof(v));
                }
                continue;
            }
            if (full && arith == CG_ARITH_BIAS_TABLE) {
                const int32_t *table = compiled.table;
                cg_v4i lanes_bias = cg_v4_splat(bias);
                for (uint32_t ir = 0; ir < MODEL_PE_ROWS; ir += 4) {
                    cg_v4i in;
                    memcpy(&in, &source[ir], sizeof(in));
                    cg_v4i v = (cg_v4i)((cg_v4u)in + (cg_v4u)lanes_bias);
                    overflow |= cg_v4_add_overflow_lanes(in, lanes_bias, v);
                    v = cg_v4_step_right(v, &vector_first);
                    /* The codes leave the lanes once, through memory (one
                     * vector store; lane moves to core registers are slow),
                     * then the table per value in place. */
                    memcpy(&out[ir], &v, sizeof(v));
                    out[ir] = table[out[ir] + 128];
                    out[ir + 1] = table[out[ir + 1] + 128];
                    out[ir + 2] = table[out[ir + 2] + 128];
                    out[ir + 3] = table[out[ir + 3] + 128];
                }
                continue;
            }
            if (full && arith == CG_ARITH_BIAS) {
                cg_v4i lanes_bias = cg_v4_splat(bias);
                for (uint32_t ir = 0; ir < MODEL_PE_ROWS; ir += 4) {
                    cg_v4i in;
                    memcpy(&in, &source[ir], sizeof(in));
                    cg_v4i v = (cg_v4i)((cg_v4u)in + (cg_v4u)lanes_bias);
                    overflow |= cg_v4_add_overflow_lanes(in, lanes_bias, v);
                    memcpy(&out[ir], &v, sizeof(v));
                }
                continue;
            }
            if (full && arith == CG_ARITH_SHIFTED_ACTIVATE) {
                cg_bias_bound_t *bound = &fused.bound[channel_first + icoe];
                for (uint32_t ir = 0; ir < MODEL_PE_ROWS; ir += 4) {
                    cg_v4i in;
                    memcpy(&in, &source[ir], sizeof(in));
                    overflow |= cg_v4_outside(in, bound);
                    cg_v4i v = cg_v4_activate_bits(cg_v4_bias_shift_lanes(in, value_shift, bound),
                        ca_nzero, ca_shift, ca_pl_scale, MODEL_X_BITS);
                    memcpy(&out[ir], &v, sizeof(v));
                }
                continue;
            }
            /* The bundle's shape decides the loop, not each value. */
            if (arith == CG_ARITH_QUANT) {
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    out[ir] = cg_fixed_step_right(source[ir], &compiled.first);
                }
            } else if (arith == CG_ARITH_BIAS_TABLE) {
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    out[ir] = compiled.table[cg_fixed_step_right(
                                                 cg_add_bias(source[ir], bias), &compiled.first)
                        + 128];
                }
            } else if (arith == CG_ARITH_BIAS) {
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    out[ir] = cg_add_bias(source[ir], bias);
                }
            } else if (arith == CG_ARITH_SHIFTED_ACTIVATE) {
                cg_bias_bound_t bound = fused.bound[channel_first + icoe];
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    out[ir] = cg_activate_bits(cg_bias_shifted(source[ir], value_shift, &bound),
                        ca_nzero, ca_shift, ca_pl_scale, MODEL_X_BITS);
                }
            } else {
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    int32_t value = source[ir];
                    if (has_bias) {
                        value = bias_shifted ? cg_apply_bias(value, value_shift, bias, bias_shift)
                                             : cg_add_bias(value, bias);
                    }
                    out[ir] = apply_plan(
                        value, compiled.mode, &compiled, fixed, ca_nzero, ca_shift, ca_pl_scale);
                }
            }
        }

        if (fused.kind == CG_FUSED_PLAIN) {
            fused_store_column(values, channel_first, channels, il, 0, rows, x);
        } else if (fused.kind == CG_FUSED_LAST) {
            /* NHWC, row by row with the channels contiguous. */
            for (uint32_t ir = 0; ir < rows; ++ir) {
                int32_t *target = cg_workspace->result[cg_batch_image]
                    + (((size_t)il * MODEL_PE_ROWS + ir) * width + x) * bundle->co + channel_first;
                for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                    target[icoe] = values[icoe][ir];
                }
            }
        } else if (fused.kind == CG_FUSED_DECODER) {
            /* Source rows 8 il .. are consumer rows 16 il .., two per value, in
             * tiles 2 il and 2 il + 1; columns 2 x and 2 x + 1. */
            cg_transition_quant_t quant = fused.decoder_quant;
            uint32_t upper = 2 * rows < MODEL_PE_ROWS ? 2 * rows : MODEL_PE_ROWS;
            for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                for (uint32_t ir = 0; ir < rows; ++ir) {
                    int32_t value = cg_transition_apply(values[icoe][ir], &quant);
                    int32_t *half =
                        ir < MODEL_PE_ROWS / 2 ? upsampled_low[icoe] : upsampled_high[icoe];
                    half[2 * (ir % (MODEL_PE_ROWS / 2))] = value;
                    half[2 * (ir % (MODEL_PE_ROWS / 2)) + 1] = value;
                }
            }
            fused_store_column(upsampled_low, channel_first, channels, 2 * il, 0, upper, 2 * x);
            fused_store_column(upsampled_low, channel_first, channels, 2 * il, 0, upper, 2 * x + 1);
            if (2 * rows > MODEL_PE_ROWS) {
                fused_store_column(upsampled_high, channel_first, channels, 2 * il + 1, 0,
                    2 * rows - MODEL_PE_ROWS, 2 * x);
                fused_store_column(upsampled_high, channel_first, channels, 2 * il + 1, 0,
                    2 * rows - MODEL_PE_ROWS, 2 * x + 1);
            }
        } else {
            /* Pool. Prepool values for the decoder join, NHWC: row by row with
             * the channels contiguous. */
            uint32_t pairs = rows / 2;
            for (uint32_t ir = 0; fused.retain != NULL && ir < rows; ++ir) {
                int8_t *target = fused.retain
                    + (((size_t)il * MODEL_PE_ROWS + ir) * width + x) * bundle->co + channel_first;
                for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                    if (values[icoe][ir] < INT8_MIN || values[icoe][ir] > INT8_MAX) {
                        __builtin_trap();
                    }
                    target[icoe] = values[icoe][ir];
                }
            }
            for (uint32_t icoe = 0; icoe < channels; ++icoe) {
                int32_t *carry = fused.carry[channel_first + icoe];
                for (uint32_t k = 0; k < pairs; ++k) {
                    int32_t a = values[icoe][2 * k];
                    int32_t b = values[icoe][2 * k + 1];
                    int32_t pair = a > b ? a : b;
                    if ((x & 1) == 0) {
                        carry[k] = pair;
                        continue;
                    }
                    int32_t pooled = carry[k] > pair ? carry[k] : pair;
                    if (fused.pool_compiled.mode == CG_FIXED_MODE_COMPILED) {
                        pooled = cg_fixed_apply(pooled, &fused.pool_compiled);
                    } else if (fused.pool_compiled.mode == CG_FIXED_MODE_CHECKED) {
                        pooled = cg_fixed_postprocess(pooled, fused.pool_fixed);
                    }
                    values[icoe][k] = pooled;
                }
            }
            /* Block il's pooled rows are rows (il % 2) * 4 .. of consumer tile
             * il / 2. */
            if ((x & 1) != 0) {
                fused_store_column(values, channel_first, channels, il / 2,
                    (il % 2) * (MODEL_PE_ROWS / 2), pairs, x / 2);
            }
        }
    }
    if (cg_v4_any(overflow)) {
        __builtin_trap();
    }
}

#endif
