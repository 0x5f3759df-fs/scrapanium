"""Segmented malformed UTF-8 regression test for the optional owned backend."""
import base64
import hashlib
import os
from http.server import BaseHTTPRequestHandler
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lab import certificate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def frame(kind, payload=b"", final=True):
    size = len(payload)
    length = (bytes([size]) if size < 126 else b"\x7e" + size.to_bytes(2, "big")
              if size < 65536 else b"\x7f" + size.to_bytes(8, "big"))
    return bytes([(0x80 if final else 0) | kind]) + length + payload


# Each split crosses a UTF-8 state transition at a continuation-frame boundary.
# A malformed byte that breaks a sequence must be reread as a fresh lead.
# Independently evaluated from the pinned Bend 2.0.27 io_str decoder over the
# concatenated bytes; malformed-sequence break bytes are reprocessed as leads.
EXPECTED_WIRE_BYTES = bytes.fromhex("00e240a1f09feda080f49080807ce282")
EXPECTED_CODEPOINTS = (
    0, 65533, 64, 65533, 65533, 65533, 65533, 65533, 65533, 65533,
    65533, 65533, 124, 65533,
)

FRAGMENTS = [
    b"\x00\xe2",
    b"\x40\xa1\xf0",
    b"\x9f\xed",
    b"\xa0\x80\xf4",
    b"\x90\x80\x80|\xe2\x82",
]


class EdgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        self.send_response(101)
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "websocket")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(5)
        for index, payload in enumerate(FRAGMENTS):
            self.wfile.write(frame(2 if index == 0 else 0, payload,
                                   final=index == len(FRAGMENTS) - 1))
            self.wfile.flush()


class EdgeServer:
    def __init__(self, context):
        from ws_lab import Server

        self.server = Server(("127.0.0.1", 0), EdgeHandler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"wss://127.0.0.1:{self.server.server_port}/utf8-edges"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def edge_server(tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("owned-text-edge-tls"))
    server = EdgeServer(context)
    yield server, ca
    server.close()


@pytest.fixture(scope="module")
def bend_owned_text_edges():
    from build import build

    return build(ROOT / "tests/websocket_owned_text_edges.bend",
                 ROOT / "build/ws-owned-text-edges", True)


@pytest.mark.parametrize("threads", [1, 4])
def test_bytes_text_matches_pinned_bend_io_str_across_owned_segments(
        bend_owned_text_edges, edge_server, threads):
    server, ca = edge_server
    assert b"".join(FRAGMENTS) == EXPECTED_WIRE_BYTES
    out = subprocess.run(
        [str(bend_owned_text_edges), "--threads", str(threads)],
        env={**os.environ, "SCRAPANIUM_TEST_URL": server.url,
             "SCRAPANIUM_TEST_CA": ca},
        capture_output=True, text=True, timeout=20,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    actual = tuple(int(value) for value in out.stdout.split())
    assert actual == EXPECTED_CODEPOINTS
