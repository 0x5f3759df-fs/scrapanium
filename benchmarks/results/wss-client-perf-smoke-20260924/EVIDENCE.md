# Evidence inventory

This archive contains the exact smoke manifest.json, plan.json, samples.jsonl,
and all 48 per-attempt directories, including each raw perf.data, perf
header/report/script output, control transcript, client and peer output, and
row record. first-framing-failure/ preserves the first failed attempt, its
original plan/manifest/row, and the exact runner source captured by that attempt.

The copied successful-run files are byte-identical to the persistent source
directory /home/baidu/scrapanium-experiments/wss-client-perf-smoke-20260924-nul-ack-final:

- manifest.json: 038b4060cbfa2f91a184a337fb672a524d8681110412306319a69d390019615c
- plan.json: f3abff61626e4898d4978094f521b568bdac4474ba57fee7e3c4724018c11ed0
- samples.jsonl: b7f11087ba5356547a15fdcc4c00009ad82f397ac41afaea7e7d494fe19a38ae

The failed attempt's retained runner source SHA-256 is
2d876b254d75293f87ec13bb90c3d6e7fd0ce4fa62c731e5dd63a25078fdaaa3.
Its startup error and control transcript hashes are respectively
c9963e57831882ac0d5e02911528a2ee63e29d59c90202776f32f756e9392668 and
94d97c4095d842fe2430a9401eaebd268308724aa8d82cfd2f77447c3dd5203f.
The corrected runner SHA-256 recorded by the successful manifest is
f4363ca989e6aefe681cd30f28f01dbd449fcc02c1031ebe95cabf440f7d6595.

SHA256SUMS.txt records the archive's raw evidence and report hashes, excluding
itself and this inventory page. The archive omits generated executables,
libraries, dependency/package blobs, the message corpus copy, and private keys.
The successful manifest retains their relevant source/tool/binary hashes and
records that the temporary TLS private key was not retained.
