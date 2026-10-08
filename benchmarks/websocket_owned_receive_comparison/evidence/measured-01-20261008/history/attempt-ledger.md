# Preflight attempt history

Only the final campaign-03 snapshot is a released correctness smoke and performance campaign. Earlier outputs are retained separately here to explain why the final run used its exact frozen runner; they contain no workload results.

## Campaign 01

The first smoke attempt terminated during pre-build validation: `status.json` records `status=failed`, `builds_started=false`, and `attempts_started=false`. The wrapper intentionally replaced the initial smoke exception with a generic status message. A separate retained diagnostic reproduced the failure: the pinned Clang version string omitted its final newline while raw `clang --version` output included it. The live terminal output was not saved correctly; `terminal-capture-note.txt` records that limitation. The preserved command, durable status, and reproduction diagnostic are in this directory. No build, client, peer, or workload attempt occurred.

## Campaign 02

Campaign 02 was a reviewed source snapshot, not a released run. Its 20-test unit suite and Clang-version CLI integration check passed, but it was not authorized for a smoke, build, or workload campaign. Review then identified that the runner set a child-process Python bytecode prefix but had not set the current process `sys.pycache_prefix` before importing baseline checker helpers; this left possible pre-existing helper bytecode as a provenance gap. No Campaign-02 `status.json` or timing output exists, and none is reconstructed here. The archived PLAN/runner/test sources and the exact passing check logs/commands identify this version. Its source-pins hash was the same `5f891e15575c3390e71f9e84ced837697ee46453570858d74afdfffc80f557f7` used by Campaign 03.

## Campaign 03

Campaign 03 uses a fresh, run-local bytecode cache for the current process and child imports, records the five imported helper source and `.pyc` hashes, and rechecks them after execution. Its read-only preflight completed before the 45-attempt correctness smoke and final 474-attempt performance comparison. Campaign-03 hashes and logs are authoritative for the included results.
