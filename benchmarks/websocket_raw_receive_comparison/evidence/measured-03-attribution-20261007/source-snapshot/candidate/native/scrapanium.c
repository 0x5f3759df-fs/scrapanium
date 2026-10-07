#define _POSIX_C_SOURCE 200809L
#include "scrapanium.h"
#include <curl/curl.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <limits.h>
#include <stdio.h>
#include <stdatomic.h>
#include <errno.h>
#include <unistd.h>

typedef struct { size_t used, limit; } sp_budget;
typedef struct {
  unsigned char *data;
  size_t size, cap, limit;
  int error, limit_error;
  sp_budget *budget;
} sp_buffer;
typedef struct sp_cancel_link { CURLM *multi; struct sp_cancel_link *next; } sp_cancel_link;
struct sp_cancel {
  atomic_uint refs;
  atomic_int triggered;
  pthread_mutex_t gate;
  sp_cancel_link *links;
};
struct sp_response {
  int error, curl_code;
  long status, http_version, new_connections;
  uint64_t elapsed_us;
  char message[CURL_ERROR_SIZE];
  char *url;
  sp_buffer body, headers;
  sp_sink_fn sink;
  void *sink_user;
  sp_cancel *cancel;
  uint64_t downloaded;
};
typedef struct {
  CURL *easy;
  struct curl_slist *headers;
  sp_response *response;
  int active;
} sp_slot;
struct sp_session {
  sp_config config;
  CURLM *multi;
  CURLSH *share;
  sp_slot *slots;
  pthread_mutex_t gate;
};
static pthread_once_t sp_once = PTHREAD_ONCE_INIT;
static CURLcode sp_init_code;
static void sp_init(void) { sp_init_code = curl_global_init(CURL_GLOBAL_DEFAULT); }

sp_cancel *sp_cancel_new(void) {
  sp_cancel *c = calloc(1, sizeof *c);
  if (!c) return NULL;
  atomic_init(&c->refs, 1); atomic_init(&c->triggered, 0);
  if (pthread_mutex_init(&c->gate, NULL)) { free(c); return NULL; }
  return c;
}
void sp_cancel_retain(sp_cancel *c) { if (c) atomic_fetch_add_explicit(&c->refs, 1, memory_order_relaxed); }
void sp_cancel_free(sp_cancel *c) {
  if (c && atomic_fetch_sub_explicit(&c->refs, 1, memory_order_acq_rel) == 1) {
    pthread_mutex_destroy(&c->gate); free(c);
  }
}
int sp_cancel_is_triggered(const sp_cancel *c) {
  return c && atomic_load_explicit(&c->triggered, memory_order_acquire);
}
void sp_cancel_trigger(sp_cancel *c) {
  if (!c) return;
  atomic_store_explicit(&c->triggered, 1, memory_order_release);
  pthread_mutex_lock(&c->gate);
  for (sp_cancel_link *link = c->links; link; link = link->next) curl_multi_wakeup(link->multi);
  pthread_mutex_unlock(&c->gate);
}
static void sp_cancel_attach(sp_cancel *c, sp_cancel_link *link, CURLM *multi) {
  if (!c) return;
  sp_cancel_retain(c);
  pthread_mutex_lock(&c->gate);
  *link = (sp_cancel_link){.multi = multi, .next = c->links}; c->links = link;
  pthread_mutex_unlock(&c->gate);
}
static void sp_cancel_detach(sp_cancel *c, sp_cancel_link *link) {
  if (!c) return;
  pthread_mutex_lock(&c->gate);
  sp_cancel_link **at = &c->links;
  while (*at != link) at = &(*at)->next;
  *at = link->next;
  pthread_mutex_unlock(&c->gate);
  sp_cancel_free(c);
}
static int sp_progress(void *user, curl_off_t total, curl_off_t now, curl_off_t up_total, curl_off_t up_now) {
  (void)total; (void)now; (void)up_total; (void)up_now;
  return sp_cancel_is_triggered(user);
}

