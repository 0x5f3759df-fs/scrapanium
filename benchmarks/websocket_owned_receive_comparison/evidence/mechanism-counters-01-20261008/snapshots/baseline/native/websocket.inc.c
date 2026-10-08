/* Included in scrapanium.c to share the profile and cancellation machinery. */
#include <poll.h>
#include <time.h>
#include <stdatomic.h>
#include "ws_async.h"
#include "ws_handshake.inc.c"

static atomic_uint_fast64_t sp_wss_diag_values[SP_WSS_DIAG_COUNTER_COUNT];

void sp_wss_diag_add(unsigned index, uint64_t amount) {
  if(index < SP_WSS_DIAG_COUNTER_COUNT)
    atomic_fetch_add_explicit(&sp_wss_diag_values[index], amount, memory_order_relaxed);
}
void sp_wss_diag_sub(unsigned index, uint64_t amount) {
  if(index < SP_WSS_DIAG_COUNTER_COUNT)
    atomic_fetch_sub_explicit(&sp_wss_diag_values[index], amount, memory_order_relaxed);
}
/* One operation owns the socket gate. Its state stays at a stable address in
 * that socket while parked, and is reset only after acquiring the gate. */
struct sp_ws_io {
  sp_ws *socket;
  sp_cancel *cancel;
  uint64_t deadline;
  unsigned operation, kind, message_kind, send_flags;
  const unsigned char *out;
  size_t out_size, offset, control_size;
  unsigned char control[125];
  sp_buffer message;
  short events;
  int sending, after_send, done, error;
};

struct sp_ws {
  sp_session *session;
  sp_response *handshake;
  pthread_mutex_t gate;
  curl_socket_t fd;
  int broken, closing, closed;
  sp_ws_io operation;
};

void sp_wss_diag_snapshot(uint64_t *values, size_t capacity) {
  if(!values || capacity < SP_WSS_DIAG_SNAPSHOT_COUNT)
    return;
  for(size_t i = 0; i < SP_WSS_DIAG_COUNTER_COUNT; i++)
    values[i] = atomic_load_explicit(&sp_wss_diag_values[i], memory_order_relaxed);
  values[SP_WSS_DIAG_LAYOUT_SEGMENT_SIZE] = 0;
  values[SP_WSS_DIAG_LAYOUT_BUFFER_SIZE] = sizeof(sp_buffer);
  values[SP_WSS_DIAG_LAYOUT_WS_IO_SIZE] = sizeof(sp_ws_io);
}

size_t sp_wss_diag_snapshot_count(void) {
  return SP_WSS_DIAG_SNAPSHOT_COUNT;
}

const char *sp_wss_diag_counter_name(unsigned index) {
  static const char *const names[SP_WSS_DIAG_COUNTER_COUNT] = {
    "framed_recv_calls",
    "framed_recv_bytes",
    "owned_recv_calls",
    "owned_recv_bytes",
    "direct_recv_calls",
    "direct_recv_request_bytes",
    "direct_recv_success",
    "direct_recv_return_bytes",
    "scratch_recv_calls",
    "scratch_recv_request_bytes",
    "scratch_recv_success",
    "scratch_recv_return_bytes",
    "message_append_copy_calls",
    "message_append_copy_bytes",
    "message_realloc_calls",
    "message_realloc_old_capacity_bytes",
    "message_realloc_request_bytes",
    "message_realloc_successes",
    "message_realloc_failures",
    "message_realloc_address_changed",
    "segment_alloc_attempts",
    "segment_alloc_successes",
    "segment_alloc_failures",
    "segment_alloc_request_bytes",
    "segment_frees",
    "segment_free_request_bytes",
    "segments_live",
    "segments_live_request_bytes",
    "segment_live_payload_bytes",
    "ws_flatten_calls",
    "ws_flatten_alloc_attempts",
    "ws_flatten_alloc_successes",
    "ws_flatten_alloc_failures",
    "ws_flatten_alloc_bytes",
    "ws_flatten_copy_bytes",
    "bend_bytes_flatten_calls",
    "bend_bytes_flatten_alloc_attempts",
    "bend_bytes_flatten_alloc_successes",
    "bend_bytes_flatten_alloc_failures",
    "bend_bytes_flatten_alloc_bytes",
    "bend_bytes_flatten_copy_bytes",
    "bytes_equal_calls",
    "bytes_equal_bytes",
    "bytes_equal_memcmp_spans",
    "ws_send_calls",
    "ws_send_request_bytes",
    "ws_send_success_calls",
    "ws_send_return_bytes",
    "ws_send_again_calls",
    "ws_send_error_calls",
    "ws_step_calls",
    "ws_ready_query_calls",
    "framed_recv_success_calls",
    "framed_recv_again_calls",
    "framed_recv_error_calls",
    "owned_recv_success_calls",
    "owned_recv_again_calls",
    "owned_recv_error_calls"
  };
  return index < SP_WSS_DIAG_COUNTER_COUNT ? names[index] : NULL;
}

