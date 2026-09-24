# Fresh reconstruction check

The documented preparation path completed in a new detached worktree at
`7b412fbaae907f4419b94085d0ae5bf81db94341`. The worktree and all generated
build/test artifacts were stored on WSL's persistent ext4 filesystem under
`/home/baidu/scrapanium-experiments/scrapanium-wss-recv-cold-20260924`; the
worktree is clean. Its experiment inputs were copied under ignored
`build/wss-recv-coalescing/` from the published checkout at
`26f036f0b7aaf20780ee250954146d0bc14e3a4f`.

The exact commands were:

```sh
git worktree add --detach /home/baidu/scrapanium-experiments/scrapanium-wss-recv-cold-20260924 7b412fbaae907f4419b94085d0ae5bf81db94341
mkdir -p /home/baidu/scrapanium-experiments/scrapanium-wss-recv-cold-20260924/build/wss-recv-coalescing
cp -a /mnt/c/Users/Baidu/Engrama/scrapanium/benchmarks/websocket_recv_coalescing/. /home/baidu/scrapanium-experiments/scrapanium-wss-recv-cold-20260924/build/wss-recv-coalescing/
cd /home/baidu/scrapanium-experiments/scrapanium-wss-recv-cold-20260924
python3 scripts/bootstrap.py --matched
python3 build/wss-recv-coalescing/prepare_pair.py
python3 build/wss-recv-coalescing/stream_four_condition.py --smoke --output-dir build/wss-recv-coalescing/stream-smoke-cold-20260924
```

Bootstrap and the full setup helper exited successfully. The system toolchain
was already installed on the WSL host: Python 3.14.4, Clang 21.1.8, CMake
4.2.3, Ninja 1.13.2, and Go 1.26.0. Bootstrap freshly installed the pinned
repository dependencies, including Bend, Bun, the matched curl_cffi binding,
and the test environment. This verifies the clean repository/worktree and
backend reconstruction path on that host; it does not verify installing a
clean operating system or system compiler toolchain.

The helper performed a fresh source build with identical browser-controls
inputs and one incremental curl source change. The baseline and candidate
snapshots contain 76 and 78 regular files respectively, with no symlinks, and
all files match their generated manifests. Static dependency-library hashes
matched before and after the candidate rebuild. The backend library hashes
from this fresh build were:

- Baseline: `ae88ce76f50c08423969f85170d65409cb273cd96af9092000a40d5ce8e32050`
- Candidate: `4a2477aa4de65f130d73e1eeaa75fcfdf47fdfa9088a4f696da67fcc16d902f1`

The helper created both client roots from the base revision, applied the
native bridge patch only to the candidate, and compiled each bridge and Bend
stream binary. `readelf` confirmed each binary's RUNPATH points to its own
backend snapshot. The exact generated hashes and all backend file hashes are
in `prepared-pair.json`, the two provenance files, and the baseline/candidate
backend manifests.

The correctness-only smoke completed all 32 rows: 24 positive checks and
eight corruption/swap controls, with zero failed rows. It reported
`hashes_verified_after_run: true`; it was not a performance run. The full
helper log, generated provenance, and smoke manifest/samples are retained next
to this report. No full timing experiment was repeated.

Evidence hashes:

- Helper source: `af1b8f6134ab11c08860ef0f5e27ddf44afa45f7ecbed17a0e081f8458adec52`
- Setup log: `732f209c9ace22d4ef9d9a2c52ad186b6a82d0542e6901af6ed028fae0d9c75f`
- Prepared pair: `37e17d6d17b8dba5fdd7ac6de6bd94eeaef6592b5768fe31fd21c5504e6fe858`
- Baseline provenance: `16d4a09a473d16da0020f0cb8172a7adb45c4e5caefc7d1b422d45e4eaecaa0c`
- Candidate provenance: `acc269ac783d07f28925c10ff63dd20f3bd73c5ea7b3a09fdff5778bd4c6aad9`
- Baseline backend manifest: `86ea0f8eef1de36eacafb14ca394724ac72558228161f87bb6c056d92a54250a`
- Candidate backend manifest: `ed018e18f987f8e31995ae6c569702ab8c46b96775bf99ec3ce410c366c49d7a`
- Smoke manifest: `03408500dc09611ce2bb8601e7a768f8b279c427ab5ee3a33d132ce06552d3f0`
- Smoke samples: `6c593fd0ddf8c9f315edd64f0522d6172101249e10581bd45e579c8287a51d81`