#define SP_FP_STRINGS(X) X(ciphers) X(curves) X(signature_algorithms) X(extension_order) \
  X(http2_settings) X(pseudo_header_order) X(cert_compression)

#include "profiles.inc.c"

static CURLcode sp_apply_profile(CURL *easy, const sp_config *c) {
  CURLcode code = sp_profile_start(easy, c);
  if (code) return code;
#define FP_STRING(field, option) do { if (c->fingerprint.field && *c->fingerprint.field) { \
  code = curl_easy_setopt(easy, option, c->fingerprint.field); if (code) return code; } } while (0)
  FP_STRING(ciphers, CURLOPT_SSL_CIPHER_LIST);
  FP_STRING(curves, CURLOPT_SSL_EC_CURVES);
  FP_STRING(signature_algorithms, CURLOPT_SSL_SIG_HASH_ALGS);
  FP_STRING(extension_order, CURLOPT_TLS_EXTENSION_ORDER);
  FP_STRING(http2_settings, CURLOPT_HTTP2_SETTINGS);
  FP_STRING(pseudo_header_order, CURLOPT_HTTP2_PSEUDO_HEADERS_ORDER);
  FP_STRING(cert_compression, CURLOPT_SSL_CERT_COMPRESSION);
#undef FP_STRING
  if (c->fingerprint.grease >= 0) {
    code = curl_easy_setopt(easy, CURLOPT_TLS_GREASE, (long)c->fingerprint.grease);
    if (code) return code;
  }
  if (c->fingerprint.permute_extensions >= 0) {
    code = curl_easy_setopt(easy, CURLOPT_SSL_PERMUTE_EXTENSIONS, (long)c->fingerprint.permute_extensions);
    if (code) return code;
  }
  if (c->fingerprint.http2_window_update) {
    code = curl_easy_setopt(easy, CURLOPT_HTTP2_WINDOW_UPDATE, (long)c->fingerprint.http2_window_update);
    if (code) return code;
  }
  return CURLE_OK;
}