static uint64_t sp_ws_clock(void) {
  struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
  return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000;
}
static int sp_ws_check(uint64_t deadline, sp_cancel *cancel) {
  if (sp_cancel_is_triggered(cancel)) return SP_CANCELLED;
  return sp_ws_clock() >= deadline ? SP_TIMEOUT : SP_OK;
}
static int sp_ws_utf8(const unsigned char *s, size_t n) {
  for (size_t i = 0; i < n;) {
    unsigned c = s[i++], value, count, minimum;
    if (c < 128) continue;
    if (c >= 0xc2 && c <= 0xdf) { value = c & 31; count = 1; minimum = 0x80; }
    else if (c >= 0xe0 && c <= 0xef) { value = c & 15; count = 2; minimum = 0x800; }
    else if (c >= 0xf0 && c <= 0xf4) { value = c & 7; count = 3; minimum = 0x10000; }
    else return 0;
    if (count > n - i) return 0;
    while (count--) { c = s[i++]; if ((c & 0xc0) != 0x80) return 0; value = (value << 6) | (c & 63); }
    if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return 0;
  }
  return 1;
}
static int sp_ws_code(unsigned code) {
  return (code >= 1000 && code <= 1014 && code != 1004 && code != 1005 && code != 1006) ||
    (code >= 3000 && code <= 4999);
}
static int sp_ws_close_valid(const unsigned char *p, size_t n) {
  return !n || (n >= 2 && sp_ws_code((unsigned)p[0] * 256 + p[1]) && sp_ws_utf8(p + 2, n - 2));
}

void sp_ws_free(sp_ws *w) {
  if (!w) return;
  sp_session_free(w->session); sp_response_free(w->handshake);
  pthread_mutex_destroy(&w->gate); free(w);
}

