# Local benchmark results

Recorded 2026-09-20T01:37:27.794090+00:00. CPU: 13th Gen Intel(R) Core(TM) i5-13420H.
Platform: Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.43. 5 shuffled runs per case; medians below.

Values are requests/second, higher is better. All clients use persistent sessions,
one warmup request, Chrome 146 TLS profiles, common headers and certificate verification.
Bend runs with one compute thread; batches permit 16 concurrent requests.

| Workload | scrapanium-bend | scrapanium-c | curl_cffi-release | curl_cffi-matched | tls-client-go |
| --- | ---: | ---: | ---: | ---: | ---: |
| http1-small | 3,636 | 4,757 | 2,052 | 2,042 | 4,158 |
| http1-64KiB | 2,525 | 3,536 | 1,874 | 1,893 | 2,461 |
| https1-small | 2,551 | 4,130 | 1,995 | 1,942 | 3,062 |
| https2-small | 1,972 | 2,588 | 1,550 | 1,510 | 2,139 |
| http1-batch16-delay10ms | 1,438 | 1,443 | 1,309 | 1,308 | 1,403 |
| https2-batch16 | 6,452 | 6,453 | 5,012 | 4,996 | 5,017 |

## Bend versus the same-backend curl_cffi

Ratios compare throughput, not single-request latency.

| Workload | Ratio |
| --- | ---: |
| http1-small | 1.78x |
| http1-64KiB | 1.33x |
| https1-small | 1.31x |
| https2-small | 1.31x |
| http1-batch16-delay10ms | 1.10x |
| https2-batch16 | 1.29x |

## Whole-process peak memory

Median peak resident memory in MiB. Includes startup, imports, warmup and teardown.

| Workload | scrapanium-bend | scrapanium-c | curl_cffi-release | curl_cffi-matched | tls-client-go |
| --- | ---: | ---: | ---: | ---: | ---: |
| http1-small | 4.1 | 3.6 | 31.9 | 32.2 | 14.8 |
| http1-64KiB | 4.2 | 3.8 | 32.0 | 32.0 | 15.4 |
| https1-small | 5.6 | 5.0 | 33.4 | 33.5 | 15.9 |
| https2-small | 5.9 | 5.4 | 33.7 | 33.7 | 16.4 |
| http1-batch16-delay10ms | 6.5 | 5.8 | 34.2 | 33.0 | 14.3 |
| https2-batch16 | 14.7 | 13.7 | 38.9 | 37.8 | 18.8 |

## Interpretation and limits

- This run supersedes the initial alpha measurements: the Bend configuration bridge now decodes flattened records and verification flags correctly.
- Loopback Python servers may limit throughput.
- Bend timer resolution is 1 ms.
- Buffers consumed/discarded; no text/JSON decoding.
- Batch retains all responses until completion.
- CPU and peak RSS cover the entire client process, including startup and warmup, unlike throughput timing.
- Startup and session construction excluded; first TLS handshake excluded by warmup.
- Local Python fixtures can cap throughput; these results do not establish Internet performance.
- HTTP/1 cleartext upgrade behavior differs between curl and Go; the wire streams are not identical.
- TLS-client has a different transport and feature/copying model; its values are a separate implementation comparison.
- No p95/p99 per-request latency or cold-handshake claim is supported by this run.
- Whole-process CPU seconds and peak RSS are recorded; they do not isolate transport CPU or buffer memory.
- Source and raw min/max/repeated timings are retained in [local.json](results/local.json).

## Versions

```text
Bend 2.0.17 (see dependencies.json)
curl_cffi release: 0.16.0
libcurl/8.21.0-IMPERSONATE BoringSSL zlib/1.3.1 brotli/1.2.0 zstd/1.5.7 libidn2/2.3.7 nghttp2/1.63.0 ngtcp2/1.20.0 nghttp3/1.15.0
curl_cffi matched: 0.16.4b1
libcurl/8.22.0-IMPERSONATE BoringSSL zlib/1.3.1 brotli/1.2.0 zstd/1.5.7 c-ares/1.34.8 libidn2/2.3.7 nghttp2/1.63.0 ngtcp2/1.20.0 nghttp3/1.15.0 curl-impersonate/2.2.3
go version go1.26.0 linux/amd64
tls-client: 34718e1b514b446b95bc68dc4f096247e69c7939
```

Reproduce after completing tests and other builds:

```sh
.deps/venv-matched/bin/python benchmarks/run.py --runs 5 --count 1000 --tls-client
python3 benchmarks/report.py
```
