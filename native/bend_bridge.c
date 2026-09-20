/* Included by Bend after its runtime. This ABI is pinned in dependencies.json. */
#include "scrapanium.h"
#include <sys/stat.h>

typedef struct { unsigned char *data; size_t size; } SpBendBytes;
static void sp_bend_bytes_free(SpBendBytes *b) { if (b) { free(b->data); free(b); } }

#if defined(CID_SCRAPANIUM_SEND) || defined(CID_SCRAPANIUM_BATCH) || defined(CID_SCRAPANIUM_SEND_CANCEL) || defined(CID_SCRAPANIUM_BATCH_CANCEL)
#define SP_BEND_BUFFERED
#endif
#if defined(CID_SCRAPANIUM_DOWNLOAD) || defined(CID_SCRAPANIUM_DOWNLOAD_CANCEL)
#define SP_BEND_DOWNLOAD
#endif
#if defined(SP_BEND_BUFFERED) || defined(SP_BEND_DOWNLOAD)
#define SP_BEND_REQUESTS
#endif

/* Handles carry a slot and generation, never a host pointer. All registry
 * operations run on Bend's event loop. Consuming a handle invalidates it before
 * transferring the resource to a worker; returning it creates a new generation.
 * This also rejects a Base File incorrectly wrapped as a Scrapanium handle. */
typedef struct { void *ptr; u32 generation, next; int kind; } SpBendHandle;
static SpBendHandle *sp_bend_handles;
static u32 sp_bend_count, sp_bend_capacity, sp_bend_free;

static Term sp_bend_handle_new(void *ptr, int kind) {
  u32 index;
  if (sp_bend_free) {
    index = sp_bend_free - 1; sp_bend_free = sp_bend_handles[index].next;
  } else {
    if (sp_bend_count == 0xffffff) err_fail("Scrapanium handle capacity exceeded");
    if (sp_bend_count == sp_bend_capacity) {
      u32 cap = sp_bend_capacity ? sp_bend_capacity * 2 : 64;
      if (cap > 0xffffff) cap = 0xffffff;
      sp_bend_handles = io_mem(realloc(sp_bend_handles, (size_t)cap * sizeof *sp_bend_handles));
      memset(sp_bend_handles + sp_bend_capacity, 0, (cap - sp_bend_capacity) * sizeof *sp_bend_handles);
      sp_bend_capacity = cap;
    }
    index = sp_bend_count++;
  }
  SpBendHandle *h = sp_bend_handles + index;
  h->generation++; h->ptr = ptr; h->kind = kind;
  return io_hand(((u64)h->generation << 24) | (index + 1));
}
static void *sp_bend_handle_take(Term t, int kind) {
  u64 id = io_hand_v(t); u32 slot = id & 0xffffff;
  if (!slot || slot > sp_bend_count) err_fail("invalid Scrapanium handle");
  SpBendHandle *h = sp_bend_handles + slot - 1;
  if (!h->ptr || h->kind != kind || h->generation != (id >> 24)) err_fail("invalid Scrapanium handle");
  void *ptr = h->ptr; h->ptr = NULL;
  if (h->generation != UINT32_MAX) { h->next = sp_bend_free; sp_bend_free = slot; }
  return ptr;
}
static void sp_bend_cleanup(void) {
  for (u32 i = 0; i < sp_bend_count; i++) {
    SpBendHandle *h = sp_bend_handles + i;
    if (!h->ptr) continue;
    if (h->kind == 1) sp_session_free(h->ptr);
    else if (h->kind == 2) sp_response_free(h->ptr);
    else if (h->kind == 3) sp_cancel_free(h->ptr);
    else if (h->kind == 4) sp_bend_bytes_free(h->ptr);
    else if (h->kind == 5) sp_ws_free(h->ptr);
  }
  free(sp_bend_handles);
}

