/* Counters-only diagnostic hook. Called at four quiescent workload boundaries. */
#include "scrapanium.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern void curl_ws_diag_snapshot(uint64_t *, size_t);
extern size_t curl_ws_diag_snapshot_count(void);
extern const char *curl_ws_diag_counter_name(size_t);

static pthread_mutex_t diag_gate = PTHREAD_MUTEX_INITIALIZER;
static int diag_fd = -1;
static unsigned diag_next_phase;

static const char *native_name(unsigned i) {
  const char *name = sp_wss_diag_counter_name((unsigned)i);
  if(name)
    return name;
  switch(i) {
    case SP_WSS_DIAG_LAYOUT_SEGMENT_SIZE: return "layout_segment_size";
    case SP_WSS_DIAG_LAYOUT_BUFFER_SIZE: return "layout_buffer_size";
    case SP_WSS_DIAG_LAYOUT_WS_IO_SIZE: return "layout_ws_io_size";
    default: return NULL;
  }
}

static int append_values(char *line, size_t cap, size_t *used,
                         const char *object, const uint64_t *values,
                         size_t count, const char *(*name_fn)(unsigned)) {
  int n = snprintf(line + *used, cap - *used, "\"%s\":{", object);
  if(n < 0 || (size_t)n >= cap - *used)
    return 0;
  *used += (size_t)n;
  for(size_t i = 0; i < count; i++) {
    const char *name = name_fn((unsigned)i);
    if(!name)
      return 0;
    n = snprintf(line + *used, cap - *used, "%s\"%s\":%" PRIu64,
                 i ? "," : "", name, values[i]);
    if(n < 0 || (size_t)n >= cap - *used)
      return 0;
    *used += (size_t)n;
  }
  n = snprintf(line + *used, cap - *used, "}");
  if(n < 0 || (size_t)n >= cap - *used)
    return 0;
  *used += (size_t)n;
  return 1;
}

static const char *curl_name(unsigned i) {
  return curl_ws_diag_counter_name((size_t)i);
}

static Term diag_failure(Env e, const char *message) {
  if(diag_fd >= 0) {
    close(diag_fd);
    diag_fd = -1;
  }
  return io_fail(e, SP_BACKEND, message);
}

static int diag_write_all(int fd, const char *data, size_t size) {
  while(size) {
    ssize_t n = write(fd, data, size);
    if(n < 0 && errno == EINTR)
      continue;
    if(n <= 0)
      return 0;
    data += (size_t)n;
    size -= (size_t)n;
  }
  return 1;
}

static Term ws_bench_diag_mark(Env e, Term *fields, IoWork *work) {
  (void)work;
  static const char *const phases[] = {"cold", "gate", "work_end", "released"};
  pthread_mutex_lock(&diag_gate);
  if(diag_next_phase >= sizeof phases / sizeof phases[0]) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "too many mechanism diagnostic snapshots");
  }
  u64 phase_len = 0;
  char *phase = io_cstr(e, fields[0], &phase_len);
  const char *expected = phases[diag_next_phase];
  int phase_matches = phase_len == strlen(expected) && !memcmp(phase, expected, phase_len);
  free(phase);
  if(!phase_matches) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism diagnostic snapshots out of order");
  }
  if(diag_next_phase == 0) {
    const char *path = getenv("SCRAPANIUM_MECHANISM_COUNTERS");
    if(!path || !*path) {
      pthread_mutex_unlock(&diag_gate);
      return diag_failure(e, "SCRAPANIUM_MECHANISM_COUNTERS is required");
    }
    diag_fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if(diag_fd < 0) {
      pthread_mutex_unlock(&diag_gate);
      return diag_failure(e, "could not exclusively create mechanism counter file");
    }
  } else if(diag_fd < 0) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter file was not initialized");
  }

  size_t native_count = sp_wss_diag_snapshot_count();
  size_t curl_count = curl_ws_diag_snapshot_count();
  uint64_t native_values[128], curl_values[128];
  if(native_count > 128 || curl_count > 128) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter snapshot exceeds fixed capacity");
  }
  sp_wss_diag_snapshot(native_values, native_count);
  curl_ws_diag_snapshot(curl_values, curl_count);

  char line[32768];
  size_t used = 0;
  int n = snprintf(line, sizeof line, "{\"phase\":\"%s\",", expected);
  if(n < 0 || (size_t)n >= sizeof line) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter row formatting failed");
  }
  used = (size_t)n;
  if(!append_values(line, sizeof line, &used, "native", native_values,
                    native_count, native_name) || used >= sizeof line) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter native row formatting failed");
  }
  line[used++] = ',';
  if(!append_values(line, sizeof line, &used, "curl", curl_values,
                    curl_count, curl_name)) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter Curl row formatting failed");
  }
  if(used + 2 > sizeof line) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter row is too long");
  }
  line[used++] = '}';
  line[used++] = '\n';
  if(!diag_write_all(diag_fd, line, used) || fsync(diag_fd)) {
    pthread_mutex_unlock(&diag_gate);
    return diag_failure(e, "mechanism counter snapshot write failed");
  }
  diag_next_phase++;
  if(diag_next_phase == sizeof phases / sizeof phases[0]) {
    int close_error = close(diag_fd);
    diag_fd = -1;
    if(close_error) {
      pthread_mutex_unlock(&diag_gate);
      return io_fail(e, SP_BACKEND, "mechanism counter file close failed");
    }
  }
  pthread_mutex_unlock(&diag_gate);
  return io_done(e, term_pak(CID_UNIT, 0));
}

static void __attribute__((constructor)) ws_bench_diag_register(void) {
  io_eff(CID_WSBENCHDIAG_CHECKED_MARK, ws_bench_diag_mark, 0);
}
