#!/usr/bin/env python3
"""Small adversarial WS/WSS parser corpus for the c492717 public native API."""
from __future__ import annotations
import argparse
import base64
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LIBPATH = HERE / "libscrapanium.so"
sys.path.insert(0, str(ROOT / "tests"))
from lab import certificate


class Fingerprint(C.Structure):
    _fields_ = [(x, C.c_char_p) for x in (
        "ciphers", "curves", "signature_algorithms", "extension_order",
        "http2_settings", "pseudo_header_order", "cert_compression"
    )] + [("grease", C.c_int), ("permute_extensions", C.c_int),
         ("http2_window_update", C.c_uint32)]


class Config(C.Structure):
    _fields_ = [(x, C.c_char_p) for x in ("profile", "proxy", "ca_bundle")] + [
        (x, C.c_uint32) for x in (
            "timeout_ms", "connect_timeout_ms", "max_redirects", "concurrency"
        )
    ] + [(x, C.c_size_t) for x in ("max_body_bytes", "max_header_bytes")] + [
        (x, C.c_int) for x in ("verify", "follow_redirects", "default_headers")
    ] + [("fingerprint", Fingerprint)] + [
        (x, C.c_size_t) for x in ("max_batch_bytes", "max_batch_requests")
    ]


class Request(C.Structure):
    _fields_ = [("method", C.c_char_p), ("url", C.c_char_p),
                ("headers", C.POINTER(C.c_char_p)), ("header_count", C.c_size_t),
                ("body", C.c_void_p), ("body_size", C.c_size_t)]


lib = C.CDLL(str(LIBPATH))
lib.sp_config_default.argtypes = [C.POINTER(Config)]
lib.sp_session_new.argtypes = [C.POINTER(Config), C.POINTER(C.c_int)]
lib.sp_session_new.restype = C.c_void_p
lib.sp_ws_upgrade.argtypes = [C.c_void_p, C.POINTER(Request), C.c_void_p, C.POINTER(C.c_int)]
lib.sp_ws_upgrade.restype = C.c_void_p
lib.sp_ws_free.argtypes = [C.c_void_p]
lib.sp_ws_receive.argtypes = [
    C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_void_p),
    C.POINTER(C.c_size_t), C.c_uint32, C.c_void_p
]
lib.sp_ws_receive.restype = C.c_int
lib.sp_ws_send.argtypes = [
    C.c_void_p, C.c_uint, C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p
]
lib.sp_ws_send.restype = C.c_int
lib.sp_ws_close.argtypes = [
    C.c_void_p, C.c_uint, C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p
]
lib.sp_ws_close.restype = C.c_int
lib.sp_error_message.argtypes = [C.c_int]
lib.sp_error_message.restype = C.c_char_p
lib.sp_backend_version.restype = C.c_char_p
libc = C.CDLL(None)
libc.free.argtypes = [C.c_void_p]


def c_error(code: int) -> str:
    value = lib.sp_error_message(code)
    return value.decode("utf-8", "replace") if value else "unknown"


class Client:
    def __init__(self, url: str, ca_path: str):
        cfg = Config()
        lib.sp_config_default(C.byref(cfg))
        cfg.profile = b"chrome150"
        cfg.ca_bundle = ca_path.encode()
        cfg.timeout_ms = 2000
        cfg.connect_timeout_ms = 1000
        cfg.max_redirects = 0
        cfg.concurrency = 1
        cfg.max_body_bytes = 256
        err = C.c_int()
        session = lib.sp_session_new(C.byref(cfg), C.byref(err))
        if not session:
            raise RuntimeError(f"sp_session_new error={err.value}:{c_error(err.value)}")
        requrl = url.encode()
        headers = (C.c_char_p * 0)()
        req = Request(b"GET", requrl, headers, 0, None, 0)
        self.ptr = lib.sp_ws_upgrade(session, C.byref(req), None, C.byref(err))
        self.upgrade_error = err.value
        if not self.ptr:
            self.upgrade_message = c_error(err.value)
        else:
            self.upgrade_message = "success"

    def recv(self, timeout_ms: int):
        kind, data, size = C.c_uint(), C.c_void_p(), C.c_size_t()
        err = lib.sp_ws_receive(self.ptr, C.byref(kind), C.byref(data),
                                C.byref(size), timeout_ms, None)
        try:
            payload = C.string_at(data, size.value) if data and size.value else b""
        finally:
            if data:
                libc.free(data)
        return err, kind.value, payload

    def send(self, payload: bytes, timeout_ms: int = 300):
        buf = C.create_string_buffer(payload) if payload else None
        return lib.sp_ws_send(self.ptr, 2, C.cast(buf, C.c_void_p) if buf else None,
                              len(payload), timeout_ms, None)

    def close(self):
        if self.ptr:
            lib.sp_ws_free(self.ptr)
            self.ptr = None


