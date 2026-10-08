#ifndef SCRAPANIUM_H
#define SCRAPANIUM_H
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct sp_session sp_session;
typedef struct sp_response sp_response;
typedef struct sp_cancel sp_cancel;
typedef struct sp_ws sp_ws;

/* Owned receive chunks are immutable views whose release callback owns the
 * backing allocation. They can outlive the socket and may be released from a
 * different thread; callers must not copy or mutate the view in place. */
typedef struct sp_ws_segment {
  const unsigned char *data;
  size_t size;
  void *owner;
  size_t charge;
  void (*release)(void *, size_t);
  struct sp_ws_segment *next;
} sp_ws_segment;
void sp_ws_segments_free(sp_ws_segment *);

enum sp_error {
  SP_OK = 0, SP_INVALID = 1, SP_NOMEM = 2, SP_PROFILE = 3,
  SP_TRANSPORT = 4, SP_BODY_LIMIT = 5, SP_HEADER_LIMIT = 6,
  SP_BUSY = 7, SP_BACKEND = 8, SP_BATCH_LIMIT = 9, SP_CANCELLED = 10,
  SP_SINK = 11, SP_HTTP_STATUS = 12, SP_INPUT_LIMIT = 13,
  SP_TIMEOUT = 14, SP_PROTOCOL = 15, SP_CLOSED = 16
};

/* Overrides are applied after the named profile. NULL/empty strings and -1
 * toggles inherit it. Custom settings no longer imply stock-browser fidelity. */
typedef struct {
  const char *ciphers, *curves, *signature_algorithms, *extension_order;
  const char *http2_settings, *pseudo_header_order, *cert_compression;
  int grease, permute_extensions;
  uint32_t http2_window_update;
} sp_fingerprint;

/* Strings are UTF-8; NULL optional strings mean unset. Configuration is copied.
 * A session is an exclusive owner: no calls or destruction may race another call.
 * Responses remain valid independently of their session until explicitly freed. */
typedef struct {
  const char *profile, *proxy, *ca_bundle;
  uint32_t timeout_ms, connect_timeout_ms, max_redirects, concurrency;
  size_t max_body_bytes, max_header_bytes;
  int verify, follow_redirects, default_headers;
  sp_fingerprint fingerprint;
  /* Shared budget for response structures, buffer capacities and final URLs.
   * Does not include caller-owned input or libcurl's internal allocations. */
  size_t max_batch_bytes, max_batch_requests;
} sp_config;

typedef struct {
  const char *method, *url;
  const char *const *headers;
  size_t header_count;
  const unsigned char *body;
  size_t body_size;
} sp_request;

void sp_config_default(sp_config *config);
sp_session *sp_session_new(const sp_config *config, int *error);
void sp_session_free(sp_session *session);
/* Batch output preserves input order. Every non-NULL response must be freed,
 * including failures. Returns an infrastructure error; per-request errors are
 * on the response. Requests and their bytes are borrowed until this returns.
 * Invalid arguments or count/metadata-limit preflight rejection leave output
 * untouched. Initialize output entries to NULL before calling. */
int sp_session_batch(sp_session *, const sp_request *, size_t, sp_response **);
sp_response *sp_session_request(sp_session *, const sp_request *);
/* A token is one-shot, may be shared across operations, and may be triggered
 * from any thread. Keep an owned reference alive whenever passing its pointer.
 * Operations retain their own reference for the duration of the call. */
sp_cancel *sp_cancel_new(void);
void sp_cancel_retain(sp_cancel *);
void sp_cancel_free(sp_cancel *);
void sp_cancel_trigger(sp_cancel *);
int sp_cancel_is_triggered(const sp_cancel *);
int sp_session_batch_cancel(sp_session *, const sp_request *, size_t, sp_response **, sp_cancel *);
sp_response *sp_session_request_cancel(sp_session *, const sp_request *, sp_cancel *);
/* The sink runs on the calling thread. Return 0 after accepting all bytes;
 * a nonzero return aborts with SP_SINK. It must not throw across C or destroy
 * the session. A slow sink provides backpressure. Total decoded bytes are
 * limited by max_body_bytes; buffered response size is zero for a stream. */