const char *sp_error_message(int e) {
  static const char *messages[] = { "success", "invalid request or configuration",
    "out of memory", "unsupported browser profile", "transport failed",
    "response body limit exceeded", "response header limit exceeded",
    "session is busy", "backend failure", "aggregate response memory or batch count limit exceeded",
    "operation cancelled", "stream sink or destination failed", "download requires HTTP 2xx",
    "input byte limit exceeded", "operation timed out", "invalid WebSocket protocol data",
    "WebSocket closed" };
  return e >= 0 && e <= SP_CLOSED ? messages[e] : "unknown error";
}
const char *sp_backend_version(void) { return curl_version(); }
void sp_config_default(sp_config *c) {
  *c = (sp_config){ .profile = "chrome150", .timeout_ms = 30000,
    .connect_timeout_ms = 10000, .max_redirects = 10, .concurrency = 16,
    .max_body_bytes = 64u * 1024u * 1024u, .max_header_bytes = 256u * 1024u,
    .verify = 1, .follow_redirects = 1, .default_headers = 1,
    .fingerprint = {.grease = -1, .permute_extensions = -1},
    .max_batch_bytes = 128u * 1024u * 1024u, .max_batch_requests = 4096 };
}
static size_t sp_write(char *ptr, size_t size, size_t count, void *user) {
  sp_buffer *b = user;
  if (size && count > SIZE_MAX / size) { b->error = SP_NOMEM; return 0; }
  size_t n = size * count;
  if (n > b->limit - b->size) { b->error = b->limit_error; return 0; }
  size_t need = b->size + n + 1;
  if (need > b->cap) {
    size_t cap = b->cap ? b->cap : 4096;
    while (cap < need) {
      if (cap > SIZE_MAX / 2) { cap = need; break; }
      cap *= 2;
    }
    if (cap > b->limit + 1) cap = b->limit + 1;
    if (b->budget) {
      size_t remaining = b->budget->limit - b->budget->used;
      if (need - b->cap > remaining) { b->error = SP_BATCH_LIMIT; return 0; }
      if (cap - b->cap > remaining) cap = b->cap + remaining;
    }
    unsigned char *data = realloc(b->data, cap);
    if (!data) { b->error = SP_NOMEM; return 0; }
    if (b->budget) b->budget->used += cap - b->cap;
    b->data = data; b->cap = cap;
  }
  if (n) memcpy(b->data + b->size, ptr, n);
  b->size += n; b->data[b->size] = 0;
  return n;
}
static size_t sp_body_write(char *ptr, size_t size, size_t count, void *user) {
  sp_response *r = user;
  if (size && count > SIZE_MAX / size) { r->body.error = SP_NOMEM; return 0; }
  size_t n = size * count;
  if (sp_cancel_is_triggered(r->cancel)) { r->body.error = SP_CANCELLED; return 0; }
  if (n > r->body.limit - r->downloaded) { r->body.error = SP_BODY_LIMIT; return 0; }
  if (r->sink) {
    if (r->sink((const unsigned char *)ptr, n, r->sink_user)) { r->body.error = SP_SINK; return 0; }
  } else if (sp_write(ptr, size, count, &r->body) != n) return 0;
  r->downloaded += n;
  return n;
}
static int sp_token(const char *s) {
  if (!s || !*s) return 0;
  for (; *s; s++) {
    unsigned char c = (unsigned char)*s;
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || strchr("!#$%&'*+-.^_`|~", c))) return 0;
  }
  return 1;
}
static int sp_header_valid(const char *s) {
  if (!s || !*s || *s == ':') return 0;
  const char *colon = strchr(s, ':');
  if (!colon) {
    /* libcurl uses Name; to send an empty field; Name: suppresses a default. */
    colon = strchr(s, ';');
    if (!colon || colon == s || colon[1]) return 0;
  }
  for (const char *p = s; p < colon; p++) {
    unsigned char c = (unsigned char)*p;
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || strchr("!#$%&'*+-.^_`|~", c))) return 0;
  }
  for (const char *p = colon + 1; *p; p++)
    if ((unsigned char)*p < 32 && *p != '\t') return 0;
    else if ((unsigned char)*p == 127) return 0;
  return 1;
}
static void sp_set_error(sp_response *r, int error, CURLcode code) {
  r->error = error; r->curl_code = (int)code;
  if (!r->message[0] || error != SP_TRANSPORT) snprintf(r->message, sizeof r->message, "%s",
    error == SP_TRANSPORT ? curl_easy_strerror(code) : sp_error_message(error));
}

