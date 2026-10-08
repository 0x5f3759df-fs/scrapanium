#!/usr/bin/env python3
"""Capture real-browser WSS ClientHello and RFC 6455 request headers.

Each sample uses a fresh browser profile and a temporary local CA/key. Only the
ClientHello and parsed WebSocket request are retained; no key material is saved.
"""
import argparse
import base64
import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from capture_browsers import browser_process, sha256
from fingerprint import detailed_hello, normalized, parse_hello
from browser_lab import browser_certificate
from ws_lab import start_ws


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise EOFError("peer closed before TLS ClientHello completed")
        data.extend(block)
    return bytes(data)


def read_clienthello(sock):
    records, handshake = [], bytearray()
    sock.settimeout(20)
    while len(handshake) < 4 or len(handshake) < 4 + int.from_bytes(handshake[1:4], "big"):
        header = exact(sock, 5)
        size = int.from_bytes(header[3:5], "big")
        if header[0] != 22 or size > 18432:
            raise ValueError("invalid TLS handshake record")
        record = header + exact(sock, size)
        records.append(record)
        handshake.extend(record[5:])
        if len(handshake) > 262144:
            raise ValueError("ClientHello exceeds capture budget")
    if handshake[0] != 1:
        raise ValueError("first handshake message is not ClientHello")
    size = 4 + int.from_bytes(handshake[1:4], "big")
    return records, bytes(handshake[:size])


def relay(source, destination):
    try:
        while True:
            block = source.recv(65536)
            if not block:
                break
            destination.sendall(block)
    except (OSError, ssl.SSLError):
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            records, hello = read_clienthello(client)
            upstream = socket.create_connection(self.server.backend, timeout=10)
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.sendall(b"".join(records))
            self.server.observations.append({
                "tls_records_hex": [record.hex() for record in records],
                "clienthello_sha256": hashlib.sha256(hello).hexdigest(),
                "clienthello_bytes": len(hello),
                "parsed_clienthello": parse_hello(hello),
                "normalized_clienthello": normalized(parse_hello(hello)),
                "detailed_clienthello": detailed_hello(hello),
            })
            self.server.observed.set()
            a = threading.Thread(target=relay, args=(client, upstream), daemon=True)
            b = threading.Thread(target=relay, args=(upstream, client), daemon=True)
            a.start(); b.start()
            a.join(15); b.join(15)
            if a.is_alive() or b.is_alive():
                try: client.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: upstream.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        except Exception as error:
            self.server.errors.append(f"{type(error).__name__}: {error}")
            self.server.observed.set()
        finally:
            if upstream:
                upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64


class PageHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/":
            body = self.server.html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(urlsplit(self.path).query)
        if urlsplit(self.path).path == "/done":
            self.server.result = {
                "state": query.get("state", [""])[0],
                "close_code": query.get("code", [""])[0],
                "was_clean": query.get("clean", [""])[0],
            }
            self.server.done.set()
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)


class PageServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def observe_ws_server(server):
    original = server.RequestHandlerClass

    class ObservedHandler(original):
        def do_GET(self):
            self.server.ws_metadata.append({
                "http_version": self.request_version,
                "path": self.path,
                "tls_version": self.connection.version(),
                "alpn": self.connection.selected_alpn_protocol(),
                "headers": list(self.headers.items()),
            })
            super().do_GET()

    server.RequestHandlerClass = ObservedHandler
    server.ws_metadata = []