static void sp_bend_take(Env e, Term t, u32 n, Term *fields) {
  if (cid_arity(term_aux(t)) != n) err_fail("Scrapanium/Bend constructor ABI mismatch");
  spare_free(e, cls_fit(n), ctr_take(e, t, n, fields));
}
static Term sp_bend_node(Env e, u32 cid, u32 n, Term *fields) {
  if (cid_arity(cid) != n) err_fail("Scrapanium/Bend constructor ABI mismatch");
  Loc l = heap_alloc(e, cls_fit(n));
  for (u32 i = 0; i < n; i++) e.mem[l + i] = io_seal(e, fields[i], cid);
  return term_ctr(cid, l);
}
#if defined(CID_SCRAPANIUM_CLOSE) || defined(SP_BEND_REQUESTS) || defined(CID_SCRAPANIUM_WS_UPGRADE)
static sp_session *sp_bend_session(Env e, Term t) {
  Term f[1]; sp_bend_take(e, t, 1, f);
  return sp_bend_handle_take(f[0], 1);
}
#endif
#if defined(CID_SCRAPANIUM_TEXT) || defined(CID_SCRAPANIUM_HEADERS) || defined(CID_SCRAPANIUM_EFFECTIVE_URL) || defined(CID_SCRAPANIUM_BYTE_AT) || defined(CID_SCRAPANIUM_SAVE) || defined(CID_SCRAPANIUM_DISCARD) || defined(CID_SCRAPANIUM_INTO_BYTES) || defined(CID_SCRAPANIUM_HEADER_PAIRS)
static sp_response *sp_bend_response(Env e, Term t) {
  Term f[4]; sp_bend_take(e, t, 4, f);
  return sp_bend_handle_take(f[0], 2);
}
#endif
#if defined(CID_SCRAPANIUM_OPEN) || defined(SP_BEND_REQUESTS)
static Term sp_bend_session_term(Env e, sp_session *s) {
  return io_box(e, CID_SESSION, sp_bend_handle_new(s, 1));
}
#endif
#if defined(SP_BEND_BUFFERED) || defined(CID_SCRAPANIUM_TEXT) || defined(CID_SCRAPANIUM_HEADERS) || defined(CID_SCRAPANIUM_EFFECTIVE_URL) || defined(CID_SCRAPANIUM_BYTE_AT) || defined(CID_SCRAPANIUM_SAVE) || defined(CID_SCRAPANIUM_HEADER_PAIRS)
static Term sp_bend_response_term(Env e, sp_response *r) {
  Term fields[] = {sp_bend_handle_new(r, 2), (Term)sp_response_status(r),
    (Term)sp_response_size(r), (Term)sp_response_http_version(r)};
  return sp_bend_node(e, CID_RESPONSE, 4, fields);
}
#endif
#ifdef SP_BEND_BUFFERED
static Term sp_bend_result(Env e, sp_response *r) {
  int error = sp_response_error(r);
  if (!error) return io_done(e, sp_bend_response_term(e, r));
  Term out = io_fail(e, (u32)error, sp_response_message(r));
  sp_response_free(r); return out;
}
#endif
static char *sp_bend_string(Env e, Term t, int *invalid, int cstring, size_t *length) {
  u64 n; char *s = io_cstr(e, t, &n);
  if (cstring && io_nul(s, n)) *invalid = 1;
  if (length) *length = (size_t)n;
  return s;
}

#if defined(CID_SCRAPANIUM_BYTES_FROM_TEXT) || defined(CID_SCRAPANIUM_WS_CLOSE)
/* io_cstr in the pinned runtime encodes arbitrary U32s without scalar checks.
 * These length-delimited payload paths must reject invalid scalars before that
 * encoding can truncate them into different, apparently valid UTF-8 bytes. */
static unsigned char *sp_bend_checked_text(Env e, Term text, int *error, size_t *length) {
  unsigned char *data = NULL; size_t size = 0, cap = 0;
  while (term_aux(text) == CID_SCON) {
    Term fields[2]; sp_bend_take(e, text, 2, fields); text = fields[1];
    u64 scalar = fields[0];
    if (*error) continue; /* Still consume the complete affine String on failure. */
    if (scalar > 0x10ffff || (scalar >= 0xd800 && scalar <= 0xdfff)) {
      *error = SP_INVALID; continue;
    }
    size_t encoded = scalar < 0x80 ? 1 : scalar < 0x800 ? 2 : scalar < 0x10000 ? 3 : 4;
    if (size > UINT32_MAX - encoded) { *error = SP_INPUT_LIMIT; continue; }
    if (size + encoded > cap) {
      size_t next = cap ? (cap > UINT32_MAX / 2 ? UINT32_MAX : cap * 2) : 64;
      unsigned char *grown = realloc(data, next);
      if (!grown) { *error = SP_NOMEM; continue; }
      data = grown; cap = next;
    }
    size += io_utf8((char *)data + size, scalar);
  }
  *length = size; return data;
}
#endif

