/* CGRA4ML inference runtime for the NG-ULTRA SoC: receives the payload from
 * the host over the DDR mailbox, runs every bundle of the model on the
 * accelerator and publishes the result. The whole runtime is this one
 * translation unit (plus the generated model tables, model.c). */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cg_config.h"
#include "cg_device.h"
#include "cg_host_link.h"
#include "cg_pack.h"
#include "cg_postprocess.h"
#include "cg_workspace.h"
#include "model.h"
#include "nx_log.h"
#include "nx_platform.h"

#if MODEL_DDR_PROTOCOL_VERSION != 2
#error "regenerate the DDR-only model tables for protocol version 2"
#endif

#define cg_mailbox ((cg_mailbox_t *)(uintptr_t)NX_DDR_MAILBOX_BASE)

/* Requests of the session, learned from the first request. */
static uint32_t session_requests;

static void external_fail(const char *reason) {
    nx_puts("external: ");
    nx_puts(reason);
    nx_puts("\r\n");
    __builtin_trap();
}

/* Waits for the host's `command`; `bytes` is what the host transfers before
 * answering (it extends the timeout). No timeout when `timeout` is NULL. */
static void wait_external_request(
    uint32_t command, uint32_t sequence, const char *timeout, uint32_t bytes) {
    volatile cg_load_request_t *request = &cg_mailbox->request;
    uint64_t start = nx_ticks();
    for (uint32_t poll = 0;; ++poll) {
        if (timeout != NULL && nx_host_wait_expired(start, poll, bytes)) {
            external_fail(timeout);
        }
        nx_host_wait_step(poll);
        nx_dma_from_device(&cg_mailbox->request, sizeof(cg_mailbox->request));
        if (request->command != 0) {
            break;
        }
    }
    /* Completion is the host's final write: refresh all metadata after
     * observing it, including identity words on the next cache line. */
    nx_dma_from_device(&cg_mailbox->request, sizeof(cg_mailbox->request));
    if (request->command != command || request->sequence != sequence) {
        external_fail("invalid command or sequence");
    }
    for (int i = 0; i < 8; ++i) {
        if (request->identity[i] != model_identity[i]) {
            external_fail("identity mismatch");
        }
    }
    if (session_requests != 0 && request->session_requests != session_requests) {
        external_fail("session count mismatch");
    }
}

static void clear_external_request(void) {
    memset(&cg_mailbox->request, 0, sizeof(cg_mailbox->request));
    nx_dma_prepare_from_device(&cg_mailbox->request, sizeof(cg_mailbox->request));
}

/* Waits for, checks and accepts payload `sequence` (1: parameters and input,
 * later: the input). */
static void cg_host_receive_payload(uint32_t sequence) {
    uint32_t offset = sequence == 1 ? 0 : MODEL_WB_BYTES;
    uint32_t logical_bytes = sequence == 1 ? MODEL_PAYLOAD_BYTES : MODEL_INPUT_BYTES;
    size_t transfer_bytes = (logical_bytes + 3) & ~3u;
    uint8_t *destination = cg_payload + offset;
    volatile cg_load_request_t *request = &cg_mailbox->request;
    cg_load_status_t *status = &cg_mailbox->status;

    if (sequence == 1) {
        memset(cg_mailbox, 0, sizeof(*cg_mailbox));
    }
    clear_external_request();
    nx_dma_prepare_from_device(destination, transfer_bytes);
    status->sequence = sequence;
    status->address = (uintptr_t)destination;
    status->bytes = logical_bytes;
    status->parameter_bytes = MODEL_WB_BYTES;
    memcpy(status->identity, model_identity, sizeof(model_identity));
    status->state = CG_LOAD_READY;
    nx_dma_to_device(status, sizeof(*status));
    nx_puts("external: ready identity=" MODEL_IDENTITY_SHA256 " address=");
    nx_put_u64(status->address);
    nx_puts(" bytes=");
    nx_put_u64(logical_bytes);
    nx_puts(" mailbox=");
    nx_put_u64(NX_DDR_MAILBOX_BASE);
    nx_puts(" sequence=");
    nx_put_u64(sequence);
    nx_puts("\r\n");

    /* READY is intentional idle; BEGIN starts the bounded transfer. */
    wait_external_request(CG_LOAD_BEGIN, sequence, NULL, 0);
    if (request->address != (uintptr_t)destination || request->bytes != logical_bytes) {
        external_fail("invalid payload range or length");
    }
    if (sequence == 1) {
        if (request->session_requests == 0) {
            external_fail("invalid session count");
        }
        if (CG_VALIDATE && request->session_requests != 1) {
            external_fail("checked application requires one request");
        }
        session_requests = request->session_requests;
    }
    clear_external_request();
    status->session_requests = session_requests;
    /* Metadata visible before the state the host polls. */
    nx_dma_to_device(status, sizeof(*status));
    status->state = CG_LOAD_RECEIVING;
    nx_dma_to_device(status, sizeof(*status));
    wait_external_request(CG_LOAD_COMPLETE, sequence, "load timeout", logical_bytes);
    if (request->address != (uintptr_t)destination || request->bytes != logical_bytes) {
        external_fail("invalid payload range or length");
    }
    nx_dma_from_device(destination, transfer_bytes);
    if (cg_crc32(destination, logical_bytes) != request->crc32) {
        external_fail("payload CRC mismatch");
    }
    /* Inference never writes the parameters: this also catches a host write
     * between requests, including outside the input's last cache line. */
    nx_dma_from_device(cg_payload, MODEL_WB_BYTES);
    if (cg_crc32(cg_payload, MODEL_WB_BYTES) != MODEL_PARAMETERS_CRC32) {
        external_fail("parameter CRC mismatch");
    }
    status->state = CG_LOAD_RUNNING;
    nx_dma_to_device(status, sizeof(*status));
    nx_puts("external: accepted sequence=");
    nx_put_u64(sequence);
    nx_puts("\r\n");
}