sp_ws *sp_ws_upgrade(sp_session *s, const sp_request *q, sp_cancel *cancel, int *error) {
  int err = SP_INVALID;
  sp_ws *w = NULL;
  if (!s || !q || !q->method || strcmp(q->method, "GET") || q->body_size || q->body) goto fail;
  /* libcurl owns framing and negotiation. Extensions (including compression)
   * are not implemented; callers may request a subprotocol and set Origin. */
  for (size_t i = 0; i < q->header_count; i++) {
    if (!q->headers || !q->headers[i]) goto fail;
    const char *h = q->headers[i];
    if (!strncasecmp(h, "Sec-WebSocket-", 14) && strncasecmp(h, "Sec-WebSocket-Protocol:", 23)) goto fail;
    if (!strncasecmp(h, "Connection:", 11) || !strncasecmp(h, "Upgrade:", 8)) goto fail;
  }
  w = calloc(1, sizeof *w);
  if (!w) { err = SP_NOMEM; goto fail; }
  w->session = s;
  if (pthread_mutex_init(&w->gate, NULL)) { free(w); w = NULL; err = SP_NOMEM; goto fail; }
  w->handshake = calloc(1, sizeof *w->handshake);
  if (!w->handshake) { err = SP_NOMEM; goto fail; }
  sp_response *r = w->handshake; r->cancel = cancel;
  char key[25], expected[29], key_header[64];
  err = sp_ws_nonce(key); if (err) goto fail;
  sp_ws_accept(key, expected);
  snprintf(key_header, sizeof key_header, "Sec-WebSocket-Key: %s", key);
  sp_cancel_link link; sp_cancel_attach(cancel, &link, s->multi);
  err = sp_cancel_is_triggered(cancel) ? SP_CANCELLED : sp_prepare(s, s->slots, q, r, 1);
  if (!err) {
    struct curl_slist *headers = curl_slist_append(s->slots[0].headers, key_header);
    if (!headers) err = SP_NOMEM;
    else { s->slots[0].headers = headers; if (curl_easy_setopt(s->slots[0].easy, CURLOPT_HTTPHEADER, headers)) err = SP_BACKEND; }
  }
  int complete = 0;
  uint64_t deadline = sp_ws_clock() + s->config.timeout_ms;
  while (!err && !complete) {
    err = sp_ws_check(deadline, cancel); if (err) break;
    int running;
    if (curl_multi_perform(s->multi, &running) != CURLM_OK) { err = SP_BACKEND; break; }
    int queued; CURLMsg *message;
    while ((message = curl_multi_info_read(s->multi, &queued))) if (message->msg == CURLMSG_DONE) {
      CURLcode code = message->data.result;
      err = r->headers.error ? r->headers.error : r->body.error ? r->body.error :
        code == CURLE_OPERATION_TIMEDOUT ? SP_TIMEOUT : code ? SP_TRANSPORT : SP_OK;
      complete = 1;
    }
    if (!err && !complete && curl_multi_poll(s->multi, NULL, 0, 100, NULL) != CURLM_OK) err = SP_BACKEND;
  }
  sp_cancel_detach(cancel, &link); r->cancel = NULL;
  curl_easy_setopt(s->slots[0].easy, CURLOPT_NOPROGRESS, 1L);
  curl_easy_setopt(s->slots[0].easy, CURLOPT_XFERINFODATA, NULL);
  long status = 0; curl_easy_getinfo(s->slots[0].easy, CURLINFO_RESPONSE_CODE, &status);
  if (!err && status != 101) err = SP_PROTOCOL;
  if (!err) err = sp_ws_verify_handshake(q, &r->headers, expected);
  if (!err && (curl_easy_getinfo(s->slots[0].easy, CURLINFO_ACTIVESOCKET, &w->fd) || w->fd == CURL_SOCKET_BAD)) err = SP_CLOSED;
  if (err) goto fail;
  /* CONNECT_ONLY handles stay attached to their multi for their entire life. */
  if (error) *error = SP_OK;
  return w;
fail:
  if (w) sp_ws_free(w); else sp_session_free(s);
  if (error) *error = err;
  return NULL;
}

/* The blocking C API and Bend's event loop drive this same state machine.
 * A step never waits for a descriptor: libcurl owns framing, masking and TLS,
 * and CURLE_AGAIN hands readiness back to the caller without losing offsets,
 * fragments or an in-flight control reply. */
static unsigned sp_ws_flags(unsigned kind) {
  return kind == 1 ? CURLWS_TEXT : kind == 2 ? CURLWS_BINARY : kind == 8 ? CURLWS_CLOSE :
    kind == 9 ? CURLWS_PING : CURLWS_PONG;
}
static int sp_ws_io_end(sp_ws_io *io, int error) {
  io->done = 1; io->error = error;
  if (error) io->socket->broken = 1;
  if (io->operation == SP_WS_CLOSE) io->socket->closing = 1;
  return error;
}
static void sp_ws_io_send(sp_ws_io *io, unsigned kind, const unsigned char *data,
                          size_t size, int after_send) {
  io->out = data; io->out_size = size; io->offset = 0;
  io->send_flags = sp_ws_flags(kind); io->sending = 1; io->after_send = after_send;
}

