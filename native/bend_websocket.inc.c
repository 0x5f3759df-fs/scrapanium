#if defined(CID_SCRAPANIUM_WS_UPGRADE) || defined(CID_SCRAPANIUM_WS_SEND) || defined(CID_SCRAPANIUM_WS_RECEIVE) || defined(CID_SCRAPANIUM_WS_CLOSE)
typedef struct {
  sp_ws *socket;
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
  if (j->operation == 0) { j->socket = sp_ws_upgrade(j->session, &j->request, j->cancel, &j->error); j->session = NULL; }
  else if (j->operation == 1) j->error = sp_ws_send(j->socket, j->kind, j->bytes->data, j->bytes->size, j->timeout, j->cancel);
  else if (j->operation == 2) j->error = sp_ws_receive(j->socket, &j->kind, &j->bytes->data, &j->bytes->size, j->timeout, j->cancel);
  else j->error = sp_ws_close(j->socket, j->code, j->bytes->data, j->bytes->size, j->timeout, j->cancel);
}
static Term sp_bend_ws_term(Env e, sp_ws *socket) {
  return io_box(e, CID_WEBSOCKET, sp_bend_handle_new(socket, 5));
}
static Term sp_bend_ws_pack(Env e, IoWork *work) {
  SpWsJob *j = (SpWsJob *)work->data;
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
  return io_work(work, sp_bend_ws_call, sp_bend_ws_pack);
}
#endif
#ifdef CID_SCRAPANIUM_WS_RECEIVE
static Term sp_bend_ws_receive(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->operation = 2; j->socket = sp_bend_ws_take(e, f[0]); j->timeout = f[1];
  j->bytes = io_mem(calloc(1, sizeof *j->bytes)); j->cancel = sp_bend_ws_token(e, f[2], &j->error);
  return io_work(work, sp_bend_ws_call, sp_bend_ws_pack);
}
#endif
#ifdef CID_SCRAPANIUM_WS_CLOSE
static Term sp_bend_ws_close(Env e, Term *f, IoWork *work) {
  SpWsJob *j = io_mem(calloc(1, sizeof *j)); work->data = (char *)j;
  j->operation = 3; j->socket = sp_bend_ws_take(e, f[0]); j->code = f[1]; j->timeout = f[3];
  j->bytes = io_mem(calloc(1, sizeof *j->bytes)); int invalid = 0;
  j->bytes->data = (unsigned char *)sp_bend_string(e, f[2], &invalid, 0, &j->bytes->size);
  j->cancel = sp_bend_ws_token(e, f[4], &j->error);
  return io_work(work, sp_bend_ws_call, sp_bend_ws_pack);
}
#endif
#ifdef CID_SCRAPANIUM_WS_DISCARD
static Term sp_bend_ws_discard(Env e, Term *f, IoWork *work) {
  (void)work; sp_ws_free(sp_bend_ws_take(e, f[0])); return term_pak(CID_UNIT, 0);
}
#endif
