/* Included in scrapanium.c to share the profile and cancellation machinery. */
#include <poll.h>
#include <time.h>
#include "ws_handshake.inc.c"

struct sp_ws {
  sp_session *session;
  sp_response *handshake;
  pthread_mutex_t gate;
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
static int sp_ws_wait(sp_ws *w, short events, uint64_t deadline, sp_cancel *cancel) {
  int error = sp_ws_check(deadline, cancel);
  if (error) return error;
  curl_socket_t socket = CURL_SOCKET_BAD;
  if (curl_easy_getinfo(w->session->slots[0].easy, CURLINFO_ACTIVESOCKET, &socket) || socket == CURL_SOCKET_BAD)
    return SP_CLOSED;
  uint64_t now = sp_ws_clock();
  if (now >= deadline) return SP_TIMEOUT;
  uint64_t remaining = deadline - now;
  int wait = remaining > 50 ? 50 : (int)remaining;
  struct pollfd fd = {.fd = socket, .events = events};
  if (poll(&fd, 1, wait) < 0 && errno != EINTR) return SP_TRANSPORT;
  return sp_ws_check(deadline, cancel);
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
  if (err) goto fail;
  /* CONNECT_ONLY handles stay attached to their multi for their entire life. */
  if (error) *error = SP_OK;
  return w;
fail:
  if (w) sp_ws_free(w); else sp_session_free(s);
  if (error) *error = err;
  return NULL;
}

static int sp_ws_send_inner(sp_ws *w, unsigned kind, const unsigned char *data, size_t size,
                            uint64_t deadline, sp_cancel *cancel) {
  unsigned flags = kind == 1 ? CURLWS_TEXT : kind == 2 ? CURLWS_BINARY : kind == 8 ? CURLWS_CLOSE :
    kind == 9 ? CURLWS_PING : CURLWS_PONG;
  size_t offset = 0;
  do {
    int error = sp_ws_check(deadline, cancel); if (error) return error;
    size_t sent = 0;
    CURLcode code = curl_ws_send(w->session->slots[0].easy, data ? data + offset : (const unsigned char *)"",
      size - offset, &sent, 0, flags);
    offset += sent;
    if (code == CURLE_OK && offset == size) return SP_OK;
    if (code && code != CURLE_AGAIN) return SP_TRANSPORT;
    error = sp_ws_wait(w, POLLOUT, deadline, cancel); if (error) return error;
  } while (1);
}
int sp_ws_send(sp_ws *w, unsigned kind, const unsigned char *data, size_t size, uint32_t timeout, sp_cancel *cancel) {
  if (!w || !timeout || (size && !data) || (kind != 1 && kind != 2 && kind != 9 && kind != 10) ||
      (kind >= 9 && size > 125) || (kind == 1 && !sp_ws_utf8(data, size))) return SP_INVALID;
  if (size > w->session->config.max_body_bytes) return SP_INPUT_LIMIT;
  if (pthread_mutex_trylock(&w->gate)) return SP_BUSY;
  sp_cancel_retain(cancel);
  int error = w->broken || w->closed || w->closing ? SP_CLOSED : sp_ws_send_inner(w, kind, data, size, sp_ws_clock() + timeout, cancel);
  if (error) w->broken = 1;
  sp_cancel_free(cancel); pthread_mutex_unlock(&w->gate);
  return error;
}

static int sp_ws_receive_inner(sp_ws *w, unsigned *kind, unsigned char **data, size_t *size,
                               uint64_t deadline, sp_cancel *cancel) {
  sp_buffer message = {.limit = w->session->config.max_body_bytes, .limit_error = SP_BODY_LIMIT};
  unsigned char control[125]; size_t control_size = 0;
  unsigned message_kind = 0; int error = 0;
  for (;;) {
    error = sp_ws_check(deadline, cancel); if (error) break;
    unsigned char chunk[16384]; size_t got = 0; const struct curl_ws_frame *meta = NULL;
    CURLcode code = curl_ws_recv(w->session->slots[0].easy, chunk, sizeof chunk, &got, &meta);
    if (code == CURLE_AGAIN) { error = sp_ws_wait(w, POLLIN, deadline, cancel); if (error) break; continue; }
    if (code || !meta) { error = code == CURLE_GOT_NOTHING ? SP_CLOSED : SP_TRANSPORT; break; }
    unsigned flags = meta->flags;
    unsigned frame_kind = flags & CURLWS_CLOSE ? 8 : flags & CURLWS_PING ? 9 : flags & CURLWS_PONG ? 10 :
      flags & CURLWS_TEXT ? 1 : flags & CURLWS_BINARY ? 2 : 0;
    if (!frame_kind || meta->offset < 0 || meta->bytesleft < 0) { error = SP_PROTOCOL; break; }
    if (frame_kind >= 8) {
      if (!meta->offset) control_size = 0;
      if ((flags & CURLWS_CONT) || got > sizeof control - control_size ||
          (uint64_t)meta->bytesleft > sizeof control - control_size - got) { error = SP_PROTOCOL; break; }
      memcpy(control + control_size, chunk, got); control_size += got;
      if (meta->bytesleft) continue;
      if (frame_kind == 9) { error = sp_ws_send_inner(w, 10, control, control_size, deadline, cancel); if (error) break; }
      else if (frame_kind == 8) {
        if (!sp_ws_close_valid(control, control_size)) { error = SP_PROTOCOL; break; }
        if (!w->closing) { error = sp_ws_send_inner(w, 8, control, control_size, deadline, cancel); if (error) break; }
        w->closed = 1;
        free(message.data); message = (sp_buffer){.limit = 125, .limit_error = SP_BODY_LIMIT};
        if (sp_write((char *)control, 1, control_size, &message) != control_size || message.error) { error = message.error; break; }
        *kind = 8; *data = message.data; *size = message.size; return SP_OK;
      }
      continue;
    }
    if (message_kind && message_kind != frame_kind) { error = SP_PROTOCOL; break; }
    message_kind = frame_kind;
    if (got > message.limit - message.size || (uint64_t)meta->bytesleft > message.limit - message.size - got) { error = SP_BODY_LIMIT; break; }
    if (sp_write((char *)chunk, 1, got, &message) != got || message.error) { error = message.error; break; }
    if (!meta->bytesleft && !(flags & CURLWS_CONT)) {
      if (message_kind == 1 && !sp_ws_utf8(message.data, message.size)) { error = SP_PROTOCOL; break; }
      *kind = message_kind; *data = message.data; *size = message.size; return SP_OK;
    }
  }
  free(message.data); return error;
}
int sp_ws_receive(sp_ws *w, unsigned *kind, unsigned char **data, size_t *size, uint32_t timeout, sp_cancel *cancel) {
  if (!w || !kind || !data || !size || !timeout) return SP_INVALID;
  *kind = 0; *data = NULL; *size = 0;
  if (pthread_mutex_trylock(&w->gate)) return SP_BUSY;
  sp_cancel_retain(cancel);
  int error = w->broken || w->closed ? SP_CLOSED : sp_ws_receive_inner(w, kind, data, size, sp_ws_clock() + timeout, cancel);
  if (error) w->broken = 1;
  sp_cancel_free(cancel); pthread_mutex_unlock(&w->gate); return error;
}
int sp_ws_close(sp_ws *w, unsigned code, const unsigned char *reason, size_t size, uint32_t timeout, sp_cancel *cancel) {
  if (!w || !timeout || size > 123 || (size && !reason) || !sp_ws_code(code) || !sp_ws_utf8(reason, size)) return SP_INVALID;
  if (pthread_mutex_trylock(&w->gate)) return SP_BUSY;
  sp_cancel_retain(cancel);
  uint64_t deadline = sp_ws_clock() + timeout;
  unsigned char payload[125] = {(unsigned char)(code >> 8), (unsigned char)code};
  if (size) memcpy(payload + 2, reason, size);
  int error = w->closed ? SP_OK : w->broken ? SP_CLOSED : sp_ws_send_inner(w, 8, payload, size + 2, deadline, cancel);
  w->closing = 1;
  while (!error && !w->closed) {
    unsigned kind; unsigned char *data = NULL; size_t length;
    error = sp_ws_receive_inner(w, &kind, &data, &length, deadline, cancel); free(data);
  }
  if (error) w->broken = 1;
  sp_cancel_free(cancel); pthread_mutex_unlock(&w->gate); return error;
}