/* WS bodies are length-delimited bytes, not C strings. The HTTP writer's NUL
 * sentinel doubles a power-of-two frame's allocation and can grow repeatedly
 * while a frame arrives. Reserve the validated announced remainder once, bounded
 * to 256 KiB so a peer cannot force a large allocation by advertising a frame
 * and stalling. Larger or open-ended fragmented messages grow geometrically
 * as their bytes arrive. Control payloads use this same length-only ownership. */
static int sp_ws_append(sp_buffer *message, const unsigned char *data, size_t size,
                         size_t frame_left, int final, int stored) {
  if (size > message->limit - message->size || frame_left > message->limit - message->size - size)
    return message->limit_error;
  size_t need = message->size + size, announced = need + frame_left, target = need;
  if (!message->cap || final) {
    size_t hint = announced < 262144 ? announced : 262144;
    if (hint > target) target = hint;
  }
  if (target > message->cap) {
    size_t cap;
    if (final && announced <= 262144) cap = announced;
    else {
      cap = message->cap ? message->cap : target > 256 ? target : 256;
      while (cap < target) {
        if (cap > SIZE_MAX / 2) { cap = target; break; }
        cap *= 2;
      }
    }
    if (cap > message->limit) cap = message->limit;
    int had_old_allocation = message->data != NULL;
    uintptr_t old_address = (uintptr_t)message->data;
    sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_CALLS, 1);
    sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_OLD_CAPACITY_BYTES, message->cap);
    sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_REQUEST_BYTES, cap);
    unsigned char *buffer = realloc(message->data, cap);
    if (!buffer) {
      sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_FAILURES, 1);
      return SP_NOMEM;
    }
    sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_SUCCESSES, 1);
    if (had_old_allocation && (uintptr_t)buffer != old_address)
      sp_wss_diag_add(SP_WSS_DIAG_MESSAGE_REALLOC_ADDRESS_CHANGED, 1);
    message->data = buffer; message->cap = cap;
  }
  /* A direct receive already filled this tail. realloc preserves those bytes;
   * its old input pointer must never be dereferenced after possible growth. */
  if (size && !stored) {
    sp_wss_diag_add(SP_WSS_DIAG_SCRATCH_APPEND_COPY_CALLS, 1);
    sp_wss_diag_add(SP_WSS_DIAG_SCRATCH_APPEND_COPY_BYTES, size);
    memcpy(message->data + message->size, data, size);
  }
  message->size = need;
  return SP_OK;
}

sp_ws_io *sp_ws_io_start(sp_ws *w, unsigned operation, unsigned kind,
                         const unsigned char *data, size_t size, uint32_t timeout,
                         sp_cancel *cancel, int *error) {
  int err = SP_INVALID;
  if (!w || !timeout || (size && !data)) goto fail;
  if (operation == SP_WS_SEND) {
    if ((kind != 1 && kind != 2 && kind != 9 && kind != 10) ||
        (kind >= 9 && size > 125) || (kind == 1 && !sp_ws_utf8(data, size))) goto fail;
    if (size > w->session->config.max_body_bytes) { err = SP_INPUT_LIMIT; goto fail; }
  } else if (operation == SP_WS_CLOSE) {
    if (size > 123 || !sp_ws_code(kind) || !sp_ws_utf8(data, size)) goto fail;
  } else if (operation != SP_WS_RECEIVE) goto fail;
  if (pthread_mutex_trylock(&w->gate)) { err = SP_BUSY; goto fail; }
  sp_ws_io *io = &w->operation;
  memset(io, 0, sizeof *io);
  io->socket = w; io->operation = operation; io->deadline = sp_ws_clock() + timeout;
  io->message.limit = w->session->config.max_body_bytes; io->message.limit_error = SP_BODY_LIMIT;
  io->cancel = cancel; sp_cancel_retain(cancel);
  if (operation == SP_WS_CLOSE && w->closed) sp_ws_io_end(io, SP_OK);
  else if (w->broken || w->closed || (operation == SP_WS_SEND && w->closing)) sp_ws_io_end(io, SP_CLOSED);
  else if (operation == SP_WS_SEND) sp_ws_io_send(io, kind, data, size, 0);
  else if (operation == SP_WS_CLOSE) {
    io->control[0] = (unsigned char)(kind >> 8); io->control[1] = (unsigned char)kind;
    if (size) memcpy(io->control + 2, data, size);
    sp_ws_io_send(io, 8, io->control, size + 2, 1);
  }
  if (error) *error = SP_OK;
  return io;
fail:
  if (error) *error = err;
  return NULL;
}

