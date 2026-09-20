# Bend response memory scaling

Recorded 2026-09-20T01:37:39.104713+00:00.

Median whole-process peak RSS over 3 shuffled runs. Linux/WSL2, one Bend compute thread.
Both modes consume identical HTTP/1 response bytes; download writes into a Linux temporary directory.

| Decoded body | Buffered RSS | Download RSS |
| --- | ---: | ---: |
| 0.0625 MiB | 4.2 MiB | 4.1 MiB |
| 1 MiB | 5.0 MiB | 4.1 MiB |
| 16 MiB | 20.1 MiB | 4.1 MiB |
| 64 MiB | 68.0 MiB | 4.2 MiB |

This checks observed memory scaling; it does not establish a process-wide memory bound.
Allocation accounting excludes backend internals and the Bend heap. Disk writing and buffering
produce different outputs, so this probe makes no throughput comparison.

Raw samples, versions and source hashes: [resources.json](results/resources.json).

```sh
.deps/venv-matched/bin/python benchmarks/resources.py --runs 3
```