def one_sample(kind, binary, *, headless, timeout):
    with tempfile.TemporaryDirectory(prefix="scrapanium-browser-wss-") as work:
        context, ca, leaf = browser_certificate(work)
        ws_server, _ = start_ws(context)
        observe_ws_server(ws_server)
        proxy = ProxyServer(("127.0.0.1", 0), ProxyHandler)
        proxy.backend = ws_server.server_address
        proxy.observations, proxy.errors = [], []
        proxy.observed = threading.Event()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        ws_url = f"wss://localhost:{proxy.server_address[1]}/echo"
        page = PageServer(("127.0.0.1", 0), PageHandler)
        page.done, page.result = threading.Event(), None
        page.html = (
            "<!doctype html><meta charset=utf-8><link rel=icon href=data:,>"
            "<body><script>let state='pending';"
            f"const ws=new WebSocket({json.dumps(ws_url)});"
            "ws.onopen=()=>{state='open';ws.close(1000,'capture');};"
            "ws.onerror=()=>{state='error';};"
            "ws.onclose=e=>fetch('/done?state='+state+'&code='+e.code+'&clean='+e.wasClean,"
            "{cache:'no-store'});</script></body>"
        )
        threading.Thread(target=page.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{page.server_port}"
        try:
            with browser_process(
                kind, binary, origin + "/", ca, leaf, headless=headless
            ) as (process, flags, preferences, log):
                if not page.done.wait(timeout):
                    log.seek(0)
                    raise TimeoutError(
                        f"browser WSS page did not finish: {log.read()[-3000:]}"
                    )
                if page.result != {"state": "open", "close_code": "1000", "was_clean": "true"}:
                    log.seek(0)
                    raise RuntimeError(
                        f"browser WSS did not open and cleanly close: {page.result}; {log.read()[-3000:]}; "
                        f"proxy={proxy.errors}; peer={ws_server.errors}"
                    )
                if not proxy.observed.wait(5):
                    raise TimeoutError("proxy did not retain the TLS ClientHello")
                deadline = time.monotonic() + 5
                while not any(frame[0] == 8 for frame in ws_server.frames):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("WebSocket close frame was not observed")
                    time.sleep(0.01)
            if len(proxy.observations) != 1 or len(ws_server.ws_metadata) != 1:
                raise AssertionError("expected exactly one proxied TLS and WebSocket request")
            meta = ws_server.ws_metadata[0]
            headers = meta["headers"]
            lower = {name.lower(): value for name, value in headers}
            assert meta["http_version"] == "HTTP/1.1"
            assert meta["path"] == "/echo"
            assert meta["alpn"] == "http/1.1"
            assert lower.get("upgrade", "").lower() == "websocket"
            assert "upgrade" in lower.get("connection", "").lower()
            assert lower.get("sec-websocket-version") == "13"
            assert lower.get("origin") == origin
            assert len(base64.b64decode(lower["sec-websocket-key"])) == 16
            if not any(opcode == 8 and payload == bytes.fromhex("03e863617074757265")
                       for opcode, payload, _ in ws_server.frames):
                raise AssertionError("the peer did not observe close code 1000 and reason capture")
            client_frames = [
                {"opcode": opcode, "payload_hex": payload.hex(), "final": final}
                for opcode, payload, final in ws_server.frames
            ]
            return {
                "browser_page_result": page.result,
                "tls": proxy.observations[0],
                "websocket": {
                    **meta,
                    "status": 101,
                    "validated_by_browser_onopen": True,
                    "client_frames": client_frames,
                },
                "launch_flags": flags,
                "preferences": preferences,
                "certificate_setup": "local SPKI allowlist" if kind == "chrome"
                                     else "local CA imported into fresh NSS profile",
            }
        finally:
            page.shutdown(); page.server_close()
            proxy.shutdown(); proxy.server_close()
            ws_server.shutdown(); ws_server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--google-chrome", type=Path)
    parser.add_argument("--firefox", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--browser-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("output must be a fresh path inside the repository")
    if sys.platform != "linux" or not 1 <= args.samples <= 100 or args.timeout <= 0:
        parser.error("requires Linux, 1..100 samples and a positive timeout")
    if not (args.chrome or args.google_chrome or args.firefox):
        parser.error("supply at least one explicit browser executable")
    if args.headed and not os.environ.get("DISPLAY"):
        parser.error("--headed requires an X display, for example xvfb-run -a")
    manifest = args.release_manifest.resolve(strict=True)
    browser_root = args.browser_root.resolve(strict=True)
    if not manifest.is_relative_to(ROOT) or not browser_root.is_relative_to(ROOT / ".deps/browsers"):
        parser.error("release manifest and browser root must remain inside their repository locations")
    lock = json.loads(manifest.read_text())
    source_paths = [
        Path(__file__).resolve(), ROOT / "scripts/capture_browsers.py",
        ROOT / "tests/fingerprint.py", ROOT / "tests/browser_lab.py",
        ROOT / "tests/ws_lab.py", manifest,
    ]
    source_hashes = {
        str(path.relative_to(ROOT)): sha256(path)
        for path in source_paths
    }
    report = {
        "schema": 1,
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": {
            "system": __import__("platform").system(),
            "release": __import__("platform").release(),
            "machine": __import__("platform").machine(),
            "libc": __import__("platform").libc_ver(),
        },
        "mode": "headed" if args.headed else "headless",
        "scope": "actual browser WebSocket API over local TLS; one RFC 6455 Upgrade per fresh temporary profile",
        "release_manifest": {"path": str(manifest.relative_to(ROOT)), "sha256": sha256(manifest)},
        "source_sha256": source_hashes,
        "browsers": {},
        "failures": [],
    }
    for label, kind in (("chrome", "chrome"), ("google_chrome", "chrome"), ("firefox", "firefox")):
        binary = getattr(args, label)
        if binary is None:
            continue
        binary = binary.resolve(strict=True)
        version = subprocess.check_output([str(binary), "--version"], text=True, timeout=15).strip()
        samples = []
        for index in range(args.samples):
            print(f"Capturing {label} WSS sample {index + 1}/{args.samples}", flush=True)
            try:
                samples.append(one_sample(kind, binary, headless=not args.headed, timeout=args.timeout))
            except Exception as error:
                report["failures"].append({
                    "browser": label, "sample": index + 1,
                    "error": f"{type(error).__name__}: {error}",
                })
                break
        entry = {
            "version_output": version,
            "executable_sha256": sha256(binary),
            "samples": samples,
        }
        pinned = (browser_root / lock[label]["executable"]).resolve(strict=True)
        if binary == pinned:
            archive = browser_root / lock[label]["archive"]
            if sha256(archive) != lock[label]["sha256"]:
                raise ValueError("browser distribution hash mismatch")
            entry["download"] = lock[label]
        if kind == "firefox":
            entry["libxul_sha256"] = sha256(binary.parent / "libxul.so")
        report["browsers"][label] = entry
        if len(samples) != args.samples:
            break
    if source_hashes != {str(path.relative_to(ROOT)): sha256(path) for path in source_paths}:
        raise RuntimeError("capture source changed while running; discard and repeat")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(args.output)
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
