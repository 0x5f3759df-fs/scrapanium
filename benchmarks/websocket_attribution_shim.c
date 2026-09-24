#define _GNU_SOURCE
#include <curl/curl.h>
#include <curl/websockets.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/select.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <poll.h>

#define WSATTR_MAX_THREADS 32

typedef struct {
  uint64_t tid;
  uint64_t recv_calls, recv_again, recv_ok, recv_other, recv_bytes;
  uint64_t recv_wall_ns, recv_thread_cpu_ns, recv_tid_mismatch;
  uint64_t poll_calls, poll_ready, poll_timeout, poll_error;
  uint64_t poll_wall_ns, poll_thread_cpu_ns, poll_tid_mismatch;
  uint64_t ppoll_calls, ppoll_ready, ppoll_timeout, ppoll_error;
  uint64_t ppoll_wall_ns, ppoll_thread_cpu_ns, ppoll_tid_mismatch;
  uint64_t select_calls, select_ready, select_timeout, select_error;
  uint64_t select_wall_ns, select_thread_cpu_ns, select_tid_mismatch;
  uint64_t epoll_calls, epoll_ready, epoll_timeout, epoll_error;
  uint64_t epoll_wall_ns, epoll_thread_cpu_ns, epoll_tid_mismatch;
} wsattr_thread;

typedef struct {
  int started, completed, clock_error, thread_overflow, write_error;
  int mode;
  uint64_t start_mono_ns, end_mono_ns;
  uint64_t start_thread_cpu_ns, end_thread_cpu_ns;
  uint64_t start_process_cpu_ns, end_process_cpu_ns;
  uint64_t start_tid, end_tid;
  size_t thread_count;
  wsattr_thread threads[WSATTR_MAX_THREADS];
} wsattr_snapshot;

typedef struct {
  int enabled, timed;
  uint64_t tid, wall_start_ns, cpu_start_ns;
  int clock_error;
} wsattr_scope;

static pthread_mutex_t wsattr_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_int wsattr_active = ATOMIC_VAR_INIT(0);
static atomic_int wsattr_mode = ATOMIC_VAR_INIT(0);
static atomic_uint wsattr_inflight = ATOMIC_VAR_INIT(0);
static int wsattr_started, wsattr_completed, wsattr_written;
static int wsattr_clock_error, wsattr_thread_overflow, wsattr_write_error;
static int wsattr_selected_mode;
static uint64_t wsattr_start_mono_ns, wsattr_end_mono_ns;
static uint64_t wsattr_start_thread_cpu_ns, wsattr_end_thread_cpu_ns;
static uint64_t wsattr_start_process_cpu_ns, wsattr_end_process_cpu_ns;
static uint64_t wsattr_start_tid, wsattr_end_tid;
static size_t wsattr_thread_count;
static wsattr_thread wsattr_threads[WSATTR_MAX_THREADS];

static uint64_t wsattr_tid(void) {
  return (uint64_t)syscall(SYS_gettid);
}