sp_session *sp_session_new(const sp_config *c, int *error) {
  int err = SP_INVALID;
  sp_session *s = NULL;
  if (!c || !c->profile || !*c->profile || !c->concurrency || c->concurrency > 256 ||
      !c->timeout_ms || !c->connect_timeout_ms || !c->max_body_bytes || c->max_batch_bytes < sizeof(sp_response) || !c->max_batch_requests ||
      !c->max_header_bytes || c->max_body_bytes == SIZE_MAX ||
      c->max_header_bytes == SIZE_MAX || c->fingerprint.grease < -1 ||
      c->fingerprint.grease > 1 || c->fingerprint.permute_extensions < -1 ||
      c->fingerprint.permute_extensions > 1 || c->fingerprint.http2_window_update > INT32_MAX) goto done;
  pthread_once(&sp_once, sp_init);
  if (sp_init_code != CURLE_OK) { err = SP_BACKEND; goto done; }
  s = calloc(1, sizeof *s);
  if (!s) { err = SP_NOMEM; goto done; }
  s->config = *c;
  s->config.profile = strdup(c->profile);
  s->config.proxy = c->proxy ? strdup(c->proxy) : NULL;
  s->config.ca_bundle = c->ca_bundle ? strdup(c->ca_bundle) : NULL;
#define FP_COPY(field) s->config.fingerprint.field = c->fingerprint.field ? strdup(c->fingerprint.field) : NULL;
  SP_FP_STRINGS(FP_COPY)
#undef FP_COPY
  pthread_mutex_init(&s->gate, NULL);
  s->slots = calloc(c->concurrency, sizeof *s->slots);
  if (!s->config.profile || (c->proxy && !s->config.proxy) ||
      (c->ca_bundle && !s->config.ca_bundle) || !s->slots) { err = SP_NOMEM; goto fail; }
#define FP_CHECK(field) if (c->fingerprint.field && !s->config.fingerprint.field) { err = SP_NOMEM; goto fail; }
  SP_FP_STRINGS(FP_CHECK)
#undef FP_CHECK
  s->multi = curl_multi_init(); s->share = curl_share_init();
  if (!s->multi || !s->share) { err = SP_NOMEM; goto fail; }
  if (curl_share_setopt(s->share, CURLSHOPT_SHARE, CURL_LOCK_DATA_COOKIE) != CURLSHE_OK ||
      curl_share_setopt(s->share, CURLSHOPT_SHARE, CURL_LOCK_DATA_DNS) != CURLSHE_OK ||
      curl_share_setopt(s->share, CURLSHOPT_SHARE, CURL_LOCK_DATA_SSL_SESSION) != CURLSHE_OK ||
      curl_multi_setopt(s->multi, CURLMOPT_MAX_TOTAL_CONNECTIONS, (long)c->concurrency) != CURLM_OK) {
    err = SP_BACKEND; goto fail;
  }
  for (uint32_t i = 0; i < c->concurrency; i++) {
    s->slots[i].easy = curl_easy_init();
    if (!s->slots[i].easy) { err = SP_NOMEM; goto fail; }
    if (sp_apply_profile(s->slots[i].easy, c) != CURLE_OK) {
      err = SP_PROFILE; goto fail;
    }
  }
  err = SP_OK; goto done;
fail:
  sp_session_free(s); s = NULL;
done:
  if (error) *error = err;
  return s;
}
void sp_session_free(sp_session *s) {
  if (!s) return;
  if (s->slots) for (uint32_t i = 0; i < s->config.concurrency; i++) {
    sp_slot *slot = s->slots + i;
    if (slot->active) curl_multi_remove_handle(s->multi, slot->easy);
    curl_easy_cleanup(slot->easy); curl_slist_free_all(slot->headers);
  }
  curl_multi_cleanup(s->multi); curl_share_cleanup(s->share);
  free(s->slots); free((char *)s->config.profile); free((char *)s->config.proxy);
#define FP_FREE(field) free((char *)s->config.fingerprint.field);
  SP_FP_STRINGS(FP_FREE)
#undef FP_FREE
  free((char *)s->config.ca_bundle); pthread_mutex_destroy(&s->gate); free(s);
}

