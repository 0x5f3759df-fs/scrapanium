#if defined(CID_SCRAPANIUM_WS_UPGRADE) || defined(CID_SCRAPANIUM_WS_SEND) || defined(CID_SCRAPANIUM_WS_RECEIVE) || defined(CID_SCRAPANIUM_WS_CLOSE)
#include "ws_async.h"
typedef struct {
  sp_ws *socket;
  sp_ws_io *pending;
  sp_session *session;
  sp_cancel *cancel;
  sp_request request;
  SpBendBytes *bytes;
  unsigned kind, code;
  uint32_t timeout;
  int operation, error;
} SpWsJob;
static sp_cancel *sp_bend_ws_token(Env e, Term value, int *error) {
  Term f[2]; sp_bend_take(e, value, 2, f);
  if (!f[0] && !f[1]) return NULL; /* Internal no-cancellation sentinel. */
  u32 slot = f[0];
  if (!slot || slot > sp_bend_count) { *error = SP_INVALID; return NULL; }
  SpBendHandle *h = sp_bend_handles + slot - 1;
  if (!h->ptr || h->kind != 3 || h->generation != f[1]) { *error = SP_INVALID; return NULL; }
  sp_cancel_retain(h->ptr); return h->ptr;
}
static void sp_bend_ws_call(IoWork *work) {
  SpWsJob *j = (SpWsJob *)work->data;
  if (j->error) return;
  j->socket = sp_ws_upgrade(j->session, &j->request, j->cancel, &j->error); j->session = NULL;
}
static Term sp_bend_ws_term(Env e, sp_ws *socket) {
  return io_box(e, CID_WEBSOCKET, sp_bend_handle_new(socket, 5));
}
static Term sp_bend_ws_pack(Env e, IoWork *work) {
  SpWsJob *j = (SpWsJob *)work->data;
  sp_ws_io_free(j->pending); j->pending = NULL;
  Term result = j->error ? io_fail(e, j->error, sp_error_message(j->error)) : io_done(e, term_pak(CID_UNIT, 0));
  /* Do not allocate an unused result constructor: Bend's heap has no GC. */
  if (!j->error && j->operation == 0) {
    Term field[1]; sp_bend_take(e, result, 1, field);
    result = io_done(e, sp_bend_ws_term(e, j->socket)); j->socket = NULL;
  }
#ifdef CID_SCRAPANIUM_WS_RECEIVE
  if (!j->error && j->operation == 2) {
    Term unused[1]; sp_bend_take(e, result, 1, unused);
    Term f[] = {j->kind, sp_bend_handle_new(j->bytes, 4), j->bytes->size};
    result = io_done(e, sp_bend_node(e, CID_WSMESSAGE, 3, f)); j->bytes = NULL;
  }
#endif
  if (j->operation == 1 || j->operation == 2) result = io_tup(e, sp_bend_ws_term(e, j->socket), result);
  else if (j->operation == 3) sp_ws_free(j->socket);
  else if (j->error) sp_session_free(j->session);
  sp_bend_bytes_free(j->bytes); sp_cancel_free(j->cancel);
  free((char *)j->request.url);
  for (size_t i = 0; i < j->request.header_count; i++) free((char *)j->request.headers[i]);
  free((void *)j->request.headers); free(j); return result;
}

/* These callbacks run on Bend's single IO loop. Audited against the compiler
 * pin b2791abbfaba463ed67e81ca904523e31546682b: bend2/comp.ts io_loop calls
 * io_step/io_wait; reduction workers never execute these effects or callbacks.
 * In particular, sp_ws_io_start/step/free stay on the mutex owner's thread even
 * with --threads > 1. Park on readiness instead of handing every frame to a
 * worker and waking the IO loop again through its completion pipe. */