static int wsattr_clock(clockid_t clock_id, uint64_t *value) {
  struct timespec now;
  if (clock_gettime(clock_id, &now) != 0 || now.tv_sec < 0 ||
      now.tv_nsec < 0 || now.tv_nsec >= 1000000000L ||
      (uint64_t)now.tv_sec > UINT64_MAX / UINT64_C(1000000000))
    return 0;
  *value = (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
  return 1;
}

static wsattr_thread *wsattr_thread_locked(uint64_t tid) {
  for (size_t i = 0; i < wsattr_thread_count; i++)
    if (wsattr_threads[i].tid == tid) return &wsattr_threads[i];
  if (wsattr_thread_count == WSATTR_MAX_THREADS) {
    wsattr_thread_overflow = 1;
    return NULL;
  }
  wsattr_thread *entry = &wsattr_threads[wsattr_thread_count++];
  memset(entry, 0, sizeof *entry);
  entry->tid = tid;
  return entry;
}

static void wsattr_wait_for_inflight(void) {
  while (atomic_load_explicit(&wsattr_inflight, memory_order_acquire) != 0)
    sched_yield();
}

static wsattr_scope wsattr_enter(void) {
  wsattr_scope scope = {0};
  if (!atomic_load_explicit(&wsattr_active, memory_order_acquire)) return scope;
  atomic_fetch_add_explicit(&wsattr_inflight, 1, memory_order_acq_rel);
  if (!atomic_load_explicit(&wsattr_active, memory_order_acquire)) {
    atomic_fetch_sub_explicit(&wsattr_inflight, 1, memory_order_release);
    return scope;
  }
  scope.enabled = 1;
  scope.timed = atomic_load_explicit(&wsattr_mode, memory_order_relaxed) == 1;
  scope.tid = wsattr_tid();
  if (scope.timed && (!wsattr_clock(CLOCK_MONOTONIC, &scope.wall_start_ns) ||
                      !wsattr_clock(CLOCK_THREAD_CPUTIME_ID, &scope.cpu_start_ns)))
    scope.clock_error = 1;
  return scope;
}

static void wsattr_leave(wsattr_scope *scope, uint64_t *wall_ns,
                         uint64_t *thread_cpu_ns, uint64_t *end_tid) {
  if (!scope->enabled) return;
  *wall_ns = 0;
  *thread_cpu_ns = 0;
  *end_tid = wsattr_tid();
  int clock_ok = !scope->clock_error;
  if (scope->timed) {
    uint64_t wall_end = 0, cpu_end = 0;
    if (!wsattr_clock(CLOCK_THREAD_CPUTIME_ID, &cpu_end) ||
        !wsattr_clock(CLOCK_MONOTONIC, &wall_end) ||
        wall_end < scope->wall_start_ns || cpu_end < scope->cpu_start_ns ||
        *end_tid != scope->tid) {
      clock_ok = 0;
    } else {
      *wall_ns = wall_end - scope->wall_start_ns;
      *thread_cpu_ns = cpu_end - scope->cpu_start_ns;
    }
  }
  if (!clock_ok) {
    pthread_mutex_lock(&wsattr_lock);
    wsattr_clock_error = 1;
    pthread_mutex_unlock(&wsattr_lock);
  }
}

static void wsattr_record_recv(const wsattr_scope *scope, CURLcode code,
                               size_t bytes, uint64_t wall_ns,
                               uint64_t cpu_ns, uint64_t end_tid) {
  if (!scope->enabled) return;
  pthread_mutex_lock(&wsattr_lock);
  wsattr_thread *entry = wsattr_thread_locked(scope->tid);
  if (entry) {
    entry->recv_calls++;
    if (code == CURLE_AGAIN) entry->recv_again++;
    else if (code == CURLE_OK) entry->recv_ok++;
    else entry->recv_other++;
    entry->recv_bytes += bytes;
    entry->recv_wall_ns += wall_ns;
    entry->recv_thread_cpu_ns += cpu_ns;
    if (end_tid != scope->tid) entry->recv_tid_mismatch++;
  }
  pthread_mutex_unlock(&wsattr_lock);
  atomic_fetch_sub_explicit(&wsattr_inflight, 1, memory_order_release);
}

enum wsattr_wait_kind { WSATTR_POLL, WSATTR_PPOLL, WSATTR_SELECT, WSATTR_EPOLL };

static void wsattr_record_wait(const wsattr_scope *scope, int result,
                               enum wsattr_wait_kind kind, uint64_t wall_ns,
                               uint64_t cpu_ns, uint64_t end_tid) {
  if (!scope->enabled) return;
  pthread_mutex_lock(&wsattr_lock);
  wsattr_thread *entry = wsattr_thread_locked(scope->tid);
  if (entry) {
    uint64_t *calls = NULL, *ready = NULL, *timeout = NULL, *error = NULL;
    uint64_t *wall = NULL, *cpu = NULL, *mismatch = NULL;
    switch (kind) {
      case WSATTR_POLL:
        calls = &entry->poll_calls; ready = &entry->poll_ready;
        timeout = &entry->poll_timeout; error = &entry->poll_error;
        wall = &entry->poll_wall_ns; cpu = &entry->poll_thread_cpu_ns;
        mismatch = &entry->poll_tid_mismatch; break;
      case WSATTR_PPOLL:
        calls = &entry->ppoll_calls; ready = &entry->ppoll_ready;
        timeout = &entry->ppoll_timeout; error = &entry->ppoll_error;
        wall = &entry->ppoll_wall_ns; cpu = &entry->ppoll_thread_cpu_ns;
        mismatch = &entry->ppoll_tid_mismatch; break;
      case WSATTR_SELECT:
        calls = &entry->select_calls; ready = &entry->select_ready;
        timeout = &entry->select_timeout; error = &entry->select_error;
        wall = &entry->select_wall_ns; cpu = &entry->select_thread_cpu_ns;
        mismatch = &entry->select_tid_mismatch; break;
      case WSATTR_EPOLL:
        calls = &entry->epoll_calls; ready = &entry->epoll_ready;
        timeout = &entry->epoll_timeout; error = &entry->epoll_error;
        wall = &entry->epoll_wall_ns; cpu = &entry->epoll_thread_cpu_ns;
        mismatch = &entry->epoll_tid_mismatch; break;
    }
    (*calls)++;
    if (result < 0) (*error)++;
    else if (result == 0) (*timeout)++;
    else (*ready)++;
    *wall += wall_ns;
    *cpu += cpu_ns;
    if (end_tid != scope->tid) (*mismatch)++;
  }
  pthread_mutex_unlock(&wsattr_lock);
  atomic_fetch_sub_explicit(&wsattr_inflight, 1, memory_order_release);
}

static void wsattr_copy_snapshot(wsattr_snapshot *snapshot, int completed) {
  pthread_mutex_lock(&wsattr_lock);
  memset(snapshot, 0, sizeof *snapshot);
  snapshot->started = wsattr_started;
  snapshot->completed = completed;
  snapshot->clock_error = wsattr_clock_error;
  snapshot->thread_overflow = wsattr_thread_overflow;
  snapshot->write_error = wsattr_write_error;
  snapshot->mode = wsattr_selected_mode;
  snapshot->start_mono_ns = wsattr_start_mono_ns;
  snapshot->end_mono_ns = wsattr_end_mono_ns;
  snapshot->start_thread_cpu_ns = wsattr_start_thread_cpu_ns;
  snapshot->end_thread_cpu_ns = wsattr_end_thread_cpu_ns;
  snapshot->start_process_cpu_ns = wsattr_start_process_cpu_ns;
  snapshot->end_process_cpu_ns = wsattr_end_process_cpu_ns;
  snapshot->start_tid = wsattr_start_tid;
  snapshot->end_tid = wsattr_end_tid;
  snapshot->thread_count = wsattr_thread_count;
  memcpy(snapshot->threads, wsattr_threads,
         wsattr_thread_count * sizeof(wsattr_threads[0]));
  pthread_mutex_unlock(&wsattr_lock);
}

static int wsattr_write_record(const wsattr_snapshot *snapshot) {
  const char *path = getenv("SCRAPANIUM_ATTRIBUTION_RECORD");
  if (!path || !*path) return 0;
  int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
  if (fd < 0) return 0;
  FILE *file = fdopen(fd, "w");
  if (!file) { close(fd); return 0; }
  const char *mode = snapshot->mode == 1 ? "attribution" : "control";
  wsattr_thread aggregate = {0};
  uint64_t wait_calls[4] = {0}, wait_ready[4] = {0};
  uint64_t wait_timeout[4] = {0}, wait_error[4] = {0};
  uint64_t wait_wall[4] = {0}, wait_cpu[4] = {0};
  for (size_t i = 0; i < snapshot->thread_count; i++) {
    const wsattr_thread *t = &snapshot->threads[i];
    aggregate.recv_calls += t->recv_calls; aggregate.recv_again += t->recv_again;
    aggregate.recv_ok += t->recv_ok; aggregate.recv_other += t->recv_other;
    aggregate.recv_bytes += t->recv_bytes; aggregate.recv_wall_ns += t->recv_wall_ns;
    aggregate.recv_thread_cpu_ns += t->recv_thread_cpu_ns;
    const uint64_t *calls[] = {&t->poll_calls, &t->ppoll_calls, &t->select_calls, &t->epoll_calls};
    const uint64_t *ready[] = {&t->poll_ready, &t->ppoll_ready, &t->select_ready, &t->epoll_ready};
    const uint64_t *timeout[] = {&t->poll_timeout, &t->ppoll_timeout, &t->select_timeout, &t->epoll_timeout};
    const uint64_t *error[] = {&t->poll_error, &t->ppoll_error, &t->select_error, &t->epoll_error};
    const uint64_t *wall[] = {&t->poll_wall_ns, &t->ppoll_wall_ns, &t->select_wall_ns, &t->epoll_wall_ns};
    const uint64_t *cpu[] = {&t->poll_thread_cpu_ns, &t->ppoll_thread_cpu_ns, &t->select_thread_cpu_ns, &t->epoll_thread_cpu_ns};
    for (int kind = 0; kind < 4; kind++) {
      wait_calls[kind] += *calls[kind]; wait_ready[kind] += *ready[kind];
      wait_timeout[kind] += *timeout[kind]; wait_error[kind] += *error[kind];
      wait_wall[kind] += *wall[kind]; wait_cpu[kind] += *cpu[kind];
    }
  }
  int caller_valid = snapshot->start_tid == snapshot->end_tid &&
      snapshot->end_thread_cpu_ns >= snapshot->start_thread_cpu_ns;
  int process_valid = snapshot->end_process_cpu_ns >= snapshot->start_process_cpu_ns;
  fprintf(file,
    "{\"schema\":1,\"mode\":\"%s\",\"started\":%s,\"completed\":%s,"
    "\"clock_error\":%s,\"thread_overflow\":%s,\"write_error\":%s,"
    "\"clock_scope\":\"wall start encloses CPU start; CPU end precedes wall end\","
    "\"interval\":{\"start_monotonic_ns\":%llu,\"end_monotonic_ns\":%llu,"
    "\"wall_ns\":%llu,\"start_tid\":%llu,\"end_tid\":%llu,"
    "\"caller_thread_cpu_valid\":%s,\"caller_thread_cpu_ns\":",
    mode, snapshot->started ? "true" : "false", snapshot->completed ? "true" : "false",
    snapshot->clock_error ? "true" : "false",
    snapshot->thread_overflow ? "true" : "false",
    snapshot->write_error ? "true" : "false",
    (unsigned long long)snapshot->start_mono_ns,
    (unsigned long long)snapshot->end_mono_ns,
    (unsigned long long)(snapshot->end_mono_ns >= snapshot->start_mono_ns ?
      snapshot->end_mono_ns - snapshot->start_mono_ns : 0),
    (unsigned long long)snapshot->start_tid,
    (unsigned long long)snapshot->end_tid,
    caller_valid ? "true" : "false");
  if (caller_valid)
    fprintf(file, "%llu", (unsigned long long)(snapshot->end_thread_cpu_ns - snapshot->start_thread_cpu_ns));
  else
    fputs("null", file);
  fputs(",\"process_cpu_valid\":", file);
  fputs(process_valid ? "true" : "false", file);
  fputs(",\"process_cpu_ns\":", file);
  if (process_valid)
    fprintf(file, "%llu", (unsigned long long)(snapshot->end_process_cpu_ns - snapshot->start_process_cpu_ns));
  else
    fputs("null", file);
  fputs("},\"curl_ws_recv\":{\"timing_enabled\":", file);
  fputs(snapshot->mode == 1 ? "true" : "false", file);
  fprintf(file, ",\"calls\":%llu,\"curle_again\":%llu,\"ok\":%llu,\"other\":%llu,"
               "\"bytes\":%llu,\"wall_sum_across_threads_ns\":%llu,"
               "\"thread_cpu_sum_across_threads_ns\":%llu},\"waits\":{",
    (unsigned long long)aggregate.recv_calls,
    (unsigned long long)aggregate.recv_again,
    (unsigned long long)aggregate.recv_ok,
    (unsigned long long)aggregate.recv_other,
    (unsigned long long)aggregate.recv_bytes,
    (unsigned long long)aggregate.recv_wall_ns,
    (unsigned long long)aggregate.recv_thread_cpu_ns);
  const char *names[] = {"poll", "ppoll", "select", "epoll"};
  for (int kind = 0; kind < 4; kind++) {
    if (kind) fputc(',', file);
    fprintf(file, "\"%s\":{\"calls\":%llu,\"ready\":%llu,\"timeout\":%llu,"
                 "\"error\":%llu,\"wall_sum_across_threads_ns\":%llu,"
                 "\"thread_cpu_sum_across_threads_ns\":%llu}", names[kind],
      (unsigned long long)wait_calls[kind], (unsigned long long)wait_ready[kind],
      (unsigned long long)wait_timeout[kind], (unsigned long long)wait_error[kind],
      (unsigned long long)wait_wall[kind], (unsigned long long)wait_cpu[kind]);
  }
  fputs("},\"threads\":[", file);
  for (size_t i = 0; i < snapshot->thread_count; i++) {
    const wsattr_thread *t = &snapshot->threads[i];
    if (i) fputc(',', file);
    fprintf(file,
      "{\"tid\":%llu,\"curl_ws_recv_calls\":%llu,\"curl_ws_recv_curle_again\":%llu,"
      "\"curl_ws_recv_ok\":%llu,\"curl_ws_recv_other\":%llu,\"curl_ws_recv_bytes\":%llu,"
      "\"curl_ws_recv_wall_sum_ns\":%llu,\"curl_ws_recv_thread_cpu_sum_ns\":%llu,"
      "\"curl_ws_recv_tid_mismatch\":%llu,",
      (unsigned long long)t->tid,
      (unsigned long long)t->recv_calls, (unsigned long long)t->recv_again,
      (unsigned long long)t->recv_ok, (unsigned long long)t->recv_other,
      (unsigned long long)t->recv_bytes, (unsigned long long)t->recv_wall_ns,
      (unsigned long long)t->recv_thread_cpu_ns,
      (unsigned long long)t->recv_tid_mismatch);
    const uint64_t *calls[] = {&t->poll_calls, &t->ppoll_calls, &t->select_calls, &t->epoll_calls};
    const uint64_t *ready[] = {&t->poll_ready, &t->ppoll_ready, &t->select_ready, &t->epoll_ready};
    const uint64_t *timeout[] = {&t->poll_timeout, &t->ppoll_timeout, &t->select_timeout, &t->epoll_timeout};
    const uint64_t *error[] = {&t->poll_error, &t->ppoll_error, &t->select_error, &t->epoll_error};
    const uint64_t *wall[] = {&t->poll_wall_ns, &t->ppoll_wall_ns, &t->select_wall_ns, &t->epoll_wall_ns};
    const uint64_t *cpu[] = {&t->poll_thread_cpu_ns, &t->ppoll_thread_cpu_ns, &t->select_thread_cpu_ns, &t->epoll_thread_cpu_ns};
    const uint64_t *mismatch[] = {&t->poll_tid_mismatch, &t->ppoll_tid_mismatch, &t->select_tid_mismatch, &t->epoll_tid_mismatch};
    for (int kind = 0; kind < 4; kind++) {
      if (kind) fputc(',', file);
      fprintf(file, "\"%s\":{\"calls\":%llu,\"ready\":%llu,\"timeout\":%llu,"
                   "\"error\":%llu,\"wall_sum_ns\":%llu,\"thread_cpu_sum_ns\":%llu,"
                   "\"tid_mismatch\":%llu}", names[kind],
        (unsigned long long)*calls[kind], (unsigned long long)*ready[kind],
        (unsigned long long)*timeout[kind], (unsigned long long)*error[kind],
        (unsigned long long)*wall[kind], (unsigned long long)*cpu[kind],
        (unsigned long long)*mismatch[kind]);
    }
    fputc('}', file);
  }
  fputs("]}", file);
  if (fflush(file) != 0 || fsync(fd) != 0) {
    fclose(file);
    return 0;
  }
  return fclose(file) == 0;
}

static void wsattr_finish(int completed) {
  if (!wsattr_started || wsattr_written) return;
  atomic_store_explicit(&wsattr_active, 0, memory_order_release);
  wsattr_wait_for_inflight();
  pthread_mutex_lock(&wsattr_lock);
  if (!wsattr_written) {
    if (!wsattr_clock(CLOCK_PROCESS_CPUTIME_ID, &wsattr_end_process_cpu_ns) ||
        !wsattr_clock(CLOCK_THREAD_CPUTIME_ID, &wsattr_end_thread_cpu_ns) ||
        !wsattr_clock(CLOCK_MONOTONIC, &wsattr_end_mono_ns))
      wsattr_clock_error = 1;
    wsattr_end_tid = wsattr_tid();
    wsattr_completed = completed;
    wsattr_written = 1;
  }
  pthread_mutex_unlock(&wsattr_lock);
  wsattr_snapshot snapshot;
  wsattr_copy_snapshot(&snapshot, completed);
  if (!wsattr_write_record(&snapshot)) {
    pthread_mutex_lock(&wsattr_lock);
    wsattr_write_error = 1;
    pthread_mutex_unlock(&wsattr_lock);
  }
}

__attribute__((visibility("default"))) int wsattr_begin(int mode) {
  if (mode != 0 && mode != 1) return 0;
  pthread_mutex_lock(&wsattr_lock);
  if (wsattr_started || wsattr_written || wsattr_inflight) {
    pthread_mutex_unlock(&wsattr_lock);
    return 0;
  }
  wsattr_thread_count = 0;
  memset(wsattr_threads, 0, sizeof wsattr_threads);
  wsattr_clock_error = wsattr_thread_overflow = wsattr_write_error = 0;
  wsattr_start_mono_ns = wsattr_end_mono_ns = 0;
  wsattr_start_thread_cpu_ns = wsattr_end_thread_cpu_ns = 0;
  wsattr_start_process_cpu_ns = wsattr_end_process_cpu_ns = 0;
  wsattr_start_tid = wsattr_end_tid = 0;
  wsattr_selected_mode = mode;
  if (!wsattr_clock(CLOCK_MONOTONIC, &wsattr_start_mono_ns) ||
      !wsattr_clock(CLOCK_THREAD_CPUTIME_ID, &wsattr_start_thread_cpu_ns) ||
      !wsattr_clock(CLOCK_PROCESS_CPUTIME_ID, &wsattr_start_process_cpu_ns))
    wsattr_clock_error = 1;
  wsattr_start_tid = wsattr_tid();
  wsattr_started = 1;
  wsattr_completed = 0;
  atomic_store_explicit(&wsattr_mode, mode, memory_order_relaxed);
  atomic_store_explicit(&wsattr_active, 1, memory_order_release);
  pthread_mutex_unlock(&wsattr_lock);
  return wsattr_clock_error ? 0 : 1;
}

__attribute__((visibility("default"))) uint64_t wsattr_end(void) {
  if (!wsattr_started || wsattr_written) return 0;
  wsattr_finish(1);
  return wsattr_end_mono_ns >= wsattr_start_mono_ns ?
      wsattr_end_mono_ns - wsattr_start_mono_ns : 0;
}

__attribute__((destructor)) static void wsattr_destructor(void) {
  if (wsattr_started && !wsattr_written) wsattr_finish(0);
}

typedef CURLcode (*wsattr_curl_ws_recv_fn)(CURL *, void *, size_t, size_t *,
                                           const struct curl_ws_frame **);
static wsattr_curl_ws_recv_fn real_curl_ws_recv;
static pthread_once_t curl_recv_once = PTHREAD_ONCE_INIT;

static void *wsattr_open_mapped_backend(void) {
  const char *prefix = getenv("SCRAPANIUM_CURL_DIR");
  char path[4096];
  if (prefix && *prefix) {
    int length = snprintf(path, sizeof path, "%s/lib/libcurl-impersonate.so.4", prefix);
    if (length > 0 && (size_t)length < sizeof path) {
      void *handle = dlopen(path, RTLD_LAZY | RTLD_NOLOAD);
      if (handle) return handle;
    }
    length = snprintf(path, sizeof path, "%s/libcurl-impersonate.so.4", prefix);
    if (length > 0 && (size_t)length < sizeof path) {
      void *handle = dlopen(path, RTLD_LAZY | RTLD_NOLOAD);
      if (handle) return handle;
    }
  }
  return dlopen("libcurl-impersonate.so.4", RTLD_LAZY | RTLD_NOLOAD);
}

static int wsattr_is_impersonate_symbol(wsattr_curl_ws_recv_fn function) {
  Dl_info info;
  return function && dladdr((void *)function, &info) != 0 && info.dli_fname &&
      strstr(info.dli_fname, "libcurl-impersonate.so") != NULL;
}

static void wsattr_resolve_curl_recv(void) {
  /* Prefer the exact already-mapped impersonate backend. curl_cffi may load
   * it locally, outside RTLD_NEXT's lookup scope, and another libcurl could
   * otherwise appear earlier in that scope. Never load an alternate DSO. */
  void *handle = wsattr_open_mapped_backend();
  if (handle) {
    *(void **)(&real_curl_ws_recv) = dlsym(handle, "curl_ws_recv");
    dlclose(handle);
  }
  if (!wsattr_is_impersonate_symbol(real_curl_ws_recv))
    *(void **)(&real_curl_ws_recv) = dlsym(RTLD_NEXT, "curl_ws_recv");
  if (!wsattr_is_impersonate_symbol(real_curl_ws_recv)) real_curl_ws_recv = NULL;
}

__attribute__((visibility("default"))) CURLcode curl_ws_recv(
    CURL *curl, void *buffer, size_t buflen, size_t *n, const struct curl_ws_frame **meta) {
  pthread_once(&curl_recv_once, wsattr_resolve_curl_recv);
  if (!real_curl_ws_recv) return CURLE_FAILED_INIT;
  wsattr_scope scope = wsattr_enter();
  CURLcode code = real_curl_ws_recv(curl, buffer, buflen, n, meta);
  uint64_t wall = 0, cpu = 0, end_tid = scope.tid;
  wsattr_leave(&scope, &wall, &cpu, &end_tid);
  wsattr_record_recv(&scope, code,
                     code == CURLE_OK && n ? *n : 0, wall, cpu, end_tid);
  return code;
}

typedef int (*wsattr_poll_fn)(struct pollfd *, nfds_t, int);
typedef int (*wsattr_ppoll_fn)(struct pollfd *, nfds_t, const struct timespec *, const sigset_t *);
typedef int (*wsattr_select_fn)(int, fd_set *, fd_set *, fd_set *, struct timeval *);
typedef int (*wsattr_epoll_fn)(int, struct epoll_event *, int, int);
static wsattr_poll_fn real_poll;
static wsattr_ppoll_fn real_ppoll;
static wsattr_select_fn real_select;
static wsattr_epoll_fn real_epoll_wait;
static pthread_once_t wait_resolve_once = PTHREAD_ONCE_INIT;

static void wsattr_resolve_waits(void) {
  *(void **)(&real_poll) = dlsym(RTLD_NEXT, "poll");
  *(void **)(&real_ppoll) = dlsym(RTLD_NEXT, "ppoll");
  *(void **)(&real_select) = dlsym(RTLD_NEXT, "select");
  *(void **)(&real_epoll_wait) = dlsym(RTLD_NEXT, "epoll_wait");
}

static void wsattr_wait_call_end(wsattr_scope *scope, int result, enum wsattr_wait_kind kind) {
  uint64_t wall = 0, cpu = 0, end_tid = scope->tid;
  wsattr_leave(scope, &wall, &cpu, &end_tid);
  wsattr_record_wait(scope, result, kind, wall, cpu, end_tid);
}

__attribute__((visibility("default"))) int poll(struct pollfd *fds, nfds_t nfds, int timeout) {
  pthread_once(&wait_resolve_once, wsattr_resolve_waits);
  if (!real_poll) { errno = ENOSYS; return -1; }
  wsattr_scope scope = wsattr_enter();
  int result = real_poll(fds, nfds, timeout), saved_errno = errno;
  wsattr_wait_call_end(&scope, result, WSATTR_POLL);
  errno = saved_errno;
  return result;
}

__attribute__((visibility("default"))) int ppoll(struct pollfd *fds, nfds_t nfds,
    const struct timespec *timeout, const sigset_t *sigmask) {
  pthread_once(&wait_resolve_once, wsattr_resolve_waits);
  if (!real_ppoll) { errno = ENOSYS; return -1; }
  wsattr_scope scope = wsattr_enter();
  int result = real_ppoll(fds, nfds, timeout, sigmask), saved_errno = errno;
  wsattr_wait_call_end(&scope, result, WSATTR_PPOLL);
  errno = saved_errno;
  return result;
}

__attribute__((visibility("default"))) int select(int nfds, fd_set *readfds,
    fd_set *writefds, fd_set *exceptfds, struct timeval *timeout) {
  pthread_once(&wait_resolve_once, wsattr_resolve_waits);
  if (!real_select) { errno = ENOSYS; return -1; }
  wsattr_scope scope = wsattr_enter();
  int result = real_select(nfds, readfds, writefds, exceptfds, timeout), saved_errno = errno;
  wsattr_wait_call_end(&scope, result, WSATTR_SELECT);
  errno = saved_errno;
  return result;
}

__attribute__((visibility("default"))) int epoll_wait(int epfd, struct epoll_event *events,
    int maxevents, int timeout) {
  pthread_once(&wait_resolve_once, wsattr_resolve_waits);
  if (!real_epoll_wait) { errno = ENOSYS; return -1; }
  wsattr_scope scope = wsattr_enter();
  int result = real_epoll_wait(epfd, events, maxevents, timeout), saved_errno = errno;
  wsattr_wait_call_end(&scope, result, WSATTR_EPOLL);
  errno = saved_errno;
  return result;
}