def ws_frame(opcode: int, payload: bytes = b"", *, fin: bool = True,
             rsv: int = 0, masked: bool = False, mode: str = "minimal") -> bytes:
    first = (0x80 if fin else 0) | rsv | opcode
    n = len(payload)
    if mode == "126":
        head = bytes((first, 126)) + struct.pack("!H", n)
    elif mode == "127":
        head = bytes((first, 127)) + struct.pack("!Q", n)
    elif mode == "127-high-bit":
        head = bytes((first, 127)) + bytes((0x80,)) + b"\0" * 7
    else:
        if n < 126:
            head = bytes((first, n))
        elif n <= 0xffff:
            head = bytes((first, 126)) + struct.pack("!H", n)
        else:
            head = bytes((first, 127)) + struct.pack("!Q", n)
    if not masked:
        return head + payload
    key = b"\x13\x37\xaa\x55"
    coded = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
    return bytes((first, head[1] | 0x80)) + head[2:] + key + coded


def cases():
    out = []
    for bit, name in ((0x40, "rsv1"), (0x20, "rsv2"), (0x10, "rsv3")):
        out.append({"name": name, "wire": ws_frame(2, b"x", rsv=bit)})
    out.extend([
        {"name": "reserved-opcode-3", "wire": ws_frame(3, b"x")},
        {"name": "reserved-opcode-b", "wire": ws_frame(11, b"x")},
        {"name": "masked-server-frame", "wire": ws_frame(2, b"x", masked=True)},
        {"name": "noncanonical-126-for-125",
         "wire": ws_frame(2, b"a" * 125, mode="126")},
        {"name": "noncanonical-127-for-126",
         "wire": ws_frame(2, b"b" * 126, mode="127")},
        {"name": "127-length-high-bit",
         "wire": ws_frame(2, b"", mode="127-high-bit")},
        {"name": "unexpected-continuation", "wire": ws_frame(0, b"x")},
        {"name": "new-data-before-fragment-end",
         "wire": ws_frame(2, b"first", fin=False) + ws_frame(2, b"second")},
        {"name": "fragmented-ping", "wire": ws_frame(9, b"p", fin=False)},
        {"name": "oversize-ping",
         "wire": b"\x89\x7e\x00\x7e" + b"p" * 126},
        {"name": "truncated-one-byte-header", "wire": b"\x82",
         "eof_after": True},
        {"name": "truncated-extended-header", "wire": b"\x82\x7e\x00",
         "eof_after": True},
        {"name": "truncated-payload", "wire": b"\x82\x04ab",
         "eof_after": True},
        {"name": "announced-body-over-limit",
         # Canonical 16-bit length 257 plus one byte: independently decisive
         # of the noncanonical-length cases while the peer remains open.
         "wire": b"\x82\x7e\x01\x01x",
         "hold_seconds": 1.0, "expect_fast_limit": True},
    ])
    ping = ws_frame(9, b"probe")
    expected = bytes((i * 37 + 11) & 0xff for i in range(126))
    message = ws_frame(2, expected)
    wire = ping + message
    out.append({
        "name": "valid-canonical-control-and-data",
        "wire": wire,
        "segments": [wire[:1], wire[1:3], wire[3:]],
        "valid": True,
        "expected": expected,
        "pong": b"probe",
    })
    assert len(out) == 18
    return out


