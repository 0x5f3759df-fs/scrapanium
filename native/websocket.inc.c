/* Included in scrapanium.c to share the profile and cancellation machinery. */
#include <poll.h>
#include <time.h>
#include "ws_async.h"
#include "ws_handshake.inc.c"

struct sp_ws {
  sp_session *session;
  sp_response *handshake;
  pthread_mutex_t gate;
  curl_socket_t fd;
  int broken, closing, closed;
};

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
  sp_ws_io *io = calloc(1, sizeof *io);
  if (!io) { pthread_mutex_unlock(&w->gate); err = SP_NOMEM; goto fail; }
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
  if (io->operation == SP_WS_RECEIVE &&
      (sp_write((char *)io->control, 1, io->control_size, &io->message) != io->control_size || io->message.error))
    return sp_ws_io_end(io, io->message.error);
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
      CURLcode code = curl_ws_send(w->session->slots[0].easy,
        io->out ? io->out + io->offset : (const unsigned char *)"",
        io->out_size - io->offset, &sent, 0, io->send_flags);
      io->offset += sent;
      if (code && code != CURLE_AGAIN) return sp_ws_io_end(io, SP_TRANSPORT);
      if (code == CURLE_AGAIN || io->offset != io->out_size) { io->events = POLLOUT; return SP_WS_PENDING; }
      io->sending = 0;
      if (!io->after_send) return sp_ws_io_end(io, SP_OK);
      if (io->after_send == 2) return sp_ws_io_close_message(io);
      if (io->operation == SP_WS_CLOSE) w->closing = 1;
      continue;
    }
    unsigned char chunk[16384]; size_t got = 0; const struct curl_ws_frame *meta = NULL;
    CURLcode code = curl_ws_recv(w->session->slots[0].easy, chunk, sizeof chunk, &got, &meta);
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
    sp_buffer *message = &io->message;
    if (got > message->limit - message->size || (uint64_t)meta->bytesleft > message->limit - message->size - got)
      return sp_ws_io_end(io, SP_BODY_LIMIT);
    if (sp_write((char *)chunk, 1, got, message) != got || message->error) return sp_ws_io_end(io, message->error);
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
  int error = io->done ? io->error : sp_ws_io_advance(io);
  if (!error && io->operation == SP_WS_RECEIVE) {
    *kind = io->kind; *data = io->message.data; *size = io->message.size;
    io->message.data = NULL; io->message.size = io->message.cap = 0;
  }
  return error;
}
int sp_ws_io_poll(sp_ws_io *io, int *fd, short *events, uint64_t *wake_ms) {
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
  free(io->message.data); sp_cancel_free(io->cancel);
  pthread_mutex_unlock(&io->socket->gate); free(io);
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