static int sp_prepare(sp_session *s, sp_slot *slot, const sp_request *q, sp_response *r, int websocket) {
  CURLcode code = CURLE_OK;
  slot->response = r;
  r->body.limit = s->config.max_body_bytes;
  r->headers.limit = s->config.max_header_bytes;
  r->body.limit_error = SP_BODY_LIMIT; r->headers.limit_error = SP_HEADER_LIMIT;
  if (!q->url || (websocket ? (strncasecmp(q->url, "wss://", 6) && strncasecmp(q->url, "ws://", 5)) :
      (strncasecmp(q->url, "https://", 8) && strncasecmp(q->url, "http://", 7))) ||
      !sp_token(q->method) || (q->body_size && !q->body) ||
      q->body_size > INT64_MAX || (q->header_count && !q->headers) ||
      (!strcmp(q->method, "HEAD") && q->body_size)) {
    sp_set_error(r, SP_INVALID, CURLE_OK); return SP_INVALID;
  }
  curl_easy_reset(slot->easy);
  curl_slist_free_all(slot->headers); slot->headers = NULL;
  code = sp_apply_profile(slot->easy, &s->config);
  if (code != CURLE_OK) { sp_set_error(r, SP_PROFILE, code); return SP_PROFILE; }
  for (size_t i = 0; i < q->header_count; i++) {
    if (!sp_header_valid(q->headers[i])) { sp_set_error(r, SP_INVALID, CURLE_OK); return SP_INVALID; }
    struct curl_slist *headers = curl_slist_append(slot->headers, q->headers[i]);
    if (!headers) { sp_set_error(r, SP_NOMEM, CURLE_OK); return SP_NOMEM; }
    slot->headers = headers;
  }
#define SP_OPT(key, value) do { code = curl_easy_setopt(slot->easy, key, value); if (code) goto option_fail; } while (0)
  SP_OPT(CURLOPT_URL, q->url);
  SP_OPT(CURLOPT_ERRORBUFFER, r->message);
  SP_OPT(CURLOPT_NOSIGNAL, 1L);
  SP_OPT(CURLOPT_PROTOCOLS_STR, websocket ? "ws,wss" : "http,https");
  SP_OPT(CURLOPT_REDIR_PROTOCOLS_STR, websocket ? "ws,wss" : "http,https");
  SP_OPT(CURLOPT_FOLLOWLOCATION, !websocket && s->config.follow_redirects ? (long)CURLFOLLOW_OBEYCODE : 0L);
  if (websocket) {
    SP_OPT(CURLOPT_CONNECT_ONLY, 2L);
    SP_OPT(CURLOPT_HTTP_VERSION, (long)CURL_HTTP_VERSION_1_1);
    SP_OPT(CURLOPT_WS_OPTIONS, (long)CURLWS_NOAUTOPONG);
  }
  SP_OPT(CURLOPT_MAXREDIRS, (long)s->config.max_redirects);
  /* POSTFIELDS also powers custom-method bodies. On 301/302 only an actual
   * POST should change to GET; keep PUT/PATCH/DELETE bodies and methods. A 303
   * still changes them to GET, and 307/308 always preserve them. */
  if (strcmp(q->method, "POST")) SP_OPT(CURLOPT_POSTREDIR, (long)(CURL_REDIR_POST_301 | CURL_REDIR_POST_302));
  SP_OPT(CURLOPT_TIMEOUT_MS, (long)s->config.timeout_ms);
  SP_OPT(CURLOPT_CONNECTTIMEOUT_MS, (long)s->config.connect_timeout_ms);
  SP_OPT(CURLOPT_SSL_VERIFYPEER, (long)!!s->config.verify);
  SP_OPT(CURLOPT_SSL_VERIFYHOST, s->config.verify ? 2L : 0L);
  if (s->config.ca_bundle && *s->config.ca_bundle) SP_OPT(CURLOPT_CAINFO, s->config.ca_bundle);
  /* Explicitly ignore ambient proxy variables. Proxy use is session configuration. */
  SP_OPT(CURLOPT_PROXY, s->config.proxy ? s->config.proxy : "");
  SP_OPT(CURLOPT_NOPROXY, "");
  SP_OPT(CURLOPT_SHARE, s->share);
  SP_OPT(CURLOPT_COOKIEFILE, "");
  SP_OPT(CURLOPT_ACCEPT_ENCODING, "");
  SP_OPT(CURLOPT_WRITEFUNCTION, sp_body_write);
  SP_OPT(CURLOPT_WRITEDATA, r);
  if (r->cancel) {
    SP_OPT(CURLOPT_NOPROGRESS, 0L);
    SP_OPT(CURLOPT_XFERINFOFUNCTION, sp_progress);
    SP_OPT(CURLOPT_XFERINFODATA, r->cancel);
  }
  SP_OPT(CURLOPT_HEADERFUNCTION, sp_write);
  SP_OPT(CURLOPT_HEADERDATA, &r->headers);
  SP_OPT(CURLOPT_PRIVATE, slot);
  SP_OPT(CURLOPT_HTTPHEADER, slot->headers);
  if (!strcmp(q->method, "HEAD")) SP_OPT(CURLOPT_NOBODY, 1L);
  if (strcmp(q->method, "HEAD") && (q->body || !strcmp(q->method, "POST") || !strcmp(q->method, "PUT") || !strcmp(q->method, "PATCH"))) {
    SP_OPT(CURLOPT_POSTFIELDSIZE_LARGE, (curl_off_t)q->body_size);
    SP_OPT(CURLOPT_POSTFIELDS, q->body ? (const char *)q->body : "");
  }
  /* For GET/POST, libcurl preserves its browser-compatible redirect method rules. */
  if (strcmp(q->method, "GET") && strcmp(q->method, "POST") && strcmp(q->method, "HEAD"))
    SP_OPT(CURLOPT_CUSTOMREQUEST, q->method);
  else if (!strcmp(q->method, "GET") && q->body) SP_OPT(CURLOPT_CUSTOMREQUEST, "GET");
#undef SP_OPT
  if (curl_multi_add_handle(s->multi, slot->easy) != CURLM_OK) {
    sp_set_error(r, SP_BACKEND, CURLE_OK); return SP_BACKEND;
  }
  slot->active = 1; return SP_OK;
option_fail:
  sp_set_error(r, SP_BACKEND, code); return SP_BACKEND;
}
static void sp_finish(sp_session *s, sp_slot *slot, CURLcode code) {
  sp_response *r = slot->response;
  char *url = NULL; curl_off_t micros = 0;
  curl_easy_getinfo(slot->easy, CURLINFO_RESPONSE_CODE, &r->status);
  curl_easy_getinfo(slot->easy, CURLINFO_HTTP_VERSION, &r->http_version);
  curl_easy_getinfo(slot->easy, CURLINFO_NUM_CONNECTS, &r->new_connections);
  curl_easy_getinfo(slot->easy, CURLINFO_TOTAL_TIME_T, &micros);
  curl_easy_getinfo(slot->easy, CURLINFO_EFFECTIVE_URL, &url);
  r->elapsed_us = micros > 0 ? (uint64_t)micros : 0;
  int url_error = 0;
  if (url) {
    size_t n = strlen(url) + 1;
    sp_budget *b = r->body.budget;
    if (b && n > b->limit - b->used) url_error = SP_BATCH_LIMIT;
    else { r->url = strdup(url); if (r->url && b) b->used += n; }
  }
  int err = r->body.error ? r->body.error : r->headers.error ? r->headers.error :
    (code == CURLE_ABORTED_BY_CALLBACK && sp_cancel_is_triggered(r->cancel)) ? SP_CANCELLED : url_error ? url_error :
    code ? SP_TRANSPORT : url && !r->url ? SP_NOMEM : SP_OK;
  sp_set_error(r, err, code);
  curl_multi_remove_handle(s->multi, slot->easy); slot->active = 0;
  curl_slist_free_all(slot->headers); slot->headers = NULL;
  /* Detach borrowed bytes/callback pointers before the caller frees them. */
  curl_easy_reset(slot->easy); slot->response = NULL;
  r->body.budget = NULL; r->headers.budget = NULL; r->cancel = NULL;
}
static int sp_execute(sp_session *s, const sp_request *q, size_t count, sp_response **out,
                      sp_cancel *cancel, sp_sink_fn sink, void *sink_user) {
  if (!s || (count && (!q || !out))) return SP_INVALID;
  /* Preflight rejection leaves output untouched; no unbounded output traversal. */
  if (count > s->config.max_batch_requests || count > s->config.max_batch_bytes / sizeof(sp_response)) return SP_BATCH_LIMIT;
  for (size_t i = 0; i < count; i++) out[i] = NULL;
  if (pthread_mutex_trylock(&s->gate)) return SP_BUSY;
  sp_budget budget = {.used = count * sizeof(sp_response), .limit = s->config.max_batch_bytes};
  sp_cancel_link link;
  sp_cancel_attach(cancel, &link, s->multi);
  size_t next = 0, active = 0;
  int err = SP_OK, running = 0;
  while (next < count || active) {
    for (uint32_t i = 0; i < s->config.concurrency && next < count; i++) {
      sp_slot *slot = s->slots + i;
      if (slot->active) continue;
      sp_response *r = calloc(1, sizeof *r);
      if (!r) { err = SP_NOMEM; goto finish; }
      out[next] = r;
      r->body.budget = &budget; r->headers.budget = &budget;
      r->cancel = cancel; r->sink = sink; r->sink_user = sink_user;
      if (sp_cancel_is_triggered(cancel)) sp_set_error(r, SP_CANCELLED, CURLE_ABORTED_BY_CALLBACK);
      else if (sp_prepare(s, slot, q + next, r, 0) == SP_OK) active++;
      else { curl_easy_reset(slot->easy); curl_slist_free_all(slot->headers); slot->headers = NULL; slot->response = NULL; }
      next++;
    }
    if (!active) continue;
    if (sp_cancel_is_triggered(cancel)) {
      for (uint32_t i = 0; i < s->config.concurrency; i++) if (s->slots[i].active) {
        sp_finish(s, s->slots + i, CURLE_ABORTED_BY_CALLBACK); active--;
      }
      continue;
    }
    if (curl_multi_perform(s->multi, &running) != CURLM_OK) { err = SP_BACKEND; goto finish; }
    int messages;
    CURLMsg *msg;
    while ((msg = curl_multi_info_read(s->multi, &messages))) {
      if (msg->msg != CURLMSG_DONE) continue;
      sp_slot *slot = NULL;
      curl_easy_getinfo(msg->easy_handle, CURLINFO_PRIVATE, &slot);
      sp_finish(s, slot, msg->data.result); active--;
    }
    /* Fill freed slots immediately; don't wait while work is queued. */
    if (next < count && active < s->config.concurrency) continue;
    if (active && curl_multi_poll(s->multi, NULL, 0, 1000, NULL) != CURLM_OK) { err = SP_BACKEND; goto finish; }
  }
finish:
  if (err) for (uint32_t i = 0; i < s->config.concurrency; i++)
    if (s->slots[i].active) sp_finish(s, s->slots + i, CURLE_ABORTED_BY_CALLBACK);
  for (size_t i = 0; i < count; i++) if (out[i]) {
    out[i]->body.budget = NULL; out[i]->headers.budget = NULL; out[i]->cancel = NULL;
  }
  sp_cancel_detach(cancel, &link);
  pthread_mutex_unlock(&s->gate); return err;
}
int sp_session_batch(sp_session *s, const sp_request *q, size_t count, sp_response **out) {
  return sp_execute(s, q, count, out, NULL, NULL, NULL);
}
int sp_session_batch_cancel(sp_session *s, const sp_request *q, size_t count, sp_response **out, sp_cancel *cancel) {
  return sp_execute(s, q, count, out, cancel, NULL, NULL);
}
static sp_response *sp_single(sp_session *s, const sp_request *q, sp_cancel *cancel, sp_sink_fn sink, void *user) {
  sp_response *r = NULL;
  int e = sp_execute(s, q, 1, &r, cancel, sink, user);
  if (!r) { r = calloc(1, sizeof *r); if (r) sp_set_error(r, e, CURLE_OK); }
  return r;
}
sp_response *sp_session_request(sp_session *s, const sp_request *q) { return sp_single(s, q, NULL, NULL, NULL); }
sp_response *sp_session_request_cancel(sp_session *s, const sp_request *q, sp_cancel *c) { return sp_single(s, q, c, NULL, NULL); }
sp_response *sp_session_stream(sp_session *s, const sp_request *q, sp_sink_fn sink, void *user, sp_cancel *c) {
  if (!sink) { sp_response *r = calloc(1, sizeof *r); if (r) sp_set_error(r, SP_INVALID, CURLE_OK); return r; }
  return sp_single(s, q, c, sink, user);
}
static int sp_file_sink(const unsigned char *data, size_t n, void *user) { return fwrite(data, 1, n, user) != n; }
sp_response *sp_session_download(sp_session *s, const sp_request *q, const char *path, sp_cancel *cancel) {
  sp_response *r = NULL; FILE *file = NULL; char *temporary = NULL;
  int saved_errno = 0, created = 0;
  if (!s || !q || !path || !*path) {
    r = calloc(1, sizeof *r); if (r) sp_set_error(r, SP_INVALID, CURLE_OK); return r;
  }
  size_t n = strlen(path);
  if (n > SIZE_MAX - 32 || !(temporary = malloc(n + 32))) goto fail;
  snprintf(temporary, n + 32, "%s.scrapanium-XXXXXX", path);
  int fd = mkstemp(temporary);
  if (fd < 0) { saved_errno = errno; goto fail; }
  created = 1;
  file = fdopen(fd, "wb");
  if (!file) { saved_errno = errno; close(fd); goto fail; }
  r = sp_session_stream(s, q, sp_file_sink, file, cancel);
  if (fclose(file)) saved_errno = errno;
  file = NULL;
  if (!r) goto fail;
  if (!r->error && saved_errno) sp_set_error(r, SP_SINK, CURLE_OK);
  if (!r->error && sp_cancel_is_triggered(cancel)) sp_set_error(r, SP_CANCELLED, CURLE_ABORTED_BY_CALLBACK);
  if (!r->error && (r->status < 200 || r->status >= 300)) sp_set_error(r, SP_HTTP_STATUS, CURLE_OK);
  if (!r->error && rename(temporary, path)) { saved_errno = errno; sp_set_error(r, SP_SINK, CURLE_OK); }
  if (r->error) unlink(temporary);
  free(temporary); return r;
fail:
  if (file) fclose(file);
  if (temporary) { if (created) unlink(temporary); free(temporary); }
  if (!r) r = calloc(1, sizeof *r);
  if (r) { sp_set_error(r, saved_errno ? SP_SINK : SP_NOMEM, CURLE_OK);
    if (saved_errno) snprintf(r->message, sizeof r->message, "destination: %s", strerror(saved_errno)); }
  return r;
}
void sp_response_free(sp_response *r) { if (r) { free(r->url); free(r->body.data); free(r->headers.data); free(r); } }
int sp_response_error(const sp_response *r) { return r ? r->error : SP_NOMEM; }
int sp_response_curl_code(const sp_response *r) { return r ? r->curl_code : 0; }
const char *sp_response_message(const sp_response *r) { return r ? r->message : sp_error_message(SP_NOMEM); }
long sp_response_status(const sp_response *r) { return r ? r->status : 0; }
long sp_response_http_version(const sp_response *r) { return r ? r->http_version : 0; }
const unsigned char *sp_response_body(const sp_response *r) { return r && r->body.data ? r->body.data : (const unsigned char *)""; }
unsigned char *sp_response_take_body(sp_response *r, size_t *size) {
  if (size) *size = r ? r->body.size : 0;
  if (!r) return NULL;
  unsigned char *data = r->body.data;
  r->body.data = NULL; r->body.size = r->body.cap = 0;
  return data;
}
size_t sp_response_size(const sp_response *r) { return r ? r->body.size : 0; }
const char *sp_response_headers(const sp_response *r) { return r && r->headers.data ? (const char *)r->headers.data : ""; }
size_t sp_response_headers_size(const sp_response *r) { return r ? r->headers.size : 0; }
const char *sp_response_url(const sp_response *r) { return r && r->url ? r->url : ""; }
uint64_t sp_response_elapsed_us(const sp_response *r) { return r ? r->elapsed_us : 0; }
long sp_response_new_connections(const sp_response *r) { return r ? r->new_connections : 0; }
uint64_t sp_response_downloaded(const sp_response *r) { return r ? r->downloaded : 0; }
size_t sp_response_memory_bytes(const sp_response *r) {
  return r ? sizeof *r + r->body.cap + r->headers.cap + (r->url ? strlen(r->url) + 1 : 0) : 0;
}
#include "websocket.inc.c"