#if defined(CID_SCRAPANIUM_BYTES_FROM_LIST) || defined(CID_SCRAPANIUM_BYTES_FROM_TEXT) || defined(CID_SCRAPANIUM_BYTES_READ_FILE) || defined(CID_SCRAPANIUM_BYTES_AT) || defined(CID_SCRAPANIUM_BYTES_TEXT) || defined(CID_SCRAPANIUM_BYTES_EQUAL) || defined(CID_SCRAPANIUM_INTO_BYTES) || defined(CID_SCRAPANIUM_WS_RECEIVE)
static Term sp_bend_bytes_term(Env e, SpBendBytes *b) {
  Term f[] = {sp_bend_handle_new(b, 4), (Term)b->size};
  return sp_bend_node(e, CID_BYTES, 2, f);
}
#endif
#if defined(CID_SCRAPANIUM_BYTES_AT) || defined(CID_SCRAPANIUM_BYTES_TEXT) || defined(CID_SCRAPANIUM_BYTES_EQUAL) || defined(CID_SCRAPANIUM_BYTES_DISCARD) || defined(CID_SCRAPANIUM_WS_SEND)
static SpBendBytes *sp_bend_bytes(Env e, Term value) {
  Term f[2]; sp_bend_take(e, value, 2, f);
  return sp_bend_handle_take(f[0], 4);
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_FROM_LIST
static Term sp_bend_bytes_from_list(Env e, Term *f, IoWork *w) {
  (void)w;
  SpBendBytes *b = io_mem(calloc(1, sizeof *b));
  size_t cap = 0; int error = 0; Term list = f[0];
  while (term_aux(list) == CID_CON) {
    Term item[2]; sp_bend_take(e, list, 2, item); list = item[1];
    if (item[0] > 255) error = SP_INVALID;
    if (error) continue;
    if (b->size == UINT32_MAX) { error = SP_INPUT_LIMIT; continue; }
    if (b->size == cap) {
      size_t next = cap ? cap * 2 : 1024;
      if (next > UINT32_MAX) next = UINT32_MAX;
      unsigned char *data = realloc(b->data, next);
      if (!data) { error = SP_NOMEM; continue; }
      b->data = data; cap = next;
    }
    b->data[b->size++] = (unsigned char)item[0];
  }
  if (error) { sp_bend_bytes_free(b); return io_fail(e, error, sp_error_message(error)); }
  return io_done(e, sp_bend_bytes_term(e, b));
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_FROM_TEXT
static Term sp_bend_bytes_from_text(Env e, Term *f, IoWork *w) {
  (void)w; int error = 0;
  SpBendBytes *b = io_mem(calloc(1, sizeof *b));
  b->data = sp_bend_checked_text(e, f[0], &error, &b->size);
  if (error) { sp_bend_bytes_free(b); return io_fail(e, error, sp_error_message(error)); }
  return io_done(e, sp_bend_bytes_term(e, b));
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_AT
static Term sp_bend_bytes_at(Env e, Term *f, IoWork *w) {
  (void)w; SpBendBytes *b = sp_bend_bytes(e, f[0]);
  Term item = f[1] < b->size ? io_box(e, CID_SOME, b->data[f[1]]) : term_pak(CID_NONE, 0);
  return io_tup(e, sp_bend_bytes_term(e, b), item);
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_TEXT
static Term sp_bend_bytes_text(Env e, Term *f, IoWork *w) {
  (void)w; SpBendBytes *b = sp_bend_bytes(e, f[0]);
  return io_tup(e, sp_bend_bytes_term(e, b), io_str(e, (const char *)(b->data ? b->data : (unsigned char *)""), b->size));
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_EQUAL
static Term sp_bend_bytes_equal(Env e, Term *f, IoWork *w) {
  (void)w;
  SpBendBytes *left = sp_bend_bytes(e, f[0]), *right = sp_bend_bytes(e, f[1]);
  int equal = left->size == right->size && (!left->size || !memcmp(left->data, right->data, left->size));
  Term pair = io_tup(e, sp_bend_bytes_term(e, left), sp_bend_bytes_term(e, right));
  return io_tup(e, pair, term_pak(equal ? CID_TRUE : CID_FALSE, 0));
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_DISCARD
static Term sp_bend_bytes_discard(Env e, Term *f, IoWork *w) {
  (void)w; sp_bend_bytes_free(sp_bend_bytes(e, f[0])); return term_pak(CID_UNIT, 0);
}
#endif
#ifdef CID_SCRAPANIUM_INTO_BYTES
static Term sp_bend_into_bytes(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]);
  SpBendBytes *b = io_mem(calloc(1, sizeof *b));
  b->data = sp_response_take_body(r, &b->size); sp_response_free(r);
  return sp_bend_bytes_term(e, b);
}
#endif
#ifdef CID_SCRAPANIUM_BYTES_READ_FILE
typedef struct { char *path; size_t limit; SpBendBytes *bytes; int error, saved_errno; } SpBytesRead;
static void sp_bend_bytes_read_call(IoWork *w) {
  SpBytesRead *job = (SpBytesRead *)w->data;
  int fd = open(job->path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
  if (fd < 0) { job->saved_errno = errno; job->error = SP_SINK; return; }
  struct stat st;
  if (fstat(fd, &st)) { job->error = SP_SINK; job->saved_errno = errno; }
  else if (!S_ISREG(st.st_mode)) job->error = SP_INVALID;
  else if (st.st_size > 0 && (uint64_t)st.st_size > job->limit) job->error = SP_INPUT_LIMIT;
  size_t cap = 0;
  unsigned char chunk[16384];
  while (!job->error) {
    size_t remaining = job->limit - job->bytes->size;
    size_t amount = remaining < sizeof chunk ? remaining + 1 : sizeof chunk;
    ssize_t n = read(fd, chunk, amount);
    if (n < 0 && errno == EINTR) continue;
    if (n < 0) { job->error = SP_SINK; job->saved_errno = errno; break; }
    if (!n) break;
    if ((size_t)n > remaining) { job->error = SP_INPUT_LIMIT; break; }
    size_t need = job->bytes->size + (size_t)n;
    if (need > cap) {
      size_t next = cap ? cap * 2 : sizeof chunk;
      if (next < need) next = need;
      if (next > job->limit) next = job->limit;
      unsigned char *data = realloc(job->bytes->data, next);
      if (!data) { job->error = SP_NOMEM; break; }
      job->bytes->data = data; cap = next;
    }
    memcpy(job->bytes->data + job->bytes->size, chunk, (size_t)n);
    job->bytes->size += (size_t)n;
  }
  if (close(fd) && !job->error) { job->error = SP_SINK; job->saved_errno = errno; }
}
static Term sp_bend_bytes_read_pack(Env e, IoWork *w) {
  SpBytesRead *job = (SpBytesRead *)w->data; Term result;
  if (job->error) {
    result = io_fail(e, job->error, job->saved_errno ? strerror(job->saved_errno) : sp_error_message(job->error));
    sp_bend_bytes_free(job->bytes);
  } else result = io_done(e, sp_bend_bytes_term(e, job->bytes));
  free(job->path); free(job); w->data = NULL; return result;
}
static Term sp_bend_bytes_read_file(Env e, Term *f, IoWork *w) {
  SpBytesRead *job = io_mem(calloc(1, sizeof *job)); int invalid = 0;
  job->path = sp_bend_string(e, f[0], &invalid, 1, NULL);
  job->limit = (u32)f[1]; job->bytes = io_mem(calloc(1, sizeof(SpBendBytes)));
  w->data = (char *)job;
  if (invalid) { job->error = SP_INVALID; return sp_bend_bytes_read_pack(e, w); }
  return io_work(w, sp_bend_bytes_read_call, sp_bend_bytes_read_pack);
}
#endif

#ifdef CID_SCRAPANIUM_OPEN
static Term sp_bend_open(Env e, Term *args, IoWork *w) {
  (void)w;
  /* Bend flattens nonrecursive records and enums inside constructors:
   * Config = 12 scalar fields + 10 Fingerprint fields + 2 Limits fields.
   * Bool is the unboxed arm index (False=0, True=1), not a constructor id. */
  Term f[24]; sp_bend_take(e, args[0], 24, f);
  int invalid = 0, error = 0;
  sp_config c; sp_config_default(&c);
  c.profile = sp_bend_string(e, f[0], &invalid, 1, NULL);
  c.proxy = sp_bend_string(e, f[1], &invalid, 1, NULL);
  c.ca_bundle = sp_bend_string(e, f[2], &invalid, 1, NULL);
  c.timeout_ms = f[3]; c.connect_timeout_ms = f[4]; c.max_redirects = f[5];
  c.concurrency = f[6]; c.max_body_bytes = f[7]; c.max_header_bytes = f[8];
  c.verify = f[9] != 0;
  c.follow_redirects = f[10] != 0;
  c.default_headers = f[11] != 0;
  Term *fp = f + 12;
  c.fingerprint.ciphers = sp_bend_string(e, fp[0], &invalid, 1, NULL);
  c.fingerprint.curves = sp_bend_string(e, fp[1], &invalid, 1, NULL);
  c.fingerprint.signature_algorithms = sp_bend_string(e, fp[2], &invalid, 1, NULL);
  c.fingerprint.extension_order = sp_bend_string(e, fp[3], &invalid, 1, NULL);
  c.fingerprint.http2_settings = sp_bend_string(e, fp[4], &invalid, 1, NULL);
  c.fingerprint.pseudo_header_order = sp_bend_string(e, fp[5], &invalid, 1, NULL);
  c.fingerprint.cert_compression = sp_bend_string(e, fp[6], &invalid, 1, NULL);
  if (fp[7] > 2 || fp[8] > 2) invalid = 1;
  c.fingerprint.grease = fp[7] <= 2 ? (int)fp[7] - 1 : -1;
  c.fingerprint.permute_extensions = fp[8] <= 2 ? (int)fp[8] - 1 : -1;
  c.fingerprint.http2_window_update = fp[9];
  c.max_batch_bytes = f[22]; c.max_batch_requests = f[23];
  sp_session *s = invalid ? NULL : sp_session_new(&c, &error);
  free((char *)c.profile); free((char *)c.proxy); free((char *)c.ca_bundle);
  free((char *)c.fingerprint.ciphers); free((char *)c.fingerprint.curves);
  free((char *)c.fingerprint.signature_algorithms); free((char *)c.fingerprint.extension_order);
  free((char *)c.fingerprint.http2_settings); free((char *)c.fingerprint.pseudo_header_order);
  free((char *)c.fingerprint.cert_compression);
  if (invalid) error = SP_INVALID;
  return s ? io_done(e, sp_bend_session_term(e, s)) : io_fail(e, error, sp_error_message(error));
}
#endif

#if defined(CID_SCRAPANIUM_CANCEL) || defined(CID_SCRAPANIUM_CANCEL_RELEASE) || defined(CID_SCRAPANIUM_SEND_CANCEL) || defined(CID_SCRAPANIUM_BATCH_CANCEL) || defined(CID_SCRAPANIUM_DOWNLOAD_CANCEL)
/* Tokens are copyable values; registry access stays on the event loop.
 * Workers take a reference before leaving it, so release cannot race them. */
static sp_cancel *sp_bend_cancel_read(Env e, Term token, Term *handle) {
  Term f[2]; sp_bend_take(e, token, 2, f);
  u32 slot = f[0];
  if (!slot || slot > sp_bend_count) return NULL;
  SpBendHandle *h = sp_bend_handles + slot - 1;
  if (!h->ptr || h->kind != 3 || h->generation != f[1]) return NULL;
  if (handle) *handle = io_hand(((u64)h->generation << 24) | slot);
  return h->ptr;
}
#endif
#ifdef CID_SCRAPANIUM_CANCEL_NEW
static Term sp_bend_cancel_new(Env e, Term *f, IoWork *w) {
  (void)f; (void)w;
  sp_cancel *c = sp_cancel_new();
  if (!c) return io_fail(e, SP_NOMEM, sp_error_message(SP_NOMEM));
  /* io_hand_v is a macro that evaluates its argument twice. */
  Term handle = sp_bend_handle_new(c, 3);
  u64 id = io_hand_v(handle);
  Term fields[] = {id & 0xffffff, id >> 24};
  return io_done(e, sp_bend_node(e, CID_CANCELTOKEN, 2, fields));
}
#endif
#ifdef CID_SCRAPANIUM_CANCEL
static Term sp_bend_cancel(Env e, Term *f, IoWork *w) {
  (void)w; sp_cancel *c = sp_bend_cancel_read(e, f[0], NULL);
  if (!c) return io_fail(e, SP_INVALID, "invalid or released cancellation token");
  sp_cancel_trigger(c); return io_done(e, term_pak(CID_UNIT, 0));
}
#endif
#ifdef CID_SCRAPANIUM_CANCEL_RELEASE
static Term sp_bend_cancel_release(Env e, Term *f, IoWork *w) {
  (void)w; Term handle;
  if (!sp_bend_cancel_read(e, f[0], &handle)) return io_fail(e, SP_INVALID, "invalid or released cancellation token");
  sp_cancel_free(sp_bend_handle_take(handle, 3));
  return io_done(e, term_pak(CID_UNIT, 0));
}
#endif

#ifdef SP_BEND_REQUESTS
typedef struct {
  sp_session *session;
  sp_request *requests;
  sp_response **responses;
  size_t count;
  int error, single;
  sp_cancel *cancel;
  char *path;
} SpBendJob;

static void sp_bend_request_read(Env e, Term request, sp_request *q) {
  int binary = term_aux(request) == CID_BINARYREQUEST;
  Term f[5]; sp_bend_take(e, request, binary ? 5 : 4, f);
  int invalid = 0;
  q->method = sp_bend_string(e, f[0], &invalid, 1, NULL);
  q->url = sp_bend_string(e, f[1], &invalid, 1, NULL);
  if (binary) {
    SpBendBytes *b = sp_bend_handle_take(f[3], 4);
    q->body = b->data; q->body_size = b->size; free(b);
  } else q->body = (unsigned char *)sp_bend_string(e, f[3], &invalid, 0, &q->body_size);
  /* Empty bodies must not turn GET into POST, nor affect HEAD semantics. */
  if (!q->body_size) { free((void *)q->body); q->body = NULL; }
  Term list = f[2]; size_t cap = 0;
  while (term_aux(list) == CID_CON) {
    Term item[2]; sp_bend_take(e, list, 2, item); list = item[1];
    if (q->header_count == cap) {
      cap = cap ? cap * 2 : 8;
      q->headers = io_mem(realloc((void *)q->headers, cap * sizeof(char *)));
    }
    ((const char **)q->headers)[q->header_count++] = sp_bend_string(e, item[0], &invalid, 1, NULL);
  }
  if (invalid) { free((char *)q->url); q->url = NULL; }
}
static void sp_bend_job_free(SpBendJob *job) {
  for (size_t i = 0; i < job->count; i++) {
    sp_request *q = job->requests + i;
    free((char *)q->method); free((char *)q->url); free((void *)q->body);
    for (size_t h = 0; h < q->header_count; h++) free((char *)q->headers[h]);
    free((void *)q->headers);
  }
  sp_cancel_free(job->cancel); free(job->path);
  free(job->requests); free(job->responses); free(job);
}
static void sp_bend_call(IoWork *w) {
  SpBendJob *job = (SpBendJob *)w->data;
  if (job->error) return;
  if (job->single == 2) job->responses[0] = sp_session_download(job->session, job->requests, job->path, job->cancel);
  else job->error = sp_session_batch_cancel(job->session, job->requests, job->count, job->responses, job->cancel);
}
#ifdef SP_BEND_DOWNLOAD
static Term sp_bend_download_result(Env e, sp_response *r) {
  Term out;
  if (sp_response_error(r)) out = io_fail(e, sp_response_error(r), sp_response_message(r));
  else {
    const char *url = sp_response_url(r);
    Term f[] = {(Term)sp_response_status(r), (Term)sp_response_downloaded(r),
      io_str(e, url, strlen(url)), io_str(e, sp_response_headers(r), sp_response_headers_size(r))};
    out = io_done(e, sp_bend_node(e, CID_DOWNLOADINFO, 4, f));
  }
  sp_response_free(r); return out;
}
#endif
static Term sp_bend_pack(Env e, IoWork *w) {
  SpBendJob *job = (SpBendJob *)w->data;
  Term out = term_pak(CID_NIL, 0);
  for (size_t n = job->count; n > 0; n--) {
    sp_response *r = job->responses[n - 1];
    Term item;
    if (!r) item = io_fail(e, job->error ? job->error : SP_NOMEM, sp_error_message(job->error ? job->error : SP_NOMEM));
    else {
#ifdef SP_BEND_DOWNLOAD
      if (job->single == 2) item = sp_bend_download_result(e, r);
      else
#endif
      {
#ifdef SP_BEND_BUFFERED
        item = sp_bend_result(e, r);
#else
        item = io_fail(e, SP_BACKEND, sp_error_message(SP_BACKEND));
        sp_response_free(r);
#endif
      }
    }
    out = job->single ? item : io_node(e, CID_CON, item, out);
  }
  Term result = io_tup(e, sp_bend_session_term(e, job->session), out);
  sp_bend_job_free(job); w->data = NULL; return result;
}
#if defined(CID_SCRAPANIUM_SEND) || defined(CID_SCRAPANIUM_SEND_CANCEL) || defined(SP_BEND_DOWNLOAD)
static Term sp_bend_send_start(Env e, Term *f, IoWork *w, int mode, int cancellable) {
  SpBendJob *job = io_mem(calloc(1, sizeof *job));
  job->session = sp_bend_session(e, f[0]); job->count = 1; job->single = mode;
  job->requests = io_mem(calloc(1, sizeof(sp_request)));
  job->responses = io_mem(calloc(1, sizeof(sp_response *)));
  sp_bend_request_read(e, f[1], job->requests);
  if (mode == 2) {
    int invalid = 0;
    job->path = sp_bend_string(e, f[2], &invalid, 1, NULL);
    if (invalid) job->error = SP_INVALID;
  }
#if defined(CID_SCRAPANIUM_SEND_CANCEL) || defined(CID_SCRAPANIUM_DOWNLOAD_CANCEL)
  if (cancellable) {
    job->cancel = sp_bend_cancel_read(e, f[mode == 2 ? 3 : 2], NULL);
    if (!job->cancel) job->error = SP_INVALID;
    else sp_cancel_retain(job->cancel);
  }
#else
  (void)cancellable;
#endif
  w->data = (char *)job;
  return io_work(w, sp_bend_call, sp_bend_pack);
}
#endif
#ifdef CID_SCRAPANIUM_SEND
static Term sp_bend_send(Env e, Term *f, IoWork *w) { return sp_bend_send_start(e, f, w, 1, 0); }
#endif
#ifdef CID_SCRAPANIUM_SEND_CANCEL
static Term sp_bend_send_cancel(Env e, Term *f, IoWork *w) { return sp_bend_send_start(e, f, w, 1, 1); }
#endif
#ifdef CID_SCRAPANIUM_DOWNLOAD
static Term sp_bend_download(Env e, Term *f, IoWork *w) { return sp_bend_send_start(e, f, w, 2, 0); }
#endif
#ifdef CID_SCRAPANIUM_DOWNLOAD_CANCEL
static Term sp_bend_download_cancel(Env e, Term *f, IoWork *w) { return sp_bend_send_start(e, f, w, 2, 1); }
#endif
#if defined(CID_SCRAPANIUM_BATCH) || defined(CID_SCRAPANIUM_BATCH_CANCEL)
static Term sp_bend_batch_start(Env e, Term *f, IoWork *w, int cancellable) {
  SpBendJob *job = io_mem(calloc(1, sizeof *job));
  job->session = sp_bend_session(e, f[0]);
  Term list = f[1]; size_t cap = 0;
  while (term_aux(list) == CID_CON) {
    Term item[2]; sp_bend_take(e, list, 2, item); list = item[1];
    if (job->count == cap) {
      cap = cap ? cap * 2 : 16;
      job->requests = io_mem(realloc(job->requests, cap * sizeof(sp_request)));
    }
    sp_request *q = job->requests + job->count++;
    memset(q, 0, sizeof *q); sp_bend_request_read(e, item[0], q);
  }
  job->responses = io_mem(calloc(job->count ? job->count : 1, sizeof(sp_response *)));
#ifdef CID_SCRAPANIUM_BATCH_CANCEL
  if (cancellable) {
    job->cancel = sp_bend_cancel_read(e, f[2], NULL);
    if (!job->cancel) job->error = SP_INVALID;
    else sp_cancel_retain(job->cancel);
  }
#else
  (void)cancellable;
#endif
  w->data = (char *)job;
  return io_work(w, sp_bend_call, sp_bend_pack);
}
#endif
#ifdef CID_SCRAPANIUM_BATCH
static Term sp_bend_batch(Env e, Term *f, IoWork *w) { return sp_bend_batch_start(e, f, w, 0); }
#endif
#ifdef CID_SCRAPANIUM_BATCH_CANCEL
static Term sp_bend_batch_cancel(Env e, Term *f, IoWork *w) { return sp_bend_batch_start(e, f, w, 1); }
#endif
#endif /* send or batch helpers */
#ifdef CID_SCRAPANIUM_CLOSE
static Term sp_bend_close(Env e, Term *f, IoWork *w) {
  (void)w; sp_session_free(sp_bend_session(e, f[0])); return term_pak(CID_UNIT, 0);
}
#endif
#ifdef CID_SCRAPANIUM_DISCARD
static Term sp_bend_discard(Env e, Term *f, IoWork *w) {
  (void)w; sp_response_free(sp_bend_response(e, f[0])); return term_pak(CID_UNIT, 0);
}
#endif
#ifdef CID_SCRAPANIUM_TEXT
static Term sp_bend_text(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]);
  return io_tup(e, sp_bend_response_term(e, r), io_str(e, (const char *)sp_response_body(r), sp_response_size(r)));
}
#endif
#ifdef CID_SCRAPANIUM_HEADERS
static Term sp_bend_headers(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]);
  return io_tup(e, sp_bend_response_term(e, r), io_str(e, sp_response_headers(r), sp_response_headers_size(r)));
}
#endif
#ifdef CID_SCRAPANIUM_HEADER_PAIRS
typedef struct { const char *name, *value; size_t name_size, value_size; } SpHeaderSpan;
static int sp_bend_status_line(const char *line, size_t n) {
  if (n < 8 || memcmp(line, "HTTP/", 5)) return 0;
  const char *space = memchr(line, ' ', n);
  if (!space || (size_t)(line + n - space) < 4) return 0;
  size_t version = (size_t)(space - line);
  if (!((version == 8 && line[5] == '1' && line[6] == '.' && (line[7] == '0' || line[7] == '1')) ||
        (version == 6 && (line[5] == '2' || line[5] == '3')))) return 0;
  if (space + 4 < line + n && space[4] != ' ' && space[4] != '\r' && space[4] != '\n') return 0;
  return space[1] >= '1' && space[1] <= '9' && space[2] >= '0' && space[2] <= '9' && space[3] >= '0' && space[3] <= '9';
}
static Term sp_bend_header_pairs(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]);
  int trailers = term_aux(f[1]) == CID_TRUE;
  const char *raw = sp_response_headers(r), *end = raw + sp_response_headers_size(r);
  const char *start = raw;
  /* Keep only the final status block. Earlier redirects/1xx/proxy CONNECT
   * remain available in raw headers. The pinned curl unfolds legacy fields. */
  for (const char *p = raw; p < end;) {
    const char *lf = memchr(p, '\n', (size_t)(end - p));
    const char *next = lf ? lf + 1 : end;
    if (sp_bend_status_line(p, (size_t)(next - p))) start = next;
    p = next;
  }
  SpHeaderSpan *spans = NULL; size_t count = 0, cap = 0; int in_trailers = 0;
  for (const char *p = start; p < end;) {
    const char *lf = memchr(p, '\n', (size_t)(end - p));
    const char *next = lf ? lf + 1 : end, *last = lf ? lf : end;
    if (last > p && last[-1] == '\r') last--;
    if (last == p) { in_trailers = 1; p = next; continue; }
    const char *colon = memchr(p, ':', (size_t)(last - p));
    if (in_trailers == trailers && colon && colon > p) {
      const char *value = colon + 1;
      while (value < last && (*value == ' ' || *value == '\t')) value++;
      while (last > value && (last[-1] == ' ' || last[-1] == '\t')) last--;
      if (count == cap) {
        cap = cap ? cap * 2 : 16;
        spans = io_mem(realloc(spans, cap * sizeof *spans));
      }
      spans[count++] = (SpHeaderSpan){p, value, (size_t)(colon - p), (size_t)(last - value)};
    }
    p = next;
  }
  Term list = term_pak(CID_NIL, 0);
  while (count) {
    SpHeaderSpan h = spans[--count];
    Term pair = io_tup(e, io_str(e, h.name, h.name_size), io_str(e, h.value, h.value_size));
    list = io_node(e, CID_CON, pair, list);
  }
  free(spans);
  return io_tup(e, sp_bend_response_term(e, r), list);
}
#endif
#ifdef CID_SCRAPANIUM_EFFECTIVE_URL
static Term sp_bend_effective_url(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]); const char *url = sp_response_url(r);
  return io_tup(e, sp_bend_response_term(e, r), io_str(e, url, strlen(url)));
}
#endif
#ifdef CID_SCRAPANIUM_BYTE_AT
static Term sp_bend_byte_at(Env e, Term *f, IoWork *w) {
  (void)w; sp_response *r = sp_bend_response(e, f[0]);
  Term byte = f[1] < sp_response_size(r) ? io_box(e, CID_SOME, sp_response_body(r)[f[1]]) : term_pak(CID_NONE, 0);
  return io_tup(e, sp_bend_response_term(e, r), byte);
}
#endif
#ifdef CID_SCRAPANIUM_SAVE
typedef struct { sp_response *response; char *path; int code; } SpSave;
static void sp_bend_save_call(IoWork *w) {
  SpSave *job = (SpSave *)w->data;
  FILE *file = fopen(job->path, "wb");
  if (!file) { job->code = errno; return; }
  size_t n = sp_response_size(job->response);
  if (fwrite(sp_response_body(job->response), 1, n, file) != n) job->code = errno ? errno : EIO;
  if (fclose(file) && !job->code) job->code = errno;
}
static Term sp_bend_save_pack(Env e, IoWork *w) {
  SpSave *job = (SpSave *)w->data;
  Term result = job->code ? io_fail(e, job->code, NULL) : io_done(e, term_pak(CID_UNIT, 0));
  Term out = io_tup(e, sp_bend_response_term(e, job->response), result);
  free(job->path); free(job); return out;
}
static Term sp_bend_save(Env e, Term *f, IoWork *w) {
  SpSave *job = io_mem(calloc(1, sizeof *job)); int invalid = 0;
  job->response = sp_bend_response(e, f[0]);
  job->path = sp_bend_string(e, f[1], &invalid, 1, NULL); w->data = (char *)job;
  if (invalid) { job->code = EINVAL; return sp_bend_save_pack(e, w); }
  return io_work(w, sp_bend_save_call, sp_bend_save_pack);
}
#endif
#ifdef CID_SCRAPANIUM_BACKEND
static Term sp_bend_backend(Env e, Term *f, IoWork *w) {
  (void)f; (void)w; const char *s = sp_backend_version(); return io_str(e, s, strlen(s));
}
#endif

