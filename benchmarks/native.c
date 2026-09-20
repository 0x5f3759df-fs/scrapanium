#define _POSIX_C_SOURCE 200809L
#include "scrapanium.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now(void) {
  struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}
static void check(sp_response *r) {
  if (sp_response_error(r) || sp_response_status(r) != 200) {
    fprintf(stderr, "request failed: %s status=%ld\n", sp_response_message(r), sp_response_status(r)); exit(1);
  }
  sp_response_free(r);
}
int main(int argc, char **argv) {
  if (argc != 5) return 2;
  size_t n = strtoul(argv[3], NULL, 10);
  sp_config c; sp_config_default(&c); c.profile = "chrome146"; c.ca_bundle = argv[4]; c.default_headers = 0;
  int error; sp_session *s = sp_session_new(&c, &error);
  if (!s) { fprintf(stderr, "%s\n", sp_error_message(error)); return 1; }
  const char *headers[] = {"User-Agent: scrapanium-benchmark", "Accept: */*"};
  sp_request q = {.method="GET", .url=argv[1], .headers=headers, .header_count=2};
  check(sp_session_request(s, &q));
  double start = now();
  if (!strcmp(argv[2], "batch")) {
    sp_request *qs = calloc(n, sizeof *qs); sp_response **rs = calloc(n, sizeof *rs);
    if (!qs || !rs) return 1;
    for (size_t i = 0; i < n; i++) qs[i] = q;
    if (sp_session_batch(s, qs, n, rs)) return 1;
    for (size_t i = 0; i < n; i++) check(rs[i]);
    free(qs); free(rs);
  } else for (size_t i = 0; i < n; i++) check(sp_session_request(s, &q));
  printf("%.6f\n", (now() - start) * 1000);
  sp_session_free(s); return 0;
}
