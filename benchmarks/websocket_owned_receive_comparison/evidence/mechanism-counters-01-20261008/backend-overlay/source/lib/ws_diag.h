/* SPDX-License-Identifier: curl */
#ifndef HEADER_CURL_WS_DIAG_H
#define HEADER_CURL_WS_DIAG_H
#include "curl_setup.h"
#include <stdatomic.h>
#include <stdint.h>

/* Private process-wide counters for the owned-receive mechanism census.
 * Queue/chunk/pin counters are enabled only on the WebSocket recvbuf tagged
 * by Curl_bufq_enable_pins(); they do not include other curl bufqs. The TLS
 * counters instrument ossl_recv SSL_read calls and therefore cover
 * transport reads in the instrumented client process, not only WebSocket
 * payload reads. Values are cumulative until the DSO unloads; snapshot reads
 * each atomic value independently and is not a transactional freeze. */

/* This order is consumed by the diagnostic event writer; append only. */
enum curl_ws_diag_index {
  CURL_WS_DIAG_RECV_API_CALLS,
  CURL_WS_DIAG_OWNED_API_CALLS,
  CURL_WS_DIAG_RECV_API_BYTES,
  CURL_WS_DIAG_OWNED_API_BYTES,
  CURL_WS_DIAG_RECV_COLLECT_COPY_CALLS,
  CURL_WS_DIAG_RECV_COLLECT_COPY_BYTES,
  CURL_WS_DIAG_READER_CALLS,
  CURL_WS_DIAG_READER_REQUEST_BYTES,
  CURL_WS_DIAG_READER_SUCCESS_CALLS,
  CURL_WS_DIAG_READER_RETURN_BYTES,
  CURL_WS_DIAG_READER_AGAIN_CALLS,
  CURL_WS_DIAG_READER_ERROR_CALLS,
  CURL_WS_DIAG_TLS_READ_CALLS,
  CURL_WS_DIAG_TLS_REQUEST_BYTES,
  CURL_WS_DIAG_TLS_POSITIVE_CALLS,
  CURL_WS_DIAG_TLS_RETURN_BYTES,
  CURL_WS_DIAG_TLS_WANT_READ,
  CURL_WS_DIAG_TLS_WANT_WRITE,
  /* Combines SSL_ERROR_NONE and SSL_ERROR_ZERO_RETURN outcomes. */
  CURL_WS_DIAG_TLS_ZERO_RESULT,
  CURL_WS_DIAG_TLS_SYSCALL_AGAIN,
  CURL_WS_DIAG_TLS_OTHER_FAILURE,
  CURL_WS_DIAG_RESERVATION_CALLS,
  CURL_WS_DIAG_RESERVATION_BYTES,
  CURL_WS_DIAG_PIN_ALLOC_ATTEMPTS,
  CURL_WS_DIAG_PIN_ALLOC_REQUEST_BYTES,
  CURL_WS_DIAG_PIN_ALLOC_SUCCESSES,
  CURL_WS_DIAG_PIN_ALLOC_FAILURES,
  CURL_WS_DIAG_PIN_ACQUIRE_FAILURES,
  CURL_WS_DIAG_PIN_RELEASES,
  CURL_WS_DIAG_PINS_LIVE,
  CURL_WS_DIAG_PAYLOAD_ALLOC_ATTEMPTS,
  CURL_WS_DIAG_PAYLOAD_ALLOC_REQUEST_BYTES,
  CURL_WS_DIAG_PAYLOAD_ALLOC_SUCCESSES,
  CURL_WS_DIAG_PAYLOAD_ALLOC_FAILURES,
  CURL_WS_DIAG_PAYLOAD_FREES,
  CURL_WS_DIAG_PAYLOADS_LIVE,
  CURL_WS_DIAG_CHUNK_ALLOC_ATTEMPTS,
  CURL_WS_DIAG_CHUNK_ALLOC_REQUEST_BYTES,
  CURL_WS_DIAG_CHUNK_ALLOC_SUCCESSES,
  CURL_WS_DIAG_CHUNK_ALLOC_SUCCESS_BYTES,
  CURL_WS_DIAG_CHUNKS_LIVE,
  CURL_WS_DIAG_CHUNKS_LIVE_REQUEST_BYTES,
  CURL_WS_DIAG_TAIL_REUSE,
  CURL_WS_DIAG_SPARE_REUSE,
  CURL_WS_DIAG_SPARE_RETURN,
  CURL_WS_DIAG_SHARED_DETACH_PRUNE,
  CURL_WS_DIAG_SHARED_DETACH_RESET,
  CURL_WS_DIAG_UNSHARED_DROP,
  /* Queue references released at teardown; pins may keep chunks alive. */
  CURL_WS_DIAG_QUEUE_FREE_CHUNK_REFS,
  CURL_WS_DIAG_CHUNK_PHYSICAL_FREES,
  CURL_WS_DIAG_CHUNK_PHYSICAL_FREE_REQUEST_BYTES,
  CURL_WS_DIAG_QUEUE_SKIP_CALLS,
  CURL_WS_DIAG_QUEUE_SKIP_REQUEST_BYTES,
  CURL_WS_DIAG_QUEUE_SLURP_CALLS,
  CURL_WS_DIAG_QUEUE_SLURP_RETURN_BYTES,
  CURL_WS_DIAG_COUNTER_COUNT
};

/* Snapshot layout slots follow the fixed counter slots. */
enum {
  CURL_WS_DIAG_LAYOUT_CHUNK_SIZE = CURL_WS_DIAG_COUNTER_COUNT,
  CURL_WS_DIAG_LAYOUT_BUFQ_SIZE,
  CURL_WS_DIAG_LAYOUT_PIN_SIZE,
  CURL_WS_DIAG_LAYOUT_PAYLOAD_SIZE,
  CURL_WS_DIAG_LAYOUT_RECV_CHUNK_SIZE,
  CURL_WS_DIAG_SNAPSHOT_COUNT
};

extern atomic_uint_fast64_t Curl_ws_diag_counters[CURL_WS_DIAG_COUNTER_COUNT];

static inline void Curl_ws_diag_add(enum curl_ws_diag_index index,
                                    uint64_t amount)
{
  atomic_fetch_add_explicit(&Curl_ws_diag_counters[index], amount,
                            memory_order_relaxed);
}

static inline void Curl_ws_diag_sub(enum curl_ws_diag_index index,
                                    uint64_t amount)
{
  atomic_fetch_sub_explicit(&Curl_ws_diag_counters[index], amount,
                            memory_order_relaxed);
}

size_t Curl_bufq_pin_struct_size(void);
CURL_EXTERN size_t curl_ws_diag_snapshot_count(void);
CURL_EXTERN const char *curl_ws_diag_counter_name(size_t index);
CURL_EXTERN void curl_ws_diag_snapshot(uint64_t *values, size_t capacity);

#endif
