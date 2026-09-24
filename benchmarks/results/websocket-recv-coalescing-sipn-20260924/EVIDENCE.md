# Retained evidence inventory

This directory contains the authoritative fixed run and a separate fixed receive-call probe. Raw records are copied from their original output directories; no data was moved or overwritten. The peer executable and TLS private key are not copied.

## Authoritative throughput run

- [Manifest and embedded plan, summary, environment, mapping, and provenance](manifest.json) — SHA256 `b5cd640a8122b4e6fa5196d8cc78be04f930fbe23eec085720cbdd1a8ae0851a`.
- [All 488 raw attempt records](samples.jsonl) — SHA256 `ae465c551d353a5d11aa6e201363a4dcc8dd0768443e9b2537266a82694a02c8`.
- Original persistent source directory: `/home/baidu/scrapanium-experiments/stream-full-sipn-20260924-persistent/`.
- The manifest records `hashes_verified_after_run=true`; source hashes, built-client hashes, backend mappings, and matched binding hashes are retained before and after the run.

## Receive-call probe

The one-run-per-cell probe transferred 64 MiB in each of eight rows. It supplements call-count evidence only; its interposed wall/CPU fields are not throughput samples.

- [Plan](probe/plan.json) — SHA256 `4cfb4dc6193f08451c578462f0fb22228e7a7775ab5f3af58836b4ff4a8b859f`.
- [Manifest](probe/manifest.json) — SHA256 `38e38aae4a2fd169d9cb0c5199b88f1bdce1d0c3043d70353e70d946203fc715`.
- [Eight raw rows](probe/samples.jsonl) — SHA256 `8becd944dab47b9fdc4ec7ee75c72b80757a5f46349bbc30debeeacce77fc8cb`.
- [Summary](probe/summary.json) — SHA256 `43bc116d39c9f11a25684eefe532d408f046cb7bd472a3cfac24bef2fd3e8c86`.

## Incomplete `/mnt/c` attempt

This attempt is retained separately and is not included in the throughput report. The manifest has 319 passed records (eight controls and 311 positives) and one failed attempt while replacing the manifest after the shared Windows mount returned `PermissionError`. No client sample failed; the schedule was incomplete.

- [Partial manifest](interrupted-mntc/manifest.json) — SHA256 `c34b9537433f441bc26549cddd2ff431f8b748ad7e8ea090c8357316110cba8f`.
- [319 partial records](interrupted-mntc/samples.jsonl) — SHA256 `c169fa54518099ffa09f97c80932f38b84d4972fcbb1c52f8899d375763f8266`.

A later `/tmp` run was observed to finish, but its files were lost when WSL's temporary filesystem reset before preservation. It contributes no quantitative evidence.

## Backend build provenance and frozen input identities

- [Baseline backend provenance](provenance/backend-baseline-provenance.json) — SHA256 `7d3d5dae0c19415f56959ca733ab2907fad20a8e8bcee215152fb867edde6d30`.
- [Candidate backend provenance](provenance/backend-candidate-provenance.json) — SHA256 `f25f7dcdb351c5bd987e3189f074301683385138ee866014dd9c69d17d519fe7`.
- Baseline/candidate backend libraries: `59460540f8fd6eab8eff22244e071c3132d7073f5be5a6dd9702b7c991315527` / `242162132dde4cc83a9fb081bbaf2d9f4c12a15873bfa4b5c80f13f8c022e287`.
- Baseline/candidate curl `ws.c`: `18ec1d509d44732fc94b26dbf8c5f39f3ddab32d0342df4fb20cc42e33726e80` / `9e67cfc98bd2e08de253fd9a09f57b06f1d6c5f5ab97780ac7e1da135946c058`.
- Baseline/candidate native WebSocket bridge: `0bc6766607ecfad0594d4a2df85b8247da678f4ab1cc0c9fca09504307082fb5` / `f051b39d89808f9db5a918471574db97a2e60dd4d601b256cc2714779e2de544`.
- Frozen four-condition runner SHA256: `0e13fef8779896d41f0249641a1cbfe1c2c7ce46952534fbd05a8d626fff9b6b`.
- Frozen eight-row probe helper SHA256: `dfe6896a38bc3f2ed6fca4e3051bcff4d62e81a919362d89fcf730929ef40287`.

The paired backend build is the optional browser-controls configuration, not the stock release backend used in earlier published matrices. The reproducible sources and test records are indexed from [the experiment inventory](../../websocket_recv_coalescing/README.md).

## Arithmetic audit

- [Read-only verifier and table generator](audit.py) — SHA256 `0ebad77848aa44959d8b20e11927e672925101e605a26db9a5b6f7960a96df45`.
- [Captured verifier output](audit-output.txt) — SHA256 `f75ef13b1396102f29dc23939acee5a168f8e02758797521b280f27288edfcea`.
- [Report](REPORT.md) — SHA256 `a9758e4ef4d5a5d61bcbbb99d604908570b8a4b1e0fa8fdf47e354fae46f4446`.

Run `python3 audit.py` from this directory to verify the retained hashes, schedule, row metadata, peer totals, backend mappings, frozen-input checks, and stored bootstrap intervals, then print the six-workload tables and gates.