typedef int (*sp_sink_fn)(const unsigned char *, size_t, void *);
sp_response *sp_session_stream(sp_session *, const sp_request *, sp_sink_fn, void *, sp_cancel *);
/* Streams into a private temporary file beside path and atomically replaces
 * path only after transport success, successful close, and an HTTP 2xx status.
 * Failure/cancellation preserves an existing path and removes the temp file. */
sp_response *sp_session_download(sp_session *, const sp_request *, const char *path, sp_cancel *);
void sp_response_free(sp_response *);
int sp_response_error(const sp_response *);
int sp_response_curl_code(const sp_response *);
const char *sp_response_message(const sp_response *);
long sp_response_status(const sp_response *);
long sp_response_http_version(const sp_response *);
const unsigned char *sp_response_body(const sp_response *);
/* Moves the body allocation to the caller, which must free it. Empty bodies
 * may return NULL. The response retains metadata but its body becomes empty. */
unsigned char *sp_response_take_body(sp_response *, size_t *size);
size_t sp_response_size(const sp_response *);
const char *sp_response_headers(const sp_response *);
size_t sp_response_headers_size(const sp_response *);
const char *sp_response_url(const sp_response *);
uint64_t sp_response_elapsed_us(const sp_response *);
long sp_response_new_connections(const sp_response *);
uint64_t sp_response_downloaded(const sp_response *);
size_t sp_response_memory_bytes(const sp_response *);
const char *sp_backend_version(void);
size_t sp_profile_count(void);
const char *sp_profile_name(size_t index);
const char *sp_error_message(int);

/* WebSockets own a dedicated session. Upgrade consumes the session on every
 * path, including failure. GET, ws:// or wss://, no body; redirects are disabled.
 * Only one call may use a socket at a time. Free must never race a call.
 * Incoming fragments are reassembled; ping/pong is handled during receive.
 * Receive returns text=1, binary=2, close=8. Caller frees *data (also for empty
 * messages). After any I/O failure the socket must be freed, not retried. */
sp_ws *sp_ws_upgrade(sp_session *, const sp_request *, sp_cancel *, int *error);
int sp_ws_send(sp_ws *, unsigned kind, const unsigned char *, size_t, uint32_t timeout_ms, sp_cancel *);
int sp_ws_receive(sp_ws *, unsigned *kind, unsigned char **data, size_t *size, uint32_t timeout_ms, sp_cancel *);
int sp_ws_close(sp_ws *, unsigned code, const unsigned char *reason, size_t, uint32_t timeout_ms, sp_cancel *);
void sp_ws_free(sp_ws *);