/* Publishes every image's result and waits until the host consumed it. */
static void cg_host_publish_result(uint32_t sequence) {
    uint32_t is_float = model_bundles[MODEL_BUNDLE_COUNT - 1].is_softmax;
    void *result = is_float ? (void *)cg_workspace->softmax : (void *)cg_workspace->result;
    uint32_t words = MODEL_BATCH * MODEL_FINAL_WORDS;
    cg_load_status_t *status = &cg_mailbox->status;
    nx_dma_to_device(result, words * sizeof(uint32_t));
    clear_external_request();
    status->result_address = (uintptr_t)result;
    status->result_words = words;
    status->result_is_float = is_float;
    status->state = CG_LOAD_RESULT;
    nx_dma_to_device(status, sizeof(*status));
    nx_puts("external: result identity=" MODEL_IDENTITY_SHA256 " sequence=");
    nx_put_u64(sequence);
    nx_puts(" address=");
    nx_put_u64(status->result_address);
    nx_puts(" words=");
    nx_put_u64(words);
    nx_puts(" float=");
    nx_put_u64(is_float);
    nx_puts(" mailbox=");
    nx_put_u64(NX_DDR_MAILBOX_BASE);
    nx_puts("\r\n");
    wait_external_request(CG_RESULT_CONSUMED, sequence, "result timeout", words * sizeof(uint32_t));
}

/* Marks the session complete after its last request; returns 1 if so. */
static int cg_host_session_complete(uint32_t sequence) {
    if (sequence != session_requests) {
        return 0;
    }
    cg_mailbox->status.state = CG_SESSION_COMPLETE;
    nx_dma_to_device(&cg_mailbox->status, sizeof(cg_mailbox->status));
    nx_puts("external: session complete requests=");
    nx_put_u64(session_requests);
    nx_puts("\r\n");
    return 1;
}

typedef char
    cg_bundle_table_fits[REG_BUNDLE_TABLE + MODEL_BUNDLE_COUNT * 8 <= REG_WINDOW_WORDS ? 1 : -1];

/* The output DMA has no unaligned support: both banks start on 16 bytes. */
typedef char cg_banks_aligned[sizeof(model_ocm_word_t) * MODEL_OCM_WORDS % 16 == 0 ? 1 : -1];

/* The output bank the accelerator fills next; alternates across launches. */
static uint32_t device_bank;

/* One whole inference of the payload in DDR; returns its counter ticks.
 *
 * One launch runs the model: every bundle's descriptor, start, then per
 * bundle its output packets through two ping-pong banks (wait until the bank
 * is written, read it, hand it back); the CPU packs each next bundle's input
 * and marks it ready; finish after the last. */