static int sp_ws_io_close_message(sp_ws_io *io) {
  io->socket->closed = 1;
  free(io->message.data); io->message = (sp_buffer){.limit = 125, .limit_error = SP_BODY_LIMIT};
  if (io->operation == SP_WS_RECEIVE) {
    int error = sp_ws_append(&io->message, io->control, io->control_size, 0, 1, 0);
    if (error) return sp_ws_io_end(io, error);
  }
  io->kind = 8;
  return sp_ws_io_end(io, SP_OK);
}

static int sp_ws_io_advance(sp_ws_io *io) {
  sp_ws *w = io->socket;
  /* Bound work per dispatch even with a peer that continuously supplies data.
   * The immediate wake below yields to other Bend activations; no helper is
   * needed and buffered TLS data is retried without requiring a fresh edge. */
  for (unsigned steps = 0; steps < 64; steps++) {
    int error = sp_ws_check(io->deadline, io->cancel);
    if (error) return sp_ws_io_end(io, error);
    if (io->sending) {
      size_t sent = 0;
      sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_CALLS, 1);
      sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_REQUEST_BYTES, io->out_size - io->offset);
      CURLcode code = curl_ws_send(w->session->slots[0].easy,
        io->out ? io->out + io->offset : (const unsigned char *)"",
        io->out_size - io->offset, &sent, 0, io->send_flags);
      sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_RETURN_BYTES, sent);
      if(!code)
        sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_SUCCESS_CALLS, 1);
      else if(code == CURLE_AGAIN)
        sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_AGAIN_CALLS, 1);
      else
        sp_wss_diag_add(SP_WSS_DIAG_WS_SEND_ERROR_CALLS, 1);
      io->offset += sent;
      if (code && code != CURLE_AGAIN) return sp_ws_io_end(io, SP_TRANSPORT);
      if (code == CURLE_AGAIN || io->offset != io->out_size) { io->events = POLLOUT; return SP_WS_PENDING; }
      io->sending = 0;
      if (!io->after_send) return sp_ws_io_end(io, SP_OK);
      if (io->after_send == 2) return sp_ws_io_close_message(io);
      if (io->operation == SP_WS_CLOSE) w->closing = 1;
      continue;
    }
    unsigned char scratch[16384]; size_t got = 0; const struct curl_ws_frame *meta = NULL;
    sp_buffer *message = &io->message;
    int direct = message->cap - message->size >= sizeof scratch;
    unsigned char *chunk = direct ? message->data + message->size : scratch;
    sp_wss_diag_add(direct ? SP_WSS_DIAG_DIRECT_RECV_CALLS : SP_WSS_DIAG_SCRATCH_RECV_CALLS, 1);
    sp_wss_diag_add(direct ? SP_WSS_DIAG_DIRECT_RECV_REQUEST_BYTES : SP_WSS_DIAG_SCRATCH_RECV_REQUEST_BYTES, sizeof scratch);
    sp_wss_diag_add(SP_WSS_DIAG_FRAMED_RECV_CALLS, 1);
    CURLcode code = curl_ws_recv(w->session->slots[0].easy, chunk, sizeof scratch, &got, &meta);
    if(!code)
      sp_wss_diag_add(SP_WSS_DIAG_FRAMED_RECV_SUCCESS_CALLS, 1);
    else if(code == CURLE_AGAIN)
      sp_wss_diag_add(SP_WSS_DIAG_FRAMED_RECV_AGAIN_CALLS, 1);
    else
      sp_wss_diag_add(SP_WSS_DIAG_FRAMED_RECV_ERROR_CALLS, 1);
    sp_wss_diag_add(SP_WSS_DIAG_FRAMED_RECV_BYTES, got);
    if (!code) {
      sp_wss_diag_add(direct ? SP_WSS_DIAG_DIRECT_RECV_SUCCESS : SP_WSS_DIAG_SCRATCH_RECV_SUCCESS, 1);
      sp_wss_diag_add(direct ? SP_WSS_DIAG_DIRECT_RECV_RETURN_BYTES : SP_WSS_DIAG_SCRATCH_RECV_RETURN_BYTES, got);
    }
    if (code == CURLE_AGAIN) { io->events = POLLIN; return SP_WS_PENDING; }
    if (code || !meta) return sp_ws_io_end(io, code == CURLE_GOT_NOTHING ? SP_CLOSED : SP_TRANSPORT);
    unsigned flags = meta->flags;
    unsigned frame_kind = flags & CURLWS_CLOSE ? 8 : flags & CURLWS_PING ? 9 : flags & CURLWS_PONG ? 10 :
      flags & CURLWS_TEXT ? 1 : flags & CURLWS_BINARY ? 2 : 0;
    if (!frame_kind || meta->offset < 0 || meta->bytesleft < 0) return sp_ws_io_end(io, SP_PROTOCOL);
    if (frame_kind >= 8) {
      if (!meta->offset) io->control_size = 0;
      if ((flags & CURLWS_CONT) || got > sizeof io->control - io->control_size ||
          (uint64_t)meta->bytesleft > sizeof io->control - io->control_size - got) return sp_ws_io_end(io, SP_PROTOCOL);
      memcpy(io->control + io->control_size, chunk, got); io->control_size += got;
      if (meta->bytesleft) continue;
      if (frame_kind == 9) sp_ws_io_send(io, 10, io->control, io->control_size, 1);
      else if (frame_kind == 8) {
        if (!sp_ws_close_valid(io->control, io->control_size)) return sp_ws_io_end(io, SP_PROTOCOL);
        if (!w->closing) sp_ws_io_send(io, 8, io->control, io->control_size, 2);
        else return sp_ws_io_close_message(io);
      }
      continue;
    }
    if (io->message_kind && io->message_kind != frame_kind) return sp_ws_io_end(io, SP_PROTOCOL);
    io->message_kind = frame_kind;
    if (got > message->limit - message->size || (uint64_t)meta->bytesleft > message->limit - message->size - got)
      return sp_ws_io_end(io, SP_BODY_LIMIT);
    error = sp_ws_append(message, chunk, got, (size_t)meta->bytesleft, !(flags & CURLWS_CONT), direct);
    if (error) return sp_ws_io_end(io, error);
    if (!meta->bytesleft && !(flags & CURLWS_CONT)) {
      if (frame_kind == 1 && !sp_ws_utf8(message->data, message->size)) return sp_ws_io_end(io, SP_PROTOCOL);
      if (io->operation == SP_WS_CLOSE) {
        /* A close handshake may pass complete data messages before its ack. */
        free(message->data); *message = (sp_buffer){.limit = w->session->config.max_body_bytes, .limit_error = SP_BODY_LIMIT};
        io->message_kind = 0;
      } else { io->kind = frame_kind; return sp_ws_io_end(io, SP_OK); }
    }
  }
  io->events = 0;
  return SP_WS_PENDING;
}

