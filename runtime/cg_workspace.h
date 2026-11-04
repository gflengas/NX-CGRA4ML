/* DDR workspace layout and packet/tensor index helpers.
 *
 * Batch N: per transfer the accelerator sends every image's packets in turn,
 * so state an image keeps while the others stream is indexed by the image
 * (cg_batch_image); the convolution and output buffers are scratch of one
 * image's postprocess and pack (the images finish in turn). */
#ifndef CG_WORKSPACE_H
#define CG_WORKSPACE_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cg_config.h"
#include "model.h"
#include "nx_log.h"
#include "nx_ddr_layout.h"

typedef struct {
    model_ocm_word_t ocm[2][MODEL_OCM_WORDS];
    int32_t accumulation[MODEL_BATCH][MODEL_ACCUMULATION_WORDS];
    int32_t convolution[MODEL_MAX_CONV_WORDS];
    int32_t output[MODEL_MAX_OUTPUT_WORDS];
    float softmax[MODEL_BATCH][MODEL_SOFTMAX_WORDS];
    int8_t residual[MODEL_BATCH][MODEL_RESIDUAL_SLOTS][MODEL_RESIDUAL_WORDS];
    int8_t concat_values[MODEL_BATCH][MODEL_CONCAT_WORDS];
    /* Packed bundle inputs, alternating so the next bundle's input is written
     * while the accelerator reads the current one; the images follow each
     * other (cg_intermediate). */
    uint8_t intermediate[2][MODEL_INTERMEDIATE_BYTES] __attribute__((aligned(64)));
    /* Every image's final output; written whole, so it follows the part
     * cleared before each inference. */
    int32_t result[MODEL_BATCH][MODEL_FINAL_WORDS];
} cg_workspace_t;

#define cg_workspace ((cg_workspace_t *)(uintptr_t)NX_DDR_WORKSPACE_BASE)
#define cg_payload ((uint8_t *)(uintptr_t)NX_DDR_PAYLOAD_BASE)
/* Compile-time checks (C99): a negative array size fails the build. */
typedef char cg_workspace_fits[sizeof(cg_workspace_t) <= NX_DDR_WORKSPACE_BYTES ? 1 : -1];
typedef char cg_payload_fits[((MODEL_PAYLOAD_BYTES + 3) & ~3) <= NX_DDR_PAYLOAD_CAPACITY ? 1 : -1];

/* The image of the batch being processed. */
static uint32_t cg_batch_image;

static size_t cg_convolution_index(
    const model_bundle_t *bundle, uint32_t h, uint32_t w, uint32_t c) {
    return ((size_t)h * bundle->cw + w) * bundle->co + c;
}

static size_t cg_output_index(const model_bundle_t *bundle, uint32_t h, uint32_t w, uint32_t c) {
    return ((size_t)h * bundle->pw + w) * bundle->co + c;
}

/* Packed input of bundle `bundle_index` (>= 1), every image. */
static uint8_t *cg_intermediate_batch(size_t bundle_index) {
    return cg_workspace->intermediate[bundle_index & 1];
}

/* The current image's packed input of bundle `bundle_index` (>= 1): its slice
 * when the bundle runs the batch, the whole input when the bundle runs the
 * images as rows of one item (a dense layer after a flatten). */
static uint8_t *cg_intermediate(size_t bundle_index) {
    const model_bundle_t *bundle = &model_bundles[bundle_index];
    size_t offset = bundle->n == 1 ? 0 : (size_t)cg_batch_image * bundle->x_bytes_first_pass;
    return cg_intermediate_batch(bundle_index) + offset;
}

/* Start of a packet's raw words in the current image's accumulation buffer.
 * Each row block has w_kw2 packets of one column, except the last, which has
 * kw/2+1: every padded raw word is kept, without the unused capacity after
 * the shorter packets. */
static int32_t *cg_accumulation_packet(const model_bundle_t *bundle, size_t packet) {
    size_t column_words = (size_t)MODEL_PE_ROWS * bundle->coe;
    size_t offset = (packet / bundle->w_kw2 * bundle->w + packet % bundle->w_kw2) * column_words;
    return &cg_workspace->accumulation[cg_batch_image][offset];
}

#if CG_VALIDATE
/* Golden validation: every raw packet, bundle output, residual, softmax and
 * packed input is compared with the model's goldens. */

/* Prints `validate: <what>_mismatch a=.. b=.. actual=.. expected=..` and
 * traps; `a` and `b` locate the value. */
static void cg_report_mismatch(
    const char *what, uint32_t a, uint32_t b, uint32_t actual, uint32_t expected) {
    nx_puts("validate: ");
    nx_puts(what);
    nx_puts("_mismatch a=");
    nx_put_hex32(a);
    nx_puts(" b=");
    nx_put_hex32(b);
    nx_puts(" actual=");
    nx_put_hex32(actual);
    nx_puts(" expected=");
    nx_put_hex32(expected);
    nx_puts("\r\n");
    __builtin_trap();
}

/* Compares `count` elements of `element_bytes` (1 or 4) and reports the first
 * difference as (what, a, index). */
static void cg_check_equal(const char *what, uint32_t a, const void *actual, const void *expected,
    size_t count, size_t element_bytes) {
    const uint8_t *left = actual;
    const uint8_t *right = expected;
    if (memcmp(actual, expected, count * element_bytes) == 0) {
        return;
    }
    for (size_t index = 0; index < count; ++index) {
        uint32_t left_value = left[index];
        uint32_t right_value = right[index];
        if (element_bytes == 4) {
            memcpy(&left_value, &left[index * 4], 4);
            memcpy(&right_value, &right[index * 4], 4);
        }
        if (left_value != right_value) {
            cg_report_mismatch(what, a, index, left_value, right_value);
        }
    }
}
#endif

#endif
