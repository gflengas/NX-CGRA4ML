/* Packing a bundle's output into the next bundle's tiled accelerator input,
 * including concat joins, upsampling and flatten. Tile [l][w][c][r] (r <
 * PE_ROWS + x_pad) holds input row l * PE_ROWS + r: rows past PE_ROWS repeat
 * the next tile's first rows, and the last tile's padding is zero. */
#ifndef CG_PACK_H
#define CG_PACK_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cg_config.h"
#include "cg_fixedpoint.h"
#include "cg_workspace.h"
#include "model.h"

/* A concat join's quantizers, compiled at export: an optional resize step,
 * then the join step to the accelerator's input codes. */
static int32_t cg_transition_apply(int32_t value, const cg_transition_quant_t *quant) {
    if (quant->has_resize) {
        value = cg_fixed_step(value, &quant->resize);
    }
    return cg_fixed_step(value, &quant->join);
}

/* Writes MODEL_X_BITS element `flat_index` of a packed input buffer. */
static void cg_pack_store(uint8_t *buffer, size_t flat_index, int32_t value) {
    if (MODEL_X_BITS == 8) {
        buffer[flat_index] = value;
        return;
    }
    uint32_t per_byte = 8 / MODEL_X_BITS;
    uint32_t mask = (1u << MODEL_X_BITS) - 1;
    uint32_t shift = flat_index % per_byte * MODEL_X_BITS;
    uint8_t *byte = &buffer[flat_index / per_byte];
    *byte = (*byte & ~(mask << shift)) | ((value & mask) << shift);
}

/* Tiled location of destination channel `c`: element offset of its pass,
 * the pass width and the channel within the pass. */
static void cg_pack_channel_location(const model_bundle_t *destination, uint32_t c,
    size_t *pass_offset, uint32_t *xcm, uint32_t *channel_in_pass) {
    if (c < destination->cm_p0) {
        *pass_offset = 0;
        *xcm = destination->cm_p0;
        *channel_in_pass = c;
        return;
    }
    uint32_t pass = (c - destination->cm_p0) / destination->cm;
    *pass_offset = (size_t)(destination->cm_p0 + pass * destination->cm) * destination->xp_words;
    *xcm = destination->cm;
    *channel_in_pass = (c - destination->cm_p0) % destination->cm;
}

/* Clears `destination`'s packed input: padding and rows a pack does not write
 * are zero. */
static void cg_pack_clear(uint8_t *buffer, const model_bundle_t *destination) {
    memset(buffer, 0,
        destination->x_bytes_first_pass + (size_t)(destination->p - 1) * destination->x_bytes);
}

/* Channel locations of one block, so the packing loop has no division. */
enum { CG_PACK_TABLE_CHANNELS = 1024 };
static uint32_t pack_channel_offset[CG_PACK_TABLE_CHANNELS];
static uint32_t pack_channel_xcm[CG_PACK_TABLE_CHANNELS];
static uint32_t pack_channel_index[CG_PACK_TABLE_CHANNELS];

/* Packs `values` of `source` (int32, or int8 for a retained concat skip) at
 * destination channel `channel_offset`, through `quant` unless NULL. The
 * caller cleared the buffer. */