/* Isolated benchmark-only diagnostic counters. */
enum sp_wss_diag_index {
  SP_WSS_DIAG_FRAMED_RECV_CALLS, SP_WSS_DIAG_FRAMED_RECV_BYTES,
  SP_WSS_DIAG_OWNED_RECV_CALLS, SP_WSS_DIAG_OWNED_RECV_BYTES,
  SP_WSS_DIAG_DIRECT_RECV_CALLS, SP_WSS_DIAG_DIRECT_RECV_REQUEST_BYTES,
  SP_WSS_DIAG_DIRECT_RECV_SUCCESS, SP_WSS_DIAG_DIRECT_RECV_RETURN_BYTES,
  SP_WSS_DIAG_SCRATCH_RECV_CALLS, SP_WSS_DIAG_SCRATCH_RECV_REQUEST_BYTES,
  SP_WSS_DIAG_SCRATCH_RECV_SUCCESS, SP_WSS_DIAG_SCRATCH_RECV_RETURN_BYTES,
  SP_WSS_DIAG_SCRATCH_APPEND_COPY_CALLS, SP_WSS_DIAG_SCRATCH_APPEND_COPY_BYTES,
  SP_WSS_DIAG_MESSAGE_REALLOC_CALLS, SP_WSS_DIAG_MESSAGE_REALLOC_OLD_CAPACITY_BYTES,
  SP_WSS_DIAG_MESSAGE_REALLOC_REQUEST_BYTES, SP_WSS_DIAG_MESSAGE_REALLOC_SUCCESSES,
  SP_WSS_DIAG_MESSAGE_REALLOC_FAILURES, SP_WSS_DIAG_MESSAGE_REALLOC_ADDRESS_CHANGED,
  SP_WSS_DIAG_SEGMENT_ALLOC_ATTEMPTS, SP_WSS_DIAG_SEGMENT_ALLOC_SUCCESSES,
  SP_WSS_DIAG_SEGMENT_ALLOC_FAILURES, SP_WSS_DIAG_SEGMENT_ALLOC_REQUEST_BYTES,
  SP_WSS_DIAG_SEGMENT_FREES, SP_WSS_DIAG_SEGMENT_FREE_REQUEST_BYTES,
  SP_WSS_DIAG_SEGMENTS_LIVE, SP_WSS_DIAG_SEGMENTS_LIVE_REQUEST_BYTES,
  SP_WSS_DIAG_SEGMENT_LIVE_PAYLOAD_BYTES, SP_WSS_DIAG_WS_FLATTEN_CALLS,
  SP_WSS_DIAG_WS_FLATTEN_ALLOC_ATTEMPTS, SP_WSS_DIAG_WS_FLATTEN_ALLOC_SUCCESSES,
  SP_WSS_DIAG_WS_FLATTEN_ALLOC_FAILURES, SP_WSS_DIAG_WS_FLATTEN_ALLOC_BYTES,
  SP_WSS_DIAG_WS_FLATTEN_COPY_BYTES,
  SP_WSS_DIAG_BEND_BYTES_FLATTEN_CALLS, SP_WSS_DIAG_BEND_BYTES_FLATTEN_ALLOC_ATTEMPTS,
  SP_WSS_DIAG_BEND_BYTES_FLATTEN_ALLOC_SUCCESSES,
  SP_WSS_DIAG_BEND_BYTES_FLATTEN_ALLOC_FAILURES,
  SP_WSS_DIAG_BEND_BYTES_FLATTEN_ALLOC_BYTES, SP_WSS_DIAG_BEND_BYTES_FLATTEN_COPY_BYTES,
  SP_WSS_DIAG_BYTES_EQUAL_CALLS, SP_WSS_DIAG_BYTES_EQUAL_BYTES,
  SP_WSS_DIAG_BYTES_EQUAL_MEMCMP_SPANS,
  SP_WSS_DIAG_WS_SEND_CALLS, SP_WSS_DIAG_WS_SEND_REQUEST_BYTES,
  SP_WSS_DIAG_WS_SEND_SUCCESS_CALLS, SP_WSS_DIAG_WS_SEND_RETURN_BYTES,
  SP_WSS_DIAG_WS_SEND_AGAIN_CALLS, SP_WSS_DIAG_WS_SEND_ERROR_CALLS,
  SP_WSS_DIAG_WS_STEP_CALLS, SP_WSS_DIAG_WS_READY_QUERY_CALLS,
  SP_WSS_DIAG_FRAMED_RECV_SUCCESS_CALLS, SP_WSS_DIAG_FRAMED_RECV_AGAIN_CALLS,
  SP_WSS_DIAG_FRAMED_RECV_ERROR_CALLS, SP_WSS_DIAG_OWNED_RECV_SUCCESS_CALLS,
  SP_WSS_DIAG_OWNED_RECV_AGAIN_CALLS, SP_WSS_DIAG_OWNED_RECV_ERROR_CALLS,
  SP_WSS_DIAG_COUNTER_COUNT
};
enum {
  SP_WSS_DIAG_LAYOUT_SEGMENT_SIZE = SP_WSS_DIAG_COUNTER_COUNT,
  SP_WSS_DIAG_LAYOUT_BUFFER_SIZE, SP_WSS_DIAG_LAYOUT_WS_IO_SIZE,
  SP_WSS_DIAG_SNAPSHOT_COUNT
};
void sp_wss_diag_add(unsigned index, uint64_t amount);
void sp_wss_diag_sub(unsigned index, uint64_t amount);
void sp_wss_diag_snapshot(uint64_t *values, size_t capacity);
size_t sp_wss_diag_snapshot_count(void);
const char *sp_wss_diag_counter_name(unsigned index);

#ifdef __cplusplus
}
#endif
#endif