static uint64_t run_inference(uint32_t iteration) {
    uint64_t start = nx_ticks();

    if (CG_PROFILE) {
        memset(profile_ticks, 0, sizeof(profile_ticks));
    }
    /* The workspace is not cleared: every buffer is written before it is
     * read. The host wrote the weights and input; the CPU never writes
     * them. */
    profile_start();
    nx_dma_to_device(cg_payload, MODEL_PAYLOAD_BYTES);
    profile_stop(PROFILE_CLEAN);

    profile_start();
    cg_log(2, "launch", MODEL_BUNDLE_COUNT, 0);
    nx_cache_clean_invalidate(cg_workspace->ocm[0], sizeof(cg_workspace->ocm[0]));
    nx_cache_clean_invalidate(cg_workspace->ocm[1], sizeof(cg_workspace->ocm[1]));
    write_reg(REG_START, 0);
    write_reg(REG_BUNDLE_DONE, 0);
    write_reg(REG_DONE_READ0, 0);
    write_reg(REG_DONE_READ1, 0);
    write_reg(REG_DONE_WRITE0, 0);
    write_reg(REG_DONE_WRITE1, 0);
    write_reg(REG_WEIGHTS_DONE, 0);
    write_reg(REG_PIXELS_DONE, 0);
    write_reg(REG_OUTPUT_DONE, 0);
    write_reg(REG_OCM_BASE0, (uintptr_t)cg_workspace->ocm[0]);
    write_reg(REG_OCM_BASE1, (uintptr_t)cg_workspace->ocm[1]);
    /* The weights of every bundle follow each other in the payload; the
     * controller advances through them. */
    write_reg(REG_WEIGHTS_BASE, (uintptr_t)cg_payload);
    write_reg(REG_BUNDLE_COUNT, MODEL_BUNDLE_COUNT);
    for (size_t b = 0; b < MODEL_BUNDLE_COUNT; ++b) {
        const model_bundle_t *bundle = &model_bundles[b];
        uint32_t word = REG_BUNDLE_TABLE + b * 8;
        uint8_t *input = b == 0 ? &cg_payload[MODEL_WB_BYTES] : cg_intermediate_batch(b);
        /* Input sizes are per item; a batched bundle's items follow each
         * other. */
        write_reg(word + 0, (uintptr_t)input);
        write_reg(word + 1, bundle->x_bytes_first_pass * bundle->n);
        write_reg(word + 2, bundle->x_bytes * bundle->n);
        write_reg(word + 3, bundle->w_bytes_first_pass);
        write_reg(word + 4, bundle->w_bytes);
        write_reg(word + 5, bundle->transfers_and_passes);
        write_reg(word + 6, bundle->header_lo);
        write_reg(word + 7, bundle->header_hi);
    }
    write_reg(REG_DONE_READ0, 1);
    write_reg(REG_DONE_READ1, 1);
    /* Bundle 0's input (the payload) is ready. */
    write_reg(REG_BUNDLE_DONE, 1);
    nx_sim_event("configure", (uintptr_t)&cg_payload[MODEL_WB_BYTES], MODEL_BUNDLE_COUNT);
    write_reg(REG_START, 1);
    wait_reg(REG_START, 0);
    wait_reg(REG_BUNDLE_DONE, 0);
    cg_log(2, "started", device_bank, 0);
    nx_sim_event("start", device_bank, 2);
    profile_stop(PROFILE_CONTROL);

    for (size_t b = 0; b < MODEL_BUNDLE_COUNT; ++b) {
        const model_bundle_t *bundle = &model_bundles[b];
        int last = b + 1 == MODEL_BUNDLE_COUNT;
        size_t packet = 0;
#if CG_VALIDATE
        size_t expected_word = model_expected_raw_offsets[b];
#endif
        cg_log(1, "bundle_begin", b, iteration + 1);
        int fuse = cg_fused_begin(b);
        cg_log(2, "fused", b, fuse);
        cg_log(2, "run_bundle", b, bundle->t);
#if CG_VALIDATE
        for (uint32_t image = 0; bundle->add_in_buffer_idx != -1 && image < bundle->n; ++image) {
            cg_batch_image = image;
            cg_check_residual(bundle->add_in_buffer_idx);
        }
#endif

        /* No clear: pass 0 assigns every packet word. Per transfer, the
         * accelerator sends every image of the batch in turn. */
        for (uint32_t ip = 0; ip < bundle->p; ++ip) {
            for (uint32_t it = 0; it < bundle->t; ++it) {
                for (uint32_t image = 0; image < bundle->n; ++image) {
                    cg_batch_image = image;
                    if (fuse && !last) {
                        fused.buffer = cg_intermediate(b + 1);
                    }
                    if (fuse && fused.retain != NULL) {
                        fused.retain =
                            &cg_workspace->concat_values[image][model_concat_saves[b].offset];
                    }
                    for (uint32_t il = 0; il < bundle->l; ++il) {
                        for (uint32_t iw_kw2 = 0; iw_kw2 < bundle->w_kw2; ++iw_kw2, ++packet) {
                            uint32_t w_last = iw_kw2 == bundle->w_kw2 - 1 ? bundle->kw / 2 + 1 : 1;
                            size_t words = MODEL_PE_ROWS * bundle->coe * w_last;
                            /* A fused bundle reads only the packet's valid
                             * output lanes, the first ones: invalidate just
                             * those. */
                            size_t read_words = words;
                            if (fuse) {
                                uint32_t lanes = bundle->co - bundle->coe * it < bundle->coe
                                    ? bundle->co - bundle->coe * it
                                    : bundle->coe;
                                read_words = (size_t)MODEL_PE_ROWS * w_last * lanes;
                            }

                            profile_start();
                            uint32_t bank = device_bank;
                            model_ocm_word_t *raw = cg_workspace->ocm[bank];
                            wait_reg(REG_DONE_READ0 + bank, 0);
                            wait_reg(REG_DONE_WRITE0 + bank, 1);
                            nx_cache_invalidate(raw, read_words * sizeof(model_ocm_word_t));
                            write_reg(REG_DONE_WRITE0 + bank, 0);
                            nx_sim_event("acquire", bank, read_words * sizeof(model_ocm_word_t));
                            profile_stop(PROFILE_WAIT);

                            if (CG_LOG_LEVEL >= 3) {
                                nx_puts("raw_packet: bundle=");
                                nx_put_hex32(b);
                                nx_puts(" packet=");
                                nx_put_hex32(packet);
                                nx_puts(" bank=");
                                nx_put_hex32(bank);
                                nx_puts(" address=");
                                nx_put_hex32((uintptr_t)raw);
                                nx_puts(" words=");
                                nx_put_hex32(words);
                                nx_puts(" values=");
                                for (size_t word = 0; word < words; ++word) {
                                    nx_puts(word == 0 ? "" : ",");
                                    nx_put_hex32(raw[word]);
                                }
                                nx_puts("\r\n");
                            }
                            profile_start();
#if CG_VALIDATE
                            for (size_t word = 0; word < words; ++word) {
                                uint32_t y_h = il * MODEL_PE_ROWS + word % MODEL_PE_ROWS;
                                uint32_t y_w = iw_kw2 + word / MODEL_PE_ROWS % w_last;
                                uint32_t y_c = bundle->coe * it + word / (MODEL_PE_ROWS * w_last);
                                if (y_h >= bundle->h || y_w >= bundle->w || y_c >= bundle->co) {
                                    continue;
                                }
                                if (raw[word] != model_expected_raw[expected_word]) {
                                    cg_report_mismatch("raw", packet, word, raw[word],
                                        model_expected_raw[expected_word]);
                                }
                                ++expected_word;
                            }
#endif
                            if (fuse) {
                                /* A fused bundle runs one pass: ip is 0. */
                                cg_fused_packet(raw, it, il, iw_kw2);
                                profile_stop(PROFILE_FUSED);
                                profile_start();
                            } else {
                                int32_t *accumulation = cg_accumulation_packet(
                                    bundle, ((size_t)it * bundle->l + il) * bundle->w_kw2 + iw_kw2);
                                if (ip == 0) {
                                    for (size_t word = 0; word < words; ++word) {
                                        accumulation[word] = raw[word];
                                    }
                                } else {
                                    for (size_t word = 0; word < words; ++word) {
                                        accumulation[word] += raw[word];
                                    }
                                }
                            }
                            write_reg(REG_DONE_READ0 + bank, 1);
                            device_bank = bank ^ 1;
                            nx_sim_event("release", bank, device_bank);
                            /* A fused bundle's release counts as control. */
                            profile_stop(fuse ? PROFILE_CONTROL : PROFILE_ACCUMULATE);
                        }
                    }
                }
                cg_log(2, "transfer_done", b, it);
            }
        }
#if CG_VALIDATE
        assert(expected_word == model_expected_raw_offsets[b + 1]);
#endif

        /* Image by image: the output buffer is shared scratch, so each image
         * is packed before the next one's postprocess. */
        for (uint32_t image = 0; image < bundle->n; ++image) {
            cg_batch_image = image;
            if (!fuse) {
                cg_finish_bundle(b);
            }
#if CG_VALIDATE
            if (fuse && last) {
                cg_check_equal("output", b, cg_workspace->result[image],
                    &model_expected_output[b][(size_t)image * MODEL_MAX_OUTPUT_WORDS],
                    bundle->output_words, sizeof(model_expected_output[0][0]));
            }
#endif
            if (!last) {
                profile_start();
                cg_pack(b, fuse);
                /* Concat joins apart from plain packs. */
                profile_stop(model_concat_plans[b + 1].skip_bundle != -1 ? PROFILE_PACK_CONCAT
                                                                         : PROFILE_PACK);
            }
        }
        if (!last) {
            profile_start();
            /* Only the next bundle's input: the CPU wrote nothing else. */
            const model_bundle_t *next = &model_bundles[b + 1];
            nx_dma_to_device(cg_intermediate_batch(b + 1),
                (next->x_bytes_first_pass + (size_t)(next->p - 1) * next->x_bytes) * next->n);
            profile_stop(PROFILE_CLEAN);
            /* The pixels FSM clears bundle_done when it starts the bundle. */
            profile_start();
            write_reg(REG_BUNDLE_DONE, 1);
            wait_reg(REG_BUNDLE_DONE, 0);
            nx_sim_event("input_ready", device_bank, 2);
            profile_stop(PROFILE_CONTROL);
        }
        cg_log(1, "bundle_end", b, iteration + 1);
    }

    profile_start();
    wait_reg(REG_WEIGHTS_DONE, 1);
    wait_reg(REG_PIXELS_DONE, 1);
    wait_reg(REG_OUTPUT_DONE, 1);
    assert(read_reg(REG_DONE_READ0) == 1);
    assert(read_reg(REG_DONE_READ1) == 1);
    nx_sim_event("finish", device_bank, 2);
    profile_stop(PROFILE_CONTROL);
    return nx_ticks() - start;
}