int sp_ws_io_step(sp_ws_io *io, unsigned *kind, unsigned char **data, size_t *size) {
  sp_wss_diag_add(SP_WSS_DIAG_WS_STEP_CALLS, 1);
  int error = io->done ? io->error : sp_ws_io_advance(io);
  if (!error && io->operation == SP_WS_RECEIVE) {
    *kind = io->kind; *data = io->message.data; *size = io->message.size;
    io->message.data = NULL; io->message.size = io->message.cap = 0;
  }
  return error;
}
int sp_ws_io_poll(sp_ws_io *io, int *fd, short *events, uint64_t *wake_ms) {
  sp_wss_diag_add(SP_WSS_DIAG_WS_READY_QUERY_CALLS, 1);
  int error = sp_ws_check(io->deadline, io->cancel);
  if (error) return sp_ws_io_end(io, error);
  *fd = io->socket->fd; *events = io->events;
  uint64_t now = sp_ws_clock();
  *wake_ms = !io->events ? now : io->deadline;
  /* Cancellation can originate outside Bend, so poll it even with no traffic.
   * This preserves the blocking API's maximum 50 ms cancellation latency. */
  if (io->cancel && *wake_ms > now + 50) *wake_ms = now + 50;
  return SP_OK;
}
void sp_ws_io_free(sp_ws_io *io) {
  if (!io) return;
  sp_ws *socket = io->socket;
  free(io->message.data); sp_cancel_free(io->cancel);
  io->message.data = NULL; io->cancel = NULL; io->out = NULL; io->socket = NULL;
  /* Do not access embedded state after releasing its owner's gate. */
  pthread_mutex_unlock(&socket->gate);
}
static int sp_ws_io_block(sp_ws_io *io, unsigned *kind, unsigned char **data, size_t *size) {
  int error;
  while ((error = sp_ws_io_step(io, kind, data, size)) == SP_WS_PENDING) {
    int socket; short events; uint64_t wake;
    error = sp_ws_io_poll(io, &socket, &events, &wake); if (error) break;
    uint64_t now = sp_ws_clock(), gap = wake > now ? wake - now : 0;
    struct pollfd fd = {.fd = socket, .events = events};
    if (poll(&fd, events ? 1 : 0, gap > INT_MAX ? INT_MAX : (int)gap) < 0 && errno != EINTR) {
      error = sp_ws_io_end(io, SP_TRANSPORT); break;
    }
  }
  sp_ws_io_free(io); return error;
}
int sp_ws_send(sp_ws *w, unsigned kind, const unsigned char *data, size_t size, uint32_t timeout, sp_cancel *cancel) {
  int error; sp_ws_io *io = sp_ws_io_start(w, SP_WS_SEND, kind, data, size, timeout, cancel, &error);
  return io ? sp_ws_io_block(io, NULL, NULL, NULL) : error;
}
int sp_ws_receive(sp_ws *w, unsigned *kind, unsigned char **data, size_t *size, uint32_t timeout, sp_cancel *cancel) {
  if (!w || !kind || !data || !size || !timeout) return SP_INVALID;
  *kind = 0; *data = NULL; *size = 0;
  int error; sp_ws_io *io = sp_ws_io_start(w, SP_WS_RECEIVE, 0, NULL, 0, timeout, cancel, &error);
  return io ? sp_ws_io_block(io, kind, data, size) : error;
}
int sp_ws_close(sp_ws *w, unsigned code, const unsigned char *reason, size_t size, uint32_t timeout, sp_cancel *cancel) {
  int error; sp_ws_io *io = sp_ws_io_start(w, SP_WS_CLOSE, code, reason, size, timeout, cancel, &error);
  return io ? sp_ws_io_block(io, NULL, NULL, NULL) : error;
}