#ifdef CID_SCRAPANIUM_PROFILES
static Term sp_bend_profiles(Env e, Term *f, IoWork *w) {
  (void)f; (void)w; Term list = term_pak(CID_NIL, 0);
  for (size_t i = sp_profile_count(); i > 0; i--) {
    const char *name = sp_profile_name(i - 1);
    Term fields[] = {io_str(e, name, strlen(name)), list};
    list = sp_bend_node(e, CID_CON, 2, fields);
  }
  return list;
}
#endif

#include "bend_websocket.inc.c"

static void __attribute__((constructor)) sp_bend_register(void) {
  atexit(sp_bend_cleanup);
#ifdef CID_SCRAPANIUM_PROFILES
  io_eff(CID_SCRAPANIUM_PROFILES, sp_bend_profiles, 0);
#endif
#ifdef CID_SCRAPANIUM_WS_UPGRADE
  io_eff(CID_SCRAPANIUM_WS_UPGRADE, sp_bend_ws_upgrade, 0);
#endif
#ifdef CID_SCRAPANIUM_WS_SEND
  io_eff(CID_SCRAPANIUM_WS_SEND, sp_bend_ws_send, 0);
#endif
#ifdef CID_SCRAPANIUM_WS_RECEIVE
  io_eff(CID_SCRAPANIUM_WS_RECEIVE, sp_bend_ws_receive, 0);
#endif
#ifdef CID_SCRAPANIUM_WS_CLOSE
  io_eff(CID_SCRAPANIUM_WS_CLOSE, sp_bend_ws_close, 0);
#endif
#ifdef CID_SCRAPANIUM_WS_DISCARD
  io_eff(CID_SCRAPANIUM_WS_DISCARD, sp_bend_ws_discard, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_FROM_LIST
  io_eff(CID_SCRAPANIUM_BYTES_FROM_LIST, sp_bend_bytes_from_list, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_FROM_TEXT
  io_eff(CID_SCRAPANIUM_BYTES_FROM_TEXT, sp_bend_bytes_from_text, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_READ_FILE
  io_eff(CID_SCRAPANIUM_BYTES_READ_FILE, sp_bend_bytes_read_file, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_AT
  io_eff(CID_SCRAPANIUM_BYTES_AT, sp_bend_bytes_at, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_TEXT
  io_eff(CID_SCRAPANIUM_BYTES_TEXT, sp_bend_bytes_text, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_EQUAL
  io_eff(CID_SCRAPANIUM_BYTES_EQUAL, sp_bend_bytes_equal, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTES_DISCARD
  io_eff(CID_SCRAPANIUM_BYTES_DISCARD, sp_bend_bytes_discard, 0);
#endif
#ifdef CID_SCRAPANIUM_INTO_BYTES
  io_eff(CID_SCRAPANIUM_INTO_BYTES, sp_bend_into_bytes, 0);
#endif
#ifdef CID_SCRAPANIUM_OPEN
  io_eff(CID_SCRAPANIUM_OPEN, sp_bend_open, 0);
#endif
#ifdef CID_SCRAPANIUM_CLOSE
  io_eff(CID_SCRAPANIUM_CLOSE, sp_bend_close, 0);
#endif
#ifdef CID_SCRAPANIUM_SEND
  io_eff(CID_SCRAPANIUM_SEND, sp_bend_send, 0);
#endif
#ifdef CID_SCRAPANIUM_BATCH
  io_eff(CID_SCRAPANIUM_BATCH, sp_bend_batch, 0);
#endif
#ifdef CID_SCRAPANIUM_CANCEL_NEW
  io_eff(CID_SCRAPANIUM_CANCEL_NEW, sp_bend_cancel_new, 0);
#endif
#ifdef CID_SCRAPANIUM_CANCEL
  io_eff(CID_SCRAPANIUM_CANCEL, sp_bend_cancel, 0);
#endif
#ifdef CID_SCRAPANIUM_CANCEL_RELEASE
  io_eff(CID_SCRAPANIUM_CANCEL_RELEASE, sp_bend_cancel_release, 0);
#endif
#ifdef CID_SCRAPANIUM_SEND_CANCEL
  io_eff(CID_SCRAPANIUM_SEND_CANCEL, sp_bend_send_cancel, 0);
#endif
#ifdef CID_SCRAPANIUM_BATCH_CANCEL
  io_eff(CID_SCRAPANIUM_BATCH_CANCEL, sp_bend_batch_cancel, 0);
#endif
#ifdef CID_SCRAPANIUM_DOWNLOAD
  io_eff(CID_SCRAPANIUM_DOWNLOAD, sp_bend_download, 0);
#endif
#ifdef CID_SCRAPANIUM_DOWNLOAD_CANCEL
  io_eff(CID_SCRAPANIUM_DOWNLOAD_CANCEL, sp_bend_download_cancel, 0);
#endif
#ifdef CID_SCRAPANIUM_TEXT
  io_eff(CID_SCRAPANIUM_TEXT, sp_bend_text, 0);
#endif
#ifdef CID_SCRAPANIUM_HEADERS
  io_eff(CID_SCRAPANIUM_HEADERS, sp_bend_headers, 0);
#endif
#ifdef CID_SCRAPANIUM_HEADER_PAIRS
  io_eff(CID_SCRAPANIUM_HEADER_PAIRS, sp_bend_header_pairs, 0);
#endif
#ifdef CID_SCRAPANIUM_EFFECTIVE_URL
  io_eff(CID_SCRAPANIUM_EFFECTIVE_URL, sp_bend_effective_url, 0);
#endif
#ifdef CID_SCRAPANIUM_BYTE_AT
  io_eff(CID_SCRAPANIUM_BYTE_AT, sp_bend_byte_at, 0);
#endif
#ifdef CID_SCRAPANIUM_SAVE
  io_eff(CID_SCRAPANIUM_SAVE, sp_bend_save, 0);
#endif
#ifdef CID_SCRAPANIUM_DISCARD
  io_eff(CID_SCRAPANIUM_DISCARD, sp_bend_discard, 0);
#endif
#ifdef CID_SCRAPANIUM_BACKEND
  io_eff(CID_SCRAPANIUM_BACKEND, sp_bend_backend, 0);
#endif
}
