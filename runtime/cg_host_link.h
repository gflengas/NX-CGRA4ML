/* DDR mailbox protocol with the host loader: payload in, result out. Shared
 * by the runtime (main.c) and the simulator's host (sim/sim.cpp).
 *
 * Status and request are separate 128-byte cache-line areas; the host
 * publishes request.command last.
 */
#ifndef CG_HOST_LINK_H
#define CG_HOST_LINK_H

#include <stddef.h>
#include <stdint.h>

enum {
    CG_LOAD_READY = 1,
    CG_LOAD_COMPLETE = 2,
    CG_LOAD_RUNNING = 3,
    CG_LOAD_RESULT = 4,
    CG_RESULT_CONSUMED = 5,
    CG_LOAD_BEGIN = 6,
    CG_LOAD_RECEIVING = 7,
    CG_SESSION_COMPLETE = 8
};

typedef struct {
    uint32_t state, sequence, address, bytes;
    uint32_t identity[8];
    uint32_t result_address, result_words, result_is_float, parameter_bytes;
    uint32_t session_requests;
    uint32_t reserved[15];
} cg_load_status_t;

typedef struct {
    uint32_t command, sequence, address, bytes, crc32;
    uint32_t identity[8];
    uint32_t session_requests;
    uint32_t reserved[18];
} cg_load_request_t;

typedef struct {
    cg_load_status_t status;
    cg_load_request_t request;
} cg_mailbox_t;

typedef char cg_status_cache_lines[sizeof(cg_load_status_t) == 128 ? 1 : -1];
typedef char cg_request_cache_lines[sizeof(cg_load_request_t) == 128 ? 1 : -1];

/* CRC-32 (IEEE, reflected, as zlib) of a payload. */
static uint32_t cg_crc32(const uint8_t *data, size_t bytes) {
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < bytes; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
        }
    }
    return ~crc;
}

#endif
