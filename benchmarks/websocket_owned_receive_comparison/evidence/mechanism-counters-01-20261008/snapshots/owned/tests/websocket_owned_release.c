#define _GNU_SOURCE
#include "scrapanium.h"
#include "ws_async.h"
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static uint64_t monotonic_ms(void) {
  struct timespec now;
  if(clock_gettime(CLOCK_MONOTONIC, &now))
    return 0;
  return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

typedef struct {
  sp_ws_segment *segments;
  size_t expected_size;
  int invalid;
} ReleaseCheck;

static void *release_after_easy_cleanup(void *opaque) {
  ReleaseCheck *check = opaque;
  size_t offset = 0;
  for(sp_ws_segment *segment = check->segments; segment; segment = segment->next) {
    if(segment->size && !segment->data) {
      check->invalid = 1;
      break;
    }
    for(size_t i = 0; i < segment->size; i++, offset++) {
      unsigned char expected = (unsigned char)(offset * 37 + 11);
      if(segment->data[i] != expected) {
        check->invalid = 1;
        break;
      }
    }
    if(check->invalid)
      break;
  }
  if(offset != check->expected_size)
    check->invalid = 1;
  sp_ws_segments_free(check->segments);
  check->segments = NULL;
  return NULL;
}

int main(int argc, char **argv) {
  if(argc != 3) {
    fprintf(stderr, "usage: %s ws-url ca-bundle-or-empty\n", argv[0]);
    return 2;
  }
  sp_config config;
  sp_config_default(&config);
  config.ca_bundle = argv[2][0] ? argv[2] : NULL;
  config.timeout_ms = 10000;
  int error = SP_OK;
  sp_session *session = sp_session_new(&config, &error);
  if(!session) {
    fprintf(stderr, "session creation failed: %d\n", error);
    return 1;
  }
  sp_request request = {.method = "GET", .url = argv[1]};
  sp_ws *socket = sp_ws_upgrade(session, &request, NULL, &error);
  session = NULL;
  if(!socket) {
    fprintf(stderr, "WebSocket upgrade failed: %d\n", error);
    return 1;
  }

  sp_ws_io *io = sp_ws_io_start(socket, SP_WS_RECEIVE, 0, NULL, 0,
                                10000, NULL, &error);
  if(!io || error) {
    fprintf(stderr, "receive setup failed: %d\n", error);
    sp_ws_free(socket);
    return 1;
  }

  unsigned kind = 0;
  unsigned char *data = NULL;
  size_t size = 0;
  sp_ws_segment *segments = NULL;
  for(;;) {
    error = sp_ws_io_step_owned(io, &kind, &data, &size, &segments);
    if(error != SP_WS_PENDING)
      break;
    int fd = -1;
    short events = 0;
    uint64_t wake = 0;
    error = sp_ws_io_poll(io, &fd, &events, &wake);
    if(error)
      break;
    uint64_t now = monotonic_ms();
    int timeout = wake > now ? (int)(wake - now) : 0;
    if(timeout > 100)
      timeout = 100;
    struct pollfd descriptor = {.fd = fd, .events = events};
    int ready = poll(&descriptor, 1, timeout);
    if(ready < 0 && errno != EINTR) {
      error = SP_TRANSPORT;
      break;
    }
  }
  if(error || kind != 2 || size != 300000 || data || !segments) {
    fprintf(stderr, "owned receive validation failed: error=%d kind=%u size=%zu data=%p segments=%p\n",
            error, kind, size, (void *)data, (void *)segments);
    free(data);
    sp_ws_segments_free(segments);
    sp_ws_io_free(io);
    sp_ws_free(socket);
    return 1;
  }
  sp_ws_io_free(io);
  sp_ws_free(socket);

  ReleaseCheck check = {.segments = segments, .expected_size = size};
  pthread_t worker;
  if(pthread_create(&worker, NULL, release_after_easy_cleanup, &check)) {
    sp_ws_segments_free(segments);
    return 1;
  }
  if(pthread_join(worker, NULL) || check.invalid) {
    fprintf(stderr, "payload did not survive easy cleanup and worker-thread release\n");
    return 1;
  }
  puts("owned-payload-worker-release-ok");
  return 0;
}
