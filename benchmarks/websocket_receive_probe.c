#define _GNU_SOURCE
#include <curl/curl.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static _Atomic uint64_t calls, requested, returned, again, failed, recv_ns, sends;
static _Atomic uint64_t offered[6], actual[7], max_request;
static CURLcode (*real_recv)(CURL *, void *, size_t, size_t *, const struct curl_ws_frame **);
static CURLcode (*real_send)(CURL *, const void *, size_t, size_t *, curl_off_t, unsigned);
static pthread_once_t once = PTHREAD_ONCE_INIT;
static uint64_t now(void) {
  struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
  return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void initialize(void) {
  real_recv = dlsym(RTLD_NEXT, "curl_ws_recv");
  real_send = dlsym(RTLD_NEXT, "curl_ws_send");
  if (!real_recv || !real_send) {
    void *h = dlopen("libcurl-impersonate.so.4", RTLD_LAZY | RTLD_NOLOAD);
    if (h) {
      if (!real_recv) real_recv = dlsym(h, "curl_ws_recv");
      if (!real_send) real_send = dlsym(h, "curl_ws_send");
      dlclose(h);
    }
  }
  if (!real_recv || !real_send) { fputs("probe symbol resolution failed\n", stderr); abort(); }
}
static unsigned bin(size_t n) {
  return n <= 125 ? 0 : n <= 16384 ? 1 : n <= 32768 ? 2 : n <= 65536 ? 3 : n <= 131072 ? 4 : 5;
}
CURLcode curl_ws_recv(CURL *c, void *p, size_t n, size_t *out, const struct curl_ws_frame **meta) {
  pthread_once(&once, initialize);
  uint64_t t = now(); CURLcode code = real_recv(c, p, n, out, meta);
  calls++; requested += n; offered[bin(n)]++; recv_ns += now() - t;
  uint64_t old = atomic_load(&max_request);
  while (n > old && !atomic_compare_exchange_weak(&max_request, &old, n)) {}
  if (code == CURLE_AGAIN) again++;
  else if (code != CURLE_OK) failed++;
  if (code == CURLE_OK) { returned += *out; actual[*out ? 1 + bin(*out) : 0]++; }
  return code;
}
CURLcode curl_ws_send(CURL *c, const void *p, size_t n, size_t *out, curl_off_t z, unsigned flags) {
  pthread_once(&once, initialize); sends++;
  return real_send(c, p, n, out, z, flags);
}
__attribute__((destructor)) static void report(void) {
  const char *path = getenv("WSS_PROBE_OUTPUT");
  if (!path) return;
  FILE *f = fopen(path, "w"); if (!f) return;
  fprintf(f, "{\"recv_calls\":%lu,\"requested_bytes\":%lu,\"returned_bytes\":%lu,\"again\":%lu,\"failed\":%lu,\"recv_ns_instrumented\":%lu,\"send_calls\":%lu,\"max_request\":%lu,\"offered_histogram\":[",
    calls, requested, returned, again, failed, recv_ns, sends, max_request);
  for (unsigned i = 0; i < 6; i++) fprintf(f, "%s%lu", i ? "," : "", offered[i]);
  fputs("],\"successful_return_histogram\":[", f);
  for (unsigned i = 0; i < 7; i++) fprintf(f, "%s%lu", i ? "," : "", actual[i]);
  fputs("]}\n", f); fclose(f);
}
