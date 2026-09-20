/* Benchmark-only timing. Audited against Bend b2791abb: Nat is a 48-bit
 * immediate; nat_chk preserves the U64 value or reports overflow. io_tick
 * reads CLOCK_MONOTONIC, the same domain as Python time.monotonic_ns(). */
static Term ws_bench_now_us(Env e, Term *fields, IoWork *work) {
  (void)fields; (void)work;
  return nat_chk(e, io_tick() / 1000ull);
}
static void __attribute__((constructor)) ws_bench_clock_register(void) {
  io_eff(CID_WSBENCH_NOW_US, ws_bench_now_us, 0);
}
