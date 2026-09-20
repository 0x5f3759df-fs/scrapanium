#define _POSIX_C_SOURCE 200809L
#include "scrapanium.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

typedef struct { sp_session *session; sp_cancel *token; const char *url; } Worker;
static void *requests(void *arg) {
  Worker *w = arg;
  sp_request qs[24]; sp_response *rs[24] = {0};
  for (int i = 0; i < 24; i++) qs[i] = (sp_request){.method = "GET", .url = w->url};
  assert(!sp_session_batch_cancel(w->session, qs, 24, rs, w->token));
  for (int i = 0; i < 24; i++) {
    assert(rs[i] && sp_response_error(rs[i]) == SP_CANCELLED);
    sp_response_free(rs[i]);
  }
  sp_cancel_free(w->token);
  return NULL;
}
typedef struct { sp_cancel *token; size_t received; } Sink;
static int sink(const unsigned char *data, size_t n, void *arg) {
  Sink *s = arg; assert(data && n);
  s->received += n; sp_cancel_trigger(s->token); return 0;
}
int main(int argc, char **argv) {
  assert(argc == 2);
  char slow[512], paced[512], bomb[512];
  snprintf(slow, sizeof slow, "%s/slow/250", argv[1]);
  snprintf(paced, sizeof paced, "%s/paced", argv[1]);
  snprintf(bomb, sizeof bomb, "%s/bomb", argv[1]);
  sp_config c; sp_config_default(&c); c.concurrency = 2;
  int error;
  sp_session *a = sp_session_new(&c, &error), *b = sp_session_new(&c, &error);
  assert(a && b);
  for (int round = 0; round < 40; round++) {
    sp_cancel *token = sp_cancel_new(); assert(token);
    Worker jobs[] = {{a, token, slow}, {b, token, slow}};
    pthread_t threads[2];
    for (int i = 0; i < 2; i++) { sp_cancel_retain(token); assert(!pthread_create(&threads[i], NULL, requests, jobs + i)); }
    struct timespec delay = {.tv_nsec = 1000000}; nanosleep(&delay, NULL);
    sp_cancel_trigger(token); sp_cancel_trigger(token); sp_cancel_free(token);
    for (int i = 0; i < 2; i++) assert(!pthread_join(threads[i], NULL));
    sp_request q = {.method = "GET", .url = argv[1]};
    sp_response *r = sp_session_request(a, &q); assert(!sp_response_error(r)); sp_response_free(r);
    token = sp_cancel_new(); assert(token); Sink consumer = {.token = token};
    q.url = paced;
    r = sp_session_stream(a, &q, sink, &consumer, token);
    assert(sp_response_error(r) == SP_CANCELLED && consumer.received && !sp_response_size(r));
    sp_response_free(r); sp_cancel_free(token);
  }
  sp_session_free(a); sp_session_free(b);
  c.max_batch_bytes = 32768; a = sp_session_new(&c, &error); assert(a);
  for (int round = 0; round < 20; round++) {
    sp_request qs[12]; sp_response *rs[12] = {0};
    for (int i = 0; i < 12; i++) qs[i] = (sp_request){.method = "GET", .url = bomb};
    assert(!sp_session_batch(a, qs, 12, rs));
    size_t memory = 0;
    for (int i = 0; i < 12; i++) {
      assert(sp_response_error(rs[i]) == SP_BATCH_LIMIT);
      memory += sp_response_memory_bytes(rs[i]); sp_response_free(rs[i]);
    }
    assert(memory <= c.max_batch_bytes);
  }
  sp_session_free(a);
  puts("resources: passed");
  return 0;
}