static void pack_source(uint8_t *buffer, const model_bundle_t *source,
    const model_bundle_t *destination, const void *values, uint32_t value_bytes,
    uint32_t channel_offset, const cg_transition_quant_t *quant) {
    uint32_t rows = MODEL_PE_ROWS + destination->x_pad;
    const int32_t *words = values;
    const int8_t *bytes = values;
    size_t pass_offset;
    uint32_t xcm;
    uint32_t channel;

    if (source->is_flatten) {
        /* Dense destination channel (h * pw + w) * co + c holds source value
         * (h, w, c) at the current image's row: image i is row i % PE_ROWS of
         * tile i / PE_ROWS (upstream's batched flatten). */
        uint32_t row = cg_batch_image % MODEL_PE_ROWS;
        uint32_t tile = cg_batch_image / MODEL_PE_ROWS;
        for (uint32_t c = 0; c < destination->ci; ++c) {
            cg_pack_channel_location(destination, c, &pass_offset, &xcm, &channel);
            size_t index = cg_output_index(
                source, c / source->co / source->pw, c / source->co % source->pw, c % source->co);
            cg_pack_store(
                buffer, pass_offset + ((size_t)tile * xcm + channel) * rows + row, words[index]);
        }
        return;
    }

    /* An int8 source goes through its quantizer as a table of every code's
     * result; the identity copies the bytes. A local copy of the
     * quantizer stays in registers across the stores. */
    cg_transition_quant_t join = quant != NULL ? *quant : (cg_transition_quant_t){ 0 };
    int8_t code_table[256];
    int identity = 1;
    for (int32_t code = -128; value_bytes == 1 && quant != NULL && code < 128; ++code) {
        code_table[code + 128] = cg_transition_apply(code, &join);
        identity &= code_table[code + 128] == code;
    }

    size_t source_row[2 * MODEL_PE_ROWS];
    for (uint32_t block = 0; block < source->co; block += CG_PACK_TABLE_CHANNELS) {
        uint32_t channels = source->co - block;
        channels = channels < CG_PACK_TABLE_CHANNELS ? channels : CG_PACK_TABLE_CHANNELS;
        for (uint32_t c = 0; c < channels; ++c) {
            cg_pack_channel_location(destination, channel_offset + block + c, &pass_offset,
                &pack_channel_xcm[c], &pack_channel_index[c]);
            pack_channel_offset[c] = pass_offset;
        }
        for (uint32_t l = 0; l < destination->l; ++l) {
            /* Source row of each tile row, once per row block. */
            uint32_t valid_rows = destination->h - l * MODEL_PE_ROWS;
            valid_rows = valid_rows < rows ? valid_rows : rows;
            for (uint32_t r = 0; r < valid_rows; ++r) {
                source_row[r] = (size_t)((l * MODEL_PE_ROWS + r) / source->upsample_h) * source->pw;
            }
            for (uint32_t w = 0; w < destination->w; ++w) {
                size_t column = w / source->upsample_w;
                for (uint32_t c = 0; c < channels; ++c) {
                    size_t flat = pack_channel_offset[c]
                        + (((size_t)l * destination->w + w) * pack_channel_xcm[c]
                              + pack_channel_index[c])
                            * rows;
                    size_t first = column * source->co + block + c;
                    /* The source's layout decides the loop, not each value. */
                    if (value_bytes == 4) {
                        for (uint32_t r = 0; r < valid_rows; ++r) {
                            int32_t value = words[source_row[r] * source->co + first];
                            value = quant != NULL ? cg_transition_apply(value, &join) : value;
                            cg_pack_store(buffer, flat + r, value);
                        }
                    } else if (identity) {
                        for (uint32_t r = 0; r < valid_rows; ++r) {
                            cg_pack_store(
                                buffer, flat + r, bytes[source_row[r] * source->co + first]);
                        }
                    } else {
                        for (uint32_t r = 0; r < valid_rows; ++r) {
                            cg_pack_store(buffer, flat + r,
                                code_table[bytes[source_row[r] * source->co + first] + 128]);
                        }
                    }
                }
            }
        }
    }
}

/* Packs bundle `bundle_index`'s output (the current image) as the next
 * bundle's input, unless the fused pass already did; for a concat join the
 * retained skip half is packed here either way. Debug builds then check the
 * packed input. */
static void cg_pack(size_t bundle_index, int fused) {
    size_t consumer = bundle_index + 1;
    const model_bundle_t *source = &model_bundles[bundle_index];
    const model_bundle_t *destination = &model_bundles[consumer];
    const model_concat_plan_t *plan = &model_concat_plans[consumer];
    uint8_t *buffer = cg_intermediate(consumer);

    cg_log(2, "pack", bundle_index, fused);
    if (plan->skip_bundle == -1 && !fused) {
        /* A destination that holds the images as rows is cleared once. */
        if (destination->n != 1 || cg_batch_image == 0) {
            cg_pack_clear(buffer, destination);
        }
        pack_source(buffer, source, destination, cg_workspace->output, 4, 0, NULL);
    }
    if (plan->skip_bundle != -1) {
        if (!fused) {
            cg_pack_clear(buffer, destination);
            pack_source(buffer, source, destination, cg_workspace->output, 4,
                plan->decoder_channel_offset, &model_concat_decoder_quant[consumer]);
        }
        /* The skip's pooled output, or its prepool convolution. */
        model_bundle_t skip = model_bundles[plan->skip_bundle];
        if (plan->skip_prepool) {
            skip.ph = skip.ch;
            skip.pw = skip.cw;
        }
        pack_source(buffer, &skip, destination,
            &cg_workspace->concat_values[cg_batch_image][plan->offset], 1,
            plan->skip_channel_offset, &model_concat_skip_quant[consumer]);
    }

#if CG_VALIDATE
    /* The golden holds every image's input: a batched destination checks the
     * current image's slice, one holding the images as rows checks the whole
     * input after the last image. */
    size_t begin = model_expected_intermediate_offsets[bundle_index];
    size_t image_bytes = (model_expected_intermediate_offsets[consumer] - begin) / destination->n;
    if (destination->n == 1 && cg_batch_image + 1 != source->n) {
        return;
    }
    size_t image_offset = destination->n == 1 ? 0 : cg_batch_image * image_bytes;
    cg_check_equal("packed_input", consumer, buffer,
        &model_expected_intermediate[begin + image_offset], image_bytes, 1);
#endif
}

#endif
