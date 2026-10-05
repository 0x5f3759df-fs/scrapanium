#!/usr/bin/env python3
"""Correctness-only capability probe for curl_easy_recv on a connect-only WSS handle."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEY = "dGhlIHNhbXBsZSBub25jZQ=="
ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().with_name("raw_recv_probe.c")
DEFAULT_BACKEND = ROOT / ".deps" / "curl"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_certificate_helper():
    path = ROOT / "tests" / "lab.py"
    spec = importlib.util.spec_from_file_location("scrapanium_probe_lab", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import certificate helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.certificate


def recv_exact(sock: ssl.SSLSocket, amount: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < amount:
        part = sock.recv(amount - len(chunks))
        if not part:
            raise EOFError(f"peer closed after {len(chunks)}/{amount} bytes")
        chunks.extend(part)
    return bytes(chunks)


def read_http_request(sock: ssl.SSLSocket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(4096)
        if not part:
            raise EOFError("client closed during HTTP upgrade")
        data.extend(part)
        if len(data) > 16384:
            raise ValueError("upgrade request exceeded 16 KiB")
    return bytes(data)


def read_client_frame(sock: ssl.SSLSocket) -> dict:
    first, second = recv_exact(sock, 2)
    if first & 0x70:
        raise ValueError(f"client frame has nonzero RSV bits: {first:02x}")
    fin = bool(first & 0x80)
    opcode = first & 0x0f
    if opcode not in (2, 8, 10):
        raise ValueError(f"unexpected client opcode: {opcode}")
    masked = bool(second & 0x80)
    marker = second & 0x7f
    length = marker
    if marker == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
        if length < 126:
            raise ValueError("noncanonical 16-bit client length")
    elif marker == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
        if length <= 65535 or length >> 63:
            raise ValueError("invalid/noncanonical 64-bit client length")
    if length > 1024 or (opcode >= 8 and length > 125):
        raise ValueError(f"client payload exceeds probe cap: {length}")
    if not masked:
        raise AssertionError("client WebSocket frame was not masked")
    mask = recv_exact(sock, 4)
    raw = recv_exact(sock, length)
    payload = bytes(value ^ mask[i & 3] for i, value in enumerate(raw))
    return {"fin": fin, "opcode": opcode, "masked": masked,
            "length": length, "payload_hex": payload.hex(), "payload": payload}


def send_split(sock: ssl.SSLSocket, pieces: list[bytes]) -> None:
    for piece in pieces:
        sock.sendall(piece)


def peer_thread(listener: socket.socket, context: ssl.SSLContext,
                outcome: dict, done: threading.Event) -> None:
    conn = None
    try:
        listener.settimeout(8)
        raw, address = listener.accept()
        outcome["client_address"] = list(address)
        raw.settimeout(8)
        conn = context.wrap_socket(raw, server_side=True)
        outcome["tls_version"] = conn.version()
        outcome["tls_cipher"] = conn.cipher()
        outcome["alpn"] = conn.selected_alpn_protocol()
        request = read_http_request(conn)
        outcome["request_sha256"] = hashlib.sha256(request).hexdigest()
        request_text = request.decode("latin-1")
        lines = request_text.split("\r\n")
        headers = {}
        for line in lines[1:]:
            if not line:
                break
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        outcome["request_line"] = lines[0]
        outcome["upgrade"] = headers.get("upgrade", "").lower()
        outcome["connection"] = headers.get("connection", "").lower()
        outcome["websocket_version"] = headers.get("sec-websocket-version")
        outcome["websocket_key"] = headers.get("sec-websocket-key")
        if outcome["websocket_key"] != KEY:
            raise AssertionError(f"unexpected WebSocket key: {outcome['websocket_key']!r}")
        if outcome["upgrade"] != "websocket" or "upgrade" not in outcome["connection"]:
            raise AssertionError("invalid WebSocket upgrade request")
        if outcome["websocket_version"] != "13":
            raise AssertionError("unexpected WebSocket version")
        accept = base64.b64encode(hashlib.sha1((KEY + GUID).encode()).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
        # One TLS write deliberately coalesces the first WebSocket octet with 101.
        conn.sendall(response + b"\x02")
        outcome["upgrade_and_first_frame_same_sendall"] = True
        outcome["coalesced_raw_octet"] = "02"

        ready = read_client_frame(conn)
        outcome["ready_frame"] = {k: v for k, v in ready.items() if k != "payload"}
        if not (ready["fin"] and ready["opcode"] == 2 and ready["payload"] == b"ready"):
            raise AssertionError(f"unexpected ready frame: {outcome['ready_frame']}")

        # Binary FIN=0 header and payload arrive in separately bounded writes.
        send_split(conn, [b"\x03", b"a", b"bc"])
        # PING is interleaved before the continuation and itself split across writes.
        send_split(conn, [b"\x89", b"\x04", b"pi", b"ng"])
        pong = read_client_frame(conn)
        outcome["pong_frame"] = {k: v for k, v in pong.items() if k != "payload"}
        if not (pong["fin"] and pong["opcode"] == 10 and pong["payload"] == b"ping"):
            raise AssertionError(f"unexpected PONG frame: {outcome['pong_frame']}")

        conn.sendall(b"\x80\x03def")
        large_header = bytes([0x82, 0x7f]) + (65536).to_bytes(8, "big")
        large_body = bytes(range(256)) * 256
        conn.sendall(large_header)
        conn.sendall(large_body)

        data_frame = read_client_frame(conn)
        outcome["client_data_frame"] = {k: v for k, v in data_frame.items() if k != "payload"}
        if not (data_frame["fin"] and data_frame["opcode"] == 2 and
                data_frame["payload"] == b"client-data"):
            raise AssertionError(f"unexpected client data frame: {outcome['client_data_frame']}")

        close_frame = read_client_frame(conn)
        outcome["client_close_frame"] = {k: v for k, v in close_frame.items() if k != "payload"}
        if not (close_frame["fin"] and close_frame["opcode"] == 8 and
                close_frame["payload"] == b"\x03\xe8done"):
            raise AssertionError(f"unexpected client CLOSE: {outcome['client_close_frame']}")

        conn.sendall(b"\x88\x06\x03\xe8done")
        outcome["sent_close_echo"] = True
        outcome["passed"] = True
    except BaseException as exc:
        outcome["passed"] = False
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        done.set()
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        try:
            listener.close()
        except OSError:
            pass


def run(args) -> int:
    script_dir = Path(__file__).resolve().parent
    backend = args.backend.resolve(strict=True)
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output}")
    output.mkdir(parents=True)
    binary = output / "raw_recv_probe"
    include = backend / "include"
    library = backend / "libcurl-impersonate.so"
    if not library.exists():
        library = backend / "lib" / "libcurl-impersonate.so"
    library = library.resolve(strict=True)
    compiler = shutil.which(args.cc)
    if not compiler:
        raise FileNotFoundError(f"compiler not found: {args.cc}")
    compile_argv = [
        compiler, "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-D_FORTIFY_SOURCE=2", f"-I{include}", str(SOURCE), "-o", str(binary),
        f"-L{library.parent}", f"-Wl,-rpath,{library.parent}",
        "-lcurl-impersonate", "-ldl",
    ]
    env = os.environ.copy()
    for key in ("BEND_SOURCE", "SCRAPANIUM_CURL_DIR", "BUN", "CC", "LD_PRELOAD"):
        env.pop(key, None)
    env["LD_LIBRARY_PATH"] = str(library.parent)
    compiler_info = subprocess.run([compiler, "--version"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=10, check=False)
    build = subprocess.run(compile_argv, cwd=ROOT, env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=120, check=False)
    (output / "build.log").write_text(build.stdout, encoding="utf-8")
    if build.returncode != 0:
        raise RuntimeError(f"probe build failed with {build.returncode}; see {output / 'build.log'}")
    ldd = subprocess.run(["ldd", str(binary)], text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=10, check=False)
    (output / "ldd.txt").write_text(ldd.stdout, encoding="utf-8")
    mapped_lines = [line for line in ldd.stdout.splitlines()
                    if "libcurl-impersonate" in line]
    mapped_path = None
    if len(mapped_lines) == 1 and "=>" in mapped_lines[0]:
        mapped_text = mapped_lines[0].split("=>", 1)[1].strip().split(" ", 1)[0]
        mapped_path = Path(mapped_text).resolve(strict=True)
    if ldd.returncode != 0 or len(mapped_lines) != 1 or mapped_path != library:
        raise RuntimeError("ldd did not resolve exactly the selected backend DSO")

    cert_dir = tempfile.TemporaryDirectory(prefix="wss-raw-recv-probe-")
    certificate = load_certificate_helper()
    tls_context, cert_path_string = certificate(cert_dir.name)
    cert_path = Path(cert_path_string)
    public_certificate_sha256 = sha256(cert_path)
    key_path = Path(cert_dir.name) / "key.pem"
    try:
        key_path.unlink()
    except FileNotFoundError:
        pass
    tls_context.set_alpn_protocols(["http/1.1"])

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    outcome: dict = {}
    done = threading.Event()
    thread = threading.Thread(target=peer_thread,
                              args=(listener, tls_context, outcome, done),
                              daemon=True)
    thread.start()
    command = [str(binary), f"wss://localhost:{port}/probe", str(cert_path)]
    child = None
    client_stdout = ""
    client_stderr = ""
    returncode = None
    try:
        child = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client_stdout, client_stderr = child.communicate(timeout=20)
        returncode = child.returncode
    except subprocess.TimeoutExpired:
        child.kill()
        client_stdout, client_stderr = child.communicate(timeout=5)
        returncode = child.returncode
        client_stderr += "\nrunner: client timeout; killed child\n"
    finally:
        thread.join(timeout=10)
        if thread.is_alive():
            try:
                listener.close()
            except OSError:
                pass
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            thread.join(timeout=2)
        cert_dir.cleanup()

    (output / "client.stdout").write_text(client_stdout, encoding="utf-8")
    (output / "client.stderr").write_text(client_stderr, encoding="utf-8")
    backend_lines = [line for line in client_stdout.splitlines()
                     if line.startswith("backend_path=")]
    runtime_backend = None
    if len(backend_lines) == 1:
        runtime_backend = Path(backend_lines[0].split("=", 1)[1]).resolve(strict=True)
    (output / "peer.json").write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    manifest = {
        "schema": "scrapanium-wss-raw-recv-capability-v1",
        "scope": "correctness-only capability probe; no timing or throughput claim",
        "repo": str(ROOT),
        "source_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, check=True,
            stdout=subprocess.PIPE).stdout.strip(),
        "tracked_worktree_clean": not subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
            text=True, check=True, stdout=subprocess.PIPE).stdout.strip(),
        "probe_sources": {
            SOURCE.name: sha256(SOURCE),
            Path(__file__).resolve().name: sha256(Path(__file__).resolve()),
        },
        "compiler": {"path": compiler, "version": compiler_info.stdout,
                     "argv": compile_argv, "exit_code": build.returncode},
        "backend": {"configured_prefix": str(args.backend),
                    "resolved_dso": str(library), "sha256": sha256(library),
                    "ldd_line": mapped_lines[0],
                    "ldd_resolved_path": str(mapped_path),
                    "runtime_dladdr_path": str(runtime_backend),
                    "runtime_path_matches": runtime_backend == library},
        "client": {"argv": command, "return_code": returncode,
                   "stdout_path": str(output / "client.stdout"),
                   "stderr_path": str(output / "client.stderr"),
                   "executable_sha256": sha256(binary)},
        "tls_peer": {"public_certificate_sha256": public_certificate_sha256,
                     "private_key_retained": False,
                     "outcome": outcome},
        "api_contract": {
            "impersonation_profile": "chrome146",
            "connect_only": 2, "curlws_raw_mode": False,
            "curlws_noautopong": True, "inbound_api": "curl_easy_recv",
            "outbound_api": "curl_ws_send",
            "tls_peer_verification": True, "hostname_verification": True,
        },
        "passed": (returncode == 0 and outcome.get("passed") is True and
                   runtime_backend == library and "gated_empty_recv=CURLE_AGAIN" in client_stdout),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    if not manifest["passed"]:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cc", default="gcc")
    args = parser.parse_args()
    try:
        return run(args)
    except BaseException as exc:
        print(f"probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
