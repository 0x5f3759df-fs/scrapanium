/* Bend effect bridge to the diagnostic-only, LD_PRELOAD attribution shim. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>

typedef int (*wsattr_begin_fn)(int);
typedef uint64_t (*wsattr_end_fn)(void);

static wsattr_begin_fn wsattr_begin_call;
static wsattr_end_fn wsattr_end_call;
static pthread_once_t wsattr_resolve_once = PTHREAD_ONCE_INIT;

static void wsattr_resolve(void) {
  *(void **)(&wsattr_begin_call) = dlsym(RTLD_DEFAULT, "wsattr_begin");
  *(void **)(&wsattr_end_call) = dlsym(RTLD_DEFAULT, "wsattr_end");
}

static Term wsattr_event(Env e, Term *args, IoWork *work) {
  (void)work;
  pthread_once(&wsattr_resolve_once, wsattr_resolve);
  if (!wsattr_begin_call || !wsattr_end_call)
    err_fail("WebSocket attribution shim is unavailable");
  switch ((uint64_t)args[0]) {
    case 0:
      if (!wsattr_begin_call(0)) err_fail("cannot start control attribution interval");
      return nat_chk(e, 0);
    case 1:
      if (!wsattr_begin_call(1)) err_fail("cannot start timed attribution interval");
      return nat_chk(e, 0);
    case 2: {
      uint64_t elapsed = wsattr_end_call();
      if (!elapsed) err_fail("cannot end WebSocket attribution interval");
      return nat_chk(e, elapsed);
    }
    default:
      err_fail("invalid WebSocket attribution event");
  }
  return nat_chk(e, 0);
}

static void __attribute__((constructor)) wsattr_clock_register(void) {
  io_eff(CID_WSATTR_EVENT, wsattr_event, 0);
}
