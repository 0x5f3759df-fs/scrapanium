# WebSocket operation invariants

The blocking C API and Bend's readiness callbacks drive the same operation
state machine in [`native/websocket.inc.c`](../native/websocket.inc.c).
The invariants below explain what an optimization must preserve. They are
review criteria backed by tests, not a formal verification claim.

| State | Required invariant |
| --- | --- |
| Ownership | One operation holds the socket gate from start through disposal. The Bend bridge keeps the socket and input buffer alive while parked. |
| Send | The offset counts bytes accepted by libcurl. A retry resumes at that offset with the same frame type; the input remains owned until completion. |
| Receive | Data accumulates in one bounded message buffer. Continuation frames and partial chunks cannot produce a successful partial message. |
| Control reply | Ping and close payloads remain in operation-owned storage while a reply is partially written. Fragment assembly survives an interleaved control frame. |
| Validation | Text is checked as complete UTF-8 after reassembly. Control lengths, close codes, close reasons and negotiated upgrade fields remain checked. |
| Deadline | Each operation has one monotonic deadline. Receiving fragments or pings does not restart it. Cancellation is checked on each step and within 50 ms of an idle wait. |
| Failure | An I/O failure marks the socket unusable. A partial message is freed, never returned as a successful complete message. Pre-I/O validation errors leave the socket usable. |
| Scheduling | Socket waits park the Bend activation. Bounded dispatches and completion yields let other live activations, including timers, progress. |
| Disposal | Completion releases the operation's cancellation reference and gate. Received bytes move to the caller only on success. Bend's `Ws.close` releases the socket on either outcome; native callers release it with `sp_ws_free`. |

Each socket contains one fixed operation-state structure. Start resets it only
after acquiring the gate, so a busy attempt cannot disturb a parked operation.
This eliminates one state allocation and free per operation; idle sockets retain
that fixed storage until the socket is freed. The address stays stable while parked.
Operation disposal frees any untransferred message, releases its cancellation
reference and clears ownership pointers before unlocking. It never accesses the
state after unlock. Returned message buffers have independent ownership: reusing
the operation cannot overwrite or free bytes already transferred to a caller.

The mutex requires start, step and disposal on the same OS thread. In Bend 2.0.27
(`63bee70b55a71024d6bdcb49a745111bc54b114e`), `io_loop` invokes `io_step`,
`io_wait` and their callbacks on its own thread; numeric reduction workers do
not run these effects. The runtime's `io_wait` uses `select` with descriptor sets
sized for the highest watched descriptor. Compiler upgrades must recheck
thread affinity, state ownership, and effect-handle lifetime contracts. The compatibility check exercised WSS receive,
cancellation and blocked-send paths with actual client sockets at FDs 1104–1106,
under one and four threads. See the
[compatibility report](compatibility/bend-2.0.27/README.md).

Receive buffers contain length-delimited bytes; they do not need a trailing NUL.
An announced frame remainder is checked against the message limit before it can
influence allocation. Read-ahead reservation is capped at 256 KiB. Larger or
fragmented messages grow geometrically as bytes arrive, and never beyond the
configured message limit. Control replies keep their own storage throughout
partial sends. This avoids excess allocation without trusting a peer's announced
length or returning incomplete messages.

When at least 16 KiB of owned capacity remains, the next receive writes directly
into that unused tail. The message length advances only after the frame metadata
passes validation. Interleaved control bytes move into separate control storage
without extending the message. If the announced remainder requires growth,
`realloc` preserves the bytes already received; the old pointer is never read
after growth. A fragmented-message sanitizer regression exercises that exact
transition with interleaved pings.

The test suite exercises native operations and compiled Bend programs with
AddressSanitizer and UndefinedBehaviorSanitizer. See
[validation coverage](VALIDATION.md) and [the public API semantics](WEBSOCKETS.md).
Performance evidence is separate: [benchmarks](../benchmarks/WEBSOCKET.md) use
exact-byte and opcode checks inside both clients' timed loops.