def recv_exact(sock, n: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("peer frame read deadline")
        if isinstance(sock, ssl.SSLSocket) and sock.pending() > 0:
            ready = [sock]
        else:
            ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            raise TimeoutError("peer frame read deadline")
        try:
            part = sock.recv(n - len(chunks))
        except (ssl.SSLWantReadError, BlockingIOError):
            continue
        if not part:
            raise EOFError("client closed during frame")
        chunks.extend(part)
    return bytes(chunks)


def read_client_frame(sock, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    first, second = recv_exact(sock, 2, deadline)
    fin = bool(first & 0x80)
    opcode = first & 0x0f
    masked = bool(second & 0x80)
    n = second & 0x7f
    if n == 126:
        n = struct.unpack("!H", recv_exact(sock, 2, deadline))[0]
    elif n == 127:
        n = struct.unpack("!Q", recv_exact(sock, 8, deadline))[0]
    if n > 1024 * 1024:
        raise ValueError(f"client frame too large: {n}")
    mask = recv_exact(sock, 4, deadline) if masked else b""
    payload = recv_exact(sock, n, deadline) if n else b""
    if masked:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return {"opcode": opcode, "fin": fin, "masked": masked,
            "payload_hex": payload.hex()}


def handshake(sock) -> tuple[str, str]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(4096)
        if not part:
            raise EOFError("client closed before upgrade")
        data.extend(part)
        if len(data) > 65536:
            raise ValueError("oversized HTTP request headers")
    lines = bytes(data).split(b"\r\n")
    request_parts = lines[0].split()
    if len(request_parts) < 2:
        raise ValueError("malformed HTTP request line")
    path = request_parts[1].decode("ascii", "strict")
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.strip().lower()] = value.strip()
    key = headers.get(b"sec-websocket-key")
    if not key:
        raise ValueError("missing Sec-WebSocket-Key")
    accept = base64.b64encode(hashlib.sha1(
        key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    ).digest())
    response = (b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
    return response.decode("ascii"), path


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        record = {"client_frames": [], "peer_error": None,
                  "tls_version": None, "tls_cipher": None}
        sock = self.request
        sock.settimeout(2.0)
        try:
            if isinstance(sock, ssl.SSLSocket):
                record["tls_version"] = sock.version()
                cipher = sock.cipher()
                record["tls_cipher"] = cipher[0] if cipher else None
            response_text, path = handshake(sock)
            case_name = urlsplit(path).path.lstrip("/")
            self.case = self.server.case_map[case_name]
            record["case"] = case_name
            self.server.records[case_name] = record
            response = response_text.encode("ascii")
            segments = self.case.get("segments", [self.case["wire"]])
            # Coalesce 101 and first WS bytes in one TLS/TCP write.
            sock.sendall(response + segments[0])
            for segment in segments[1:]:
                time.sleep(0.005)
                sock.sendall(segment)
            if self.case.get("eof_after"):
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            if self.case.get("valid"):
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    item = read_client_frame(sock, deadline - time.monotonic())
                    record["client_frames"].append(item)
                    if item["opcode"] == 8:
                        # Echo the valid close and then let the peer finish.
                        payload = bytes.fromhex(item["payload_hex"])
                        sock.sendall(ws_frame(8, payload))
                        break
            else:
                # Keep the peer alive after complete malformed frames, and
                # longer than the receive deadline for the body-limit header.
                hold = self.case.get("hold_seconds", 1.20)
                deadline = time.monotonic() + hold
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([sock], [], [],
                                                min(0.025, deadline - time.monotonic()))
                    if not ready:
                        continue
                    try:
                        record["client_frames"].append(read_client_frame(
                            sock, min(0.15, deadline - time.monotonic())))
                    except (EOFError, OSError, ssl.SSLError, TimeoutError, ValueError):
                        break
        except (EOFError, OSError, ssl.SSLError, TimeoutError, ValueError) as exc:
            record["peer_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                sock.close()
            except OSError:
                pass
            case_name = record.get("case")
            if case_name in self.server.events:
                self.server.events[case_name].set()


class Peer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, context, case_map):
        super().__init__(("127.0.0.1", 0), Handler)
        self.context = context
        self.case_map = case_map
        self.records = {}
        self.events = {name: threading.Event() for name in case_map}

    def get_request(self):
        sock, addr = super().get_request()
        if self.context:
            sock = self.context.wrap_socket(sock, server_side=True)
        return sock, addr


def eval_attempt(case: dict, protocol: str, peer: Peer, ca_path: str) -> dict:
    start = time.monotonic()
    url = f"{protocol}://127.0.0.1:{peer.server_address[1]}/{case['name']}"
    client = None
    item = {"protocol": protocol, "case": case["name"], "url_path": case["name"],
            "upgrade_error": None, "recv_error": None, "recv_message": None,
            "recv_kind": None, "recv_payload_hex": None, "recv_elapsed_ms": None,
            "send_after_error": None, "close_result": None, "pass": False}
    try:
        client = Client(url, ca_path)
        item["upgrade_error"] = client.upgrade_error
        item["upgrade_message"] = client.upgrade_message
        if not client.ptr:
            return item
        t0 = time.monotonic()
        err, kind, payload = client.recv(750)
        elapsed = time.monotonic() - t0
        item["recv_error"] = err
        item["recv_message"] = c_error(err)
        item["recv_kind"] = kind
        item["recv_payload_hex"] = payload.hex()
        item["recv_elapsed_ms"] = round(elapsed * 1000, 3)
        if case.get("valid"):
            if err == 0:
                # Send a normal close only after full binary message validation.
                reason = b""
                reason_buf = C.create_string_buffer(reason) if reason else None
                close_error = lib.sp_ws_close(client.ptr, 1000,
                    C.cast(reason_buf, C.c_void_p) if reason_buf else None,
                    0, 500, None)
                item["close_result"] = close_error
                item["pass"] = (
                    kind == 2 and payload == case["expected"] and close_error == 0
                )
        else:
            data = b"must-not-send"
            buf = C.create_string_buffer(data)
            send_error = lib.sp_ws_send(client.ptr, 2, C.cast(buf, C.c_void_p),
                                        len(data), 250, None)
            item["send_after_error"] = send_error
            # No malformed or partial message may be delivered as success;
            # all these peers are still open for the rejection check.
            item["pass"] = (
                err not in (0, 14) and not payload and send_error != 0
            )
            if case.get("expect_fast_limit"):
                item["pass"] = item["pass"] and err == 5
    except Exception as exc:
        item["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client and client.ptr:
            client.close()
        elapsed_total = time.monotonic() - start
        item["attempt_elapsed_ms"] = round(elapsed_total * 1000, 3)
        peer.events[case["name"]].wait(2.5)
        item["peer"] = peer.records.get(case["name"])
        if case.get("valid"):
            frames = (item["peer"] or {}).get("client_frames", [])
            item["pass"] = item["pass"] and len(frames) == 2 and [
                (f["opcode"], f["payload_hex"], f["masked"], f["fin"])
                for f in frames
            ] == [
                (10, case["pong"].hex(), True, True),
                (8, b"\x03\xe8".hex(), True, True),
            ]
        else:
            frames = (item["peer"] or {}).get("client_frames", [])
            no_data_or_pong = all(f["opcode"] not in (1, 2, 10) for f in frames)
            item["pass"] = item["pass"] and no_data_or_pong
    return item


def mapped_backend_path() -> Path:
    paths = set()
    with open("/proc/self/maps", "r", encoding="utf-8") as maps:
        for line in maps:
            if "libcurl-impersonate.so" not in line:
                continue
            parts = line.rstrip().split()
            if len(parts) >= 6 and parts[-1].startswith("/"):
                paths.add(str(Path(parts[-1]).resolve()))
    if len(paths) != 1:
        raise RuntimeError(f"expected one mapped curl DSO, found {sorted(paths)}")
    return Path(next(iter(paths)))


def inventory() -> dict:
    stock = (ROOT / ".deps/curl/libcurl-impersonate.so.4.8.0").resolve()
    mapped = mapped_backend_path()
    return {
        "source_commit": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip(),
        "native_scrapanium_c_sha256": hashlib.sha256(
            (ROOT / "native/scrapanium.c").read_bytes()
        ).hexdigest(),
        "native_websocket_inc_c_sha256": hashlib.sha256(
            (ROOT / "native/websocket.inc.c").read_bytes()
        ).hexdigest(),
        "bridge_path": str(LIBPATH.resolve()),
        "bridge_sha256": hashlib.sha256(LIBPATH.read_bytes()).hexdigest(),
        "expected_stock_backend_path": str(stock),
        "mapped_backend_path": str(mapped),
        "mapped_backend_sha256": hashlib.sha256(mapped.read_bytes()).hexdigest(),
        "stock_backend_sha256": hashlib.sha256(stock.read_bytes()).hexdigest(),
        "backend_version": lib.sp_backend_version().decode("utf-8", "replace"),
        "profile": "chrome150",
        "max_body_bytes": 256,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True,
                        help="new summary JSON path; a sibling .jsonl retains every attempt")
    args = parser.parse_args()
    output_path = args.output.resolve()
    rows_path = output_path.with_suffix(".jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or rows_path.exists():
        raise FileExistsError(f"refusing to overwrite {output_path} or {rows_path}")
    pre = inventory()
    if Path(pre["mapped_backend_path"]) != Path(pre["expected_stock_backend_path"]):
        raise RuntimeError(
            "mapped backend is not the resolved stock backend: "
            f"{pre['mapped_backend_path']} != {pre['expected_stock_backend_path']}"
        )
    if "curl-impersonate/2.2.3" not in pre["backend_version"]:
        raise RuntimeError(f"unexpected backend version: {pre['backend_version']}")
    plan = cases()
    protocol_results = []
    with rows_path.open("x", encoding="utf-8", newline="\n") as rows_file:
        with tempfile.TemporaryDirectory(prefix="wss-adversarial-") as tmp:
            context, ca_path = certificate(Path(tmp) / "cert")
            cert_hash = hashlib.sha256(Path(ca_path).read_bytes()).hexdigest()
            context.set_alpn_protocols(["http/1.1"])
            for protocol, tls_context in (("ws", None), ("wss", context)):
                peer = Peer(tls_context, {x["name"]: x for x in plan})
                thread = threading.Thread(target=peer.serve_forever, daemon=True)
                thread.start()
                try:
                    for case in plan:
                        row = eval_attempt(case, protocol, peer, ca_path)
                        protocol_results.append(row)
                        rows_file.write(json.dumps(row, sort_keys=True) + "\n")
                        rows_file.flush()
                finally:
                    peer.shutdown()
                    peer.server_close()
                    thread.join(timeout=2)
    post_error = None
    post = None
    try:
        post = inventory()
    except Exception as exc:
        post_error = f"{type(exc).__name__}: {exc}"
    failures = [x for x in protocol_results if not x["pass"]]
    hashes_stable = post is not None and all(
        pre[key] == post[key] for key in (
            "source_commit", "native_scrapanium_c_sha256",
            "native_websocket_inc_c_sha256", "bridge_sha256",
            "mapped_backend_path", "mapped_backend_sha256", "stock_backend_sha256",
            "backend_version"
        )
    )
    output = {
        "schema": "scrapanium-ws-adversarial-v1",
        **pre,
        "certificate_sha256": cert_hash,
        "protocols": ["ws", "wss"],
        "planned_case_count": len(plan),
        "attempt_count": len(protocol_results),
        "passed": len(protocol_results) - len(failures),
        "failed": len(failures),
        "hashes_stable_after": hashes_stable,
        "post_run_inventory": post,
        "post_run_metadata_error": post_error,
        "attempts_jsonl": str(rows_path),
        "cases": protocol_results,
    }
    with output_path.open("x", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: output[k] for k in (
        "source_commit", "native_scrapanium_c_sha256", "native_websocket_inc_c_sha256",
        "bridge_sha256", "mapped_backend_path", "mapped_backend_sha256",
        "backend_version", "attempt_count", "passed", "failed", "hashes_stable_after"
    )}, sort_keys=True))
    for failure in failures:
        print(f"FAIL {failure['protocol']}/{failure['case']}: "
              f"err={failure.get('recv_error')} send={failure.get('send_after_error')} "
              f"payload={failure.get('recv_payload_hex')}")
    if failures or not hashes_stable:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

