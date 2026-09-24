/* Diagnostic-only absolute CLOCK_MONOTONIC boundaries for the WSS perf run.
 * Keep absolute nanoseconds in C: Bend Nat is a 48-bit immediate and cannot
 * safely hold CLOCK_MONOTONIC nanoseconds on long-uptime hosts. */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>

static uint64_t ws_client_perf_start_ns;
static uint64_t ws_client_perf_end_ns;
static int ws_client_perf_started;
static int ws_client_perf_ended;

static uint64_t ws_client_perf_now_ns(void) {
  struct timespec value;
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0 || value.tv_sec < 0 ||
      value.tv_nsec < 0 || value.tv_nsec >= 1000000000L)
    err_fail("client perf monotonic clock failed");
  return (uint64_t)value.tv_sec * UINT64_C(1000000000) +
         (uint64_t)value.tv_nsec;
}

static Term ws_client_perf_clock(Env e, Term *args, IoWork *work) {
  (void)work;
  switch ((uint64_t)args[0]) {
    case 0: /* Immediately before sending the peer's start control frame. */
      if (ws_client_perf_started || ws_client_perf_ended)
        err_fail("client perf interval started more than once");
      ws_client_perf_start_ns = ws_client_perf_now_ns();
      ws_client_perf_started = 1;
      return nat_chk(e, 0);
    case 1: { /* Immediately after the last exact check and buffer release. */
      if (!ws_client_perf_started || ws_client_perf_ended)
        err_fail("client perf interval ended in an invalid state");
      ws_client_perf_end_ns = ws_client_perf_now_ns();
      if (ws_client_perf_end_ns < ws_client_perf_start_ns)
        err_fail("client perf monotonic clock regressed");
      ws_client_perf_ended = 1;
      return nat_chk(e, (ws_client_perf_end_ns - ws_client_perf_start_ns) /
                            UINT64_C(1000));
    }
    case 2: /* Serialize only after the measured interval has stopped. */
      if (!ws_client_perf_ended)
        err_fail("client perf interval was not completed");
      if (printf("WSS_PERF_INTERVAL_NS=%" PRIu64 ",%" PRIu64 "\n",
                 ws_client_perf_start_ns, ws_client_perf_end_ns) < 0 ||
          fflush(stdout) != 0)
        err_fail("cannot emit client perf interval");
      return nat_chk(e, 0);
    default:
      err_fail("invalid client perf clock event");
  }
  return nat_chk(e, 0);
}

static void __attribute__((constructor)) ws_client_perf_clock_register(void) {
  io_eff(CID_WSCLIENTPERF_CLOCK, ws_client_perf_clock, 0);
}
