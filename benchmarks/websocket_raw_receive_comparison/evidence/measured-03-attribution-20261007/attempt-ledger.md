# Perf-window proof attempt ledger

Attempts 01-07 are retained as development history and do not contribute to the successful window proof. The proof establishes that the isolated profiler interval can be started, stopped, and validated around one diagnostic client run; it is not a performance result.

| Attempt | Result | Evidence and interpretation |
| --- | --- | --- |
| 01 | Failed before build | The recorded prerequisite `go --version` probe exited with status 2. No capture window was produced. The exact manifest is retained under [`proof-attempts/01`](proof-attempts/01/). |
| 02 | Stopped before capture | The run was interrupted during matched-source/bytecode-cache provenance review after compilation; no WSS samples or perf window were retained. No output artifact was present at archive time. |
| 03 | Failed before build | The manifest records refusal to overwrite an existing diagnostic build directory (`FileExistsError`). No fresh capture window was produced. See [`proof-attempts/03`](proof-attempts/03/). |
| 04 | Stopped before capture | The compile preflight was held while pinning the explicit Go `GOAMD64` target; no capture artifact was retained. |
| 05 | Failed to start the interval | Build and binary provenance checks passed and the peer/perf recorder launched, but the client could not start the attribution interval. The client log is retained under [`proof-attempts/05`](proof-attempts/05/). The controller was corrected to consume the complete five-byte perf ACK (`ack\n\0`) rather than stopping at the newline. The cause of this attempt is identified from its protocol boundary and the retained [pinned-perf ACK probe](perf-ack-probe-20261007-02/manifest.json). |
| 06 | Workload ran; post-validation failed | The client, peer, and recorder completed, but expected-identity validation raised `KeyError: 'kind'`; this attempt is not accepted as a proof. See [`proof-attempts/06`](proof-attempts/06/). |
| 07 | Capture retained; initial validation failed | The initial parser reported no `cpu-clock:u` samples. Offline post-validation of the saved perf data confirmed five samples, all inside the 33.818 ms interval, with zero lost or outside-window samples. The original post-validation rejected the capture, so this attempt remains unqualified; no minimum sample-count threshold is claimed. The report and post-validation records are in [`proof-attempts/07`](proof-attempts/07/) and [`proof-attempts/07-postvalidation`](proof-attempts/07-postvalidation/). |
| 08 | Passed | The validated proof retained 10 samples, with zero lost or outside-window samples; the observed interval was 35.122 ms and all source, build, and toolchain rechecks passed. The complete artifact is under [`perf-window-proof-20261007-08`](perf-window-proof-20261007-08/). |

The first four rows follow the contemporaneous runner and operator records; attempts 02 and 04 had no artifact available to copy when this package was assembled. No missing log or manifest is reconstructed. All failed attempts are excluded from the 144-positive/24-control campaign and the 72-capture profile campaign.
