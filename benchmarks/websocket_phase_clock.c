/* Diagnostic-only nanosecond phase clock for the Bend WebSocket client.
 * Absolute monotonic values stay in uint64_t here; only bounded deltas cross
 * Bend's 48-bit Nat boundary. The process handles one socket on one thread. */
#include <stdint.h>
#include <time.h>

static uint64_t ws_phase_total_start_ns;
static uint64_t ws_phase_total_ns;
static uint64_t ws_phase_total_start_us;
static uint64_t ws_phase_total_end_us;
static uint64_t ws_phase_receive_start_ns;
static uint64_t ws_phase_receive_ns;
static uint64_t ws_phase_validation_start_ns;
static uint64_t ws_phase_validation_ns;
static unsigned ws_phase_active;
static int ws_phase_total_started;

static uint64_t ws_phase_now_ns(void) {
  struct timespec value;
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0)
    err_fail("diagnostic monotonic clock failed");
  return (uint64_t)value.tv_sec * UINT64_C(1000000000) + (uint64_t)value.tv_nsec;
}

static Term ws_phase_event(Env e, Term *args, IoWork *work) {
  (void)work;
  uint64_t event = (uint64_t)args[0];
  if (event == 6) return nat_chk(e, ws_phase_receive_ns);
  if (event == 7) return nat_chk(e, ws_phase_validation_ns);
  if (event == 8) return nat_chk(e, ws_phase_total_start_us);
  if (event == 9) return nat_chk(e, ws_phase_total_end_us);
  uint64_t now = ws_phase_now_ns();
  switch (event) {
    case 0: /* Reset phase totals and begin the total interval. */
      if (ws_phase_total_started || ws_phase_active)
        err_fail("invalid diagnostic total timing state");
      ws_phase_receive_ns = 0;
      ws_phase_validation_ns = 0;
      ws_phase_active = 0;
      ws_phase_total_start_ns = now;
      ws_phase_total_start_us = now / 1000;
      ws_phase_total_end_us = 0;
      ws_phase_total_started = 1;
      return nat_chk(e, 0);
    case 1: /* End the total interval and return its duration. */
      if (!ws_phase_total_started || ws_phase_active)
        err_fail("invalid diagnostic timing state");
      if (now < ws_phase_total_start_ns)
        err_fail("diagnostic monotonic clock regressed");
      ws_phase_total_ns = now - ws_phase_total_start_ns;
      ws_phase_total_end_us = now / 1000;
      ws_phase_total_started = 0;
      return nat_chk(e, ws_phase_total_ns);
    case 2: /* Begin receive/assembly interval. */
      if (!ws_phase_total_started || ws_phase_active)
        err_fail("invalid diagnostic receive timing state");
      ws_phase_receive_start_ns = now;
      ws_phase_active = 1;
      return nat_chk(e, 0);
    case 3: /* End receive/assembly interval. */
      if (ws_phase_active != 1)
        err_fail("invalid diagnostic receive timing state");
      if (now < ws_phase_receive_start_ns ||
          UINT64_MAX - ws_phase_receive_ns < now - ws_phase_receive_start_ns)
        err_fail("diagnostic receive clock regressed or overflowed");
      ws_phase_receive_ns += now - ws_phase_receive_start_ns;
      ws_phase_active = 0;
      return nat_chk(e, 0);
    case 4: /* Begin validation/release interval. */
      if (!ws_phase_total_started || ws_phase_active)
        err_fail("invalid diagnostic validation timing state");
      ws_phase_validation_start_ns = now;
      ws_phase_active = 2;
      return nat_chk(e, 0);
    case 5: /* End validation/release interval. */
      if (ws_phase_active != 2)
        err_fail("invalid diagnostic validation timing state");
      if (now < ws_phase_validation_start_ns ||
          UINT64_MAX - ws_phase_validation_ns < now - ws_phase_validation_start_ns)
        err_fail("diagnostic validation clock regressed or overflowed");
      ws_phase_validation_ns += now - ws_phase_validation_start_ns;
      ws_phase_active = 0;
      return nat_chk(e, 0);
    default:
      err_fail("invalid diagnostic timing event");
  }
  return nat_chk(e, 0);
}

static void __attribute__((constructor)) ws_phase_clock_register(void) {
  io_eff(CID_WSPHASE_EVENT, ws_phase_event, 0);
}