static Term sp_bend_ws_ready(Env e, IoWork *work) {
  /* io_step runs immediate continuations until something parks. With other
   * activations alive, yield even when this socket stays continuously ready,
   * so timers, cancellation and independent sockets keep making progress. */
  return io_live > 1 ? io_wait_on(work, 0, 0, io_tick(), sp_bend_ws_pack)
    : sp_bend_ws_pack(e, work);
}
static Term sp_bend_ws_more(Env e, IoWork *work) {
  SpWsJob *j = (SpWsJob *)work->data;
  j->error = sp_ws_io_step(j->pending, &j->kind, &j->bytes->data, &j->bytes->size);
  if (j->error == SP_WS_PENDING) {
    int fd; short events; uint64_t wake;
    j->error = sp_ws_io_poll(j->pending, &fd, &events, &wake);
    if (!j->error) return io_wait_on(work, fd, events, wake * 1000000, sp_bend_ws_more);
  }
  return sp_bend_ws_ready(e, work);
}
static Term sp_bend_ws_start(Env e, IoWork *work) {
  SpWsJob *j = (SpWsJob *)work->data;
  if (!j->error) j->pending = sp_ws_io_start(j->socket, j->operation,
    j->operation == SP_WS_CLOSE ? j->code : j->kind,
    j->bytes->data, j->bytes->size, j->timeout, j->cancel, &j->error);
  return j->error ? sp_bend_ws_ready(e, work) : sp_bend_ws_more(e, work);
}
#endif

#if defined(CID_SCRAPANIUM_WS_SEND) || defined(CID_SCRAPANIUM_WS_RECEIVE) || defined(CID_SCRAPANIUM_WS_CLOSE) || defined(CID_SCRAPANIUM_WS_DISCARD)
static sp_ws *sp_bend_ws_take(Env e, Term value) {
  Term f[1]; sp_bend_take(e, value, 1, f); return sp_bend_handle_take(f[0], 5);
}
#endif
#ifdef CID_SCRAPANIUM_WS_UPGRADE
static Term sp_bend_ws_upgrade(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->session = sp_bend_session(e, f[0]); j->request.method = "GET";
  int invalid = 0;
  j->request.url = sp_bend_string(e, f[1], &invalid, 1, NULL);
  Term list = f[2]; size_t cap = 0;
  while (term_aux(list) == CID_CON) {
    Term item[2]; sp_bend_take(e, list, 2, item); list = item[1];
    if (j->request.header_count == cap) {
      cap = cap ? cap * 2 : 8;
      j->request.headers = io_mem(realloc((void *)j->request.headers, cap * sizeof(char *)));
    }
    ((const char **)j->request.headers)[j->request.header_count++] = sp_bend_string(e, item[0], &invalid, 1, NULL);
  }
  if (invalid) j->error = SP_INVALID;
  j->cancel = sp_bend_ws_token(e, f[3], &j->error);
  return io_work(work, sp_bend_ws_call, sp_bend_ws_pack);
}
#endif
#ifdef CID_SCRAPANIUM_WS_SEND
static Term sp_bend_ws_send(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->operation = 1; j->socket = sp_bend_ws_take(e, f[0]);
  j->kind = f[1]; j->bytes = sp_bend_bytes(e, f[2]); j->timeout = f[3];
  j->cancel = sp_bend_ws_token(e, f[4], &j->error);
  return sp_bend_ws_start(e, work);
}
#endif
#ifdef CID_SCRAPANIUM_WS_RECEIVE
static Term sp_bend_ws_receive(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->operation = 2; j->socket = sp_bend_ws_take(e, f[0]); j->timeout = f[1];
  j->bytes = io_mem(calloc(1, sizeof *j->bytes)); j->cancel = sp_bend_ws_token(e, f[2], &j->error);
  return sp_bend_ws_start(e, work);
}
#endif
#ifdef CID_SCRAPANIUM_WS_CLOSE
static Term sp_bend_ws_close(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->operation = 3; j->socket = sp_bend_ws_take(e, f[0]); j->code = f[1]; j->timeout = f[3];
  j->bytes = io_mem(calloc(1, sizeof *j->bytes)); int invalid = 0;
  j->bytes->data = (unsigned char *)sp_bend_string(e, f[2], &invalid, 0, &j->bytes->size);
  j->cancel = sp_bend_ws_token(e, f[4], &j->error);
  return sp_bend_ws_start(e, work);
}
#endif
#ifdef CID_SCRAPANIUM_WS_DISCARD
static Term sp_bend_ws_discard(Env e, Term *f, IoWork *work) {
  (void)work; sp_ws_free(sp_bend_ws_take(e, f[0])); return term_pak(CID_UNIT, 0);
}
#endif