/* Counter ticks per millisecond (the board's 100 MHz counter; in simulation
 * a tick is an RTL cycle). */
#define TICKS_PER_MS 100000u

/* Repeats the inference back to back for at least `ms` (at least once);
 * returns the count and stores the elapsed ticks. */
static uint64_t active_window(uint64_t ms, uint64_t *ticks) {
    uint64_t start = nx_ticks();
    uint64_t count = 0;
    do {
        run_inference(0);
        ++count;
    } while (nx_ticks() - start < ms * TICKS_PER_MS);
    *ticks = nx_ticks() - start;
    return count;
}

#ifdef NX_SIM
#define CG_ENTRY cg_app_main
#else
#define CG_ENTRY main
#endif

/* Session loop: receive a payload, run every bundle, publish the result. */
int CG_ENTRY(void) {
    /* The flash bootloader enters main directly: initialize owned state. */
    session_requests = 0;
    device_bank = 0;
    nx_counter_reset();
    nx_counter_start();
    /* Before any vector instruction (the fused pass). */
    nx_simd_enable();
    cg_log(1, "main", MODEL_BUNDLE_COUNT, CG_LOG_LEVEL);

    for (uint32_t iteration = 0;; ++iteration) {
        cg_host_receive_payload(iteration + 1);

        /* Windows for the host's power sampler, timed by the `power:` lines:
         * a warm-up, then alternating idle windows (accelerator idle, CPU
         * polling the counter) and active windows (the inference back to
         * back). */
        uint64_t ticks;
        if (CG_POWER_MS > 0 && iteration == 0 && CG_POWER_WARMUP_MS > 0) {
            nx_puts("power: warmup_begin\r\n");
            active_window(CG_POWER_WARMUP_MS, &ticks);
            nx_puts("power: warmup_end\r\n");
        }
        for (uint32_t cycle = 1; CG_POWER_MS > 0 && iteration == 0 && cycle <= CG_POWER_CYCLES;
             ++cycle) {
            nx_puts("power: idle_begin ");
            nx_put_u64(cycle);
            nx_puts("\r\n");
#ifndef NX_SIM
            /* The simulated counter advances only with accelerator accesses. */
            uint64_t idle_ticks = (uint64_t)CG_POWER_IDLE_MS * TICKS_PER_MS;
            ticks = nx_ticks();
            while (nx_ticks() - ticks < idle_ticks) {
            }
#endif
            nx_puts("power: idle_end ");
            nx_put_u64(cycle);
            nx_puts("\r\npower: active_begin ");
            nx_put_u64(cycle);
            nx_puts("\r\n");
            uint64_t count = active_window(CG_POWER_MS, &ticks);
            nx_puts("power: active_end ");
            nx_put_u64(cycle);
            nx_puts(" inferences=");
            nx_put_u64(count);
            nx_puts(" ticks=");
            nx_put_u64(ticks);
            nx_puts("\r\n");
        }

        uint64_t elapsed = run_inference(iteration);
        nx_puts("timing_boundary: accepted_payload_to_computed_output; "
                "includes_runtime_preparation; excludes_loading_and_result_printing\r\n");
        int is_softmax = model_bundles[MODEL_BUNDLE_COUNT - 1].is_softmax;
        if (!CG_PRINT_RESULT) {
            /* The host compares the result in DDR. */
            nx_puts("result: not_printed\r\n");
        } else {
            /* Every image of the batch, one after the other (flat views). */
            const int32_t *result = (const int32_t *)(const void *)cg_workspace->result;
            const float *softmax = (const float *)(const void *)cg_workspace->softmax;
            nx_puts(is_softmax ? "result: logits=[" : "result: output=[");
            for (size_t word = 0; word < MODEL_BATCH * MODEL_FINAL_WORDS; ++word) {
                nx_puts(word == 0 ? "" : ", ");
                nx_put_i32(result[word]);
            }
            nx_puts("]\r\n");
            /* Softmax models size the array to MODEL_FINAL_WORDS; others to 1. */
            if (is_softmax) {
                nx_puts("result: softmax=[");
                for (size_t word = 0; word < MODEL_BATCH * MODEL_SOFTMAX_WORDS; ++word) {
                    nx_puts(word == 0 ? "" : ", ");
                    nx_put_float6(softmax[word]);
                }
                nx_puts("]\r\n");
            }
        }
#ifdef NX_SIM
        nx_puts("sim_timing: inference_rtl_cycles=");
        nx_put_u64(elapsed);
        nx_puts("\r\nsim_timing: host_cpu_cycles=not_modelled\r\n");
#else
        nx_puts("timing: inference_ticks=");
        nx_put_u64(elapsed);
        nx_puts("\r\ntiming: inference_ms=");
        nx_put_ms(elapsed);
        nx_puts(" ms\r\n");
#endif
        uint64_t total = 0;
        for (int stage = 0; CG_PROFILE && stage <= PROFILE_COUNT; ++stage) {
            uint64_t stage_ticks = stage < PROFILE_COUNT ? profile_ticks[stage] : total;
            total += stage < PROFILE_COUNT ? stage_ticks : 0;
            nx_puts("profile: stage=");
            nx_puts(stage < PROFILE_COUNT ? profile_names[stage] : "sum");
            nx_puts(" ticks=");
            nx_put_u64(stage_ticks);
#ifndef NX_SIM
            nx_puts(" ms=");
            nx_put_ms(stage_ticks);
#endif
            nx_puts("\r\n");
        }
        nx_puts("build: mode=");
        nx_puts(CG_DEBUG ? "debug" : CG_PROFILE ? "profile" : "release");
        nx_puts(" log=");
        nx_putc('0' + CG_LOG_LEVEL);
        nx_puts(" validate=");
        nx_putc('0' + CG_VALIDATE);
        nx_puts("\r\n");

        cg_host_publish_result(iteration + 1);
        if (cg_host_session_complete(iteration + 1)) {
            break;
        }
    }

    nx_counter_stop();
    if (CG_VALIDATE) {
        nx_puts(MODEL_PASS_MARKER);
    } else {
        nx_puts("TARGET PREFIX ");
        nx_put_u64(MODEL_BUNDLE_COUNT);
        nx_puts(" COMPLETE\r\n");
    }
    nx_halt();
    return 0;
}
