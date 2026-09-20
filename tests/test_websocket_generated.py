"""Reproducible RFC 6455 sequences across frame and transport boundaries."""
import base64
from dataclasses import dataclass, field
import hashlib
import http.server
import random
import socket
import threading

import pytest

from lab import certificate
from test_websocket import WebSocket
from ws_lab import frame, read_frame


SIZES = (0, 1, 125, 126, 16383, 16384, 16385, 32767, 65535, 65536,
         65537, 131071, 131072, 131073, 262143, 262144, 262145, 393221)


@dataclass
class Sequence:
    messages: list = field(default_factory=list)
    pongs: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


def sequence(seed):
    rng = random.Random(seed)
    case = Sequence()
    wire = bytearray()
    sizes = list(SIZES)
    rng.shuffle(sizes)
    for index, size in enumerate(sizes):
        kind = 1 if (index + seed) % 2 else 2
        if kind == 1:
            tile = f"{seed}:{index}:🌍\0é€".encode()
            payload = tile * (size // len(tile)) + b"x" * (size % len(tile))
        else:
            payload = rng.randbytes(size)
        case.messages.append((kind, payload))
        # Duplicate cuts deliberately create empty continuations. Arbitrary
        # byte cuts also split multibyte text without making the message invalid.
        cuts = sorted([0, size] + [rng.randrange(size + 1)
                                  for _ in range(rng.randrange(1, 7))])
        if index % 3 == 0:
            cuts.insert(1, 0)
        for part, (left, right) in enumerate(zip(cuts, cuts[1:])):
            final = part == len(cuts) - 2
            wire.extend(frame(kind if part == 0 else 0, payload[left:right], final))
            if not final:
                ping_size = (0, 1, 125)[(index + part) % 3]
                marker = f"{seed}:{index}:{part}:".encode()
                ping = (marker + rng.randbytes(125))[:ping_size]
                wire.extend(frame(9, ping))
                case.pongs.append((10, ping, True))
                wire.extend(frame(10, b"unsolicited-pong"))
    if seed % 2:
        # Write boundaries are independent of frame boundaries, including cuts
        # inside headers. TCP/TLS may coalesce writes; correctness cannot rely
        # on any particular mapping between writes and receive calls.
        chunks = [1, 2, 3, 7, 125, 16383, 16384, 16385, 65535]
        offset = 0
        while offset < len(wire):
            rng.shuffle(chunks)
            for size in chunks:
                case.writes.append(bytes(wire[offset:offset + size]))
                offset += size
                if offset >= len(wire):
                    break
    else:
        case.writes.append(bytes(wire))
    return case


class GeneratedPeer(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        case = self.server.cases[self.path]
        self.close_connection = True
        try:
            self.connection.settimeout(10)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            key = self.headers["Sec-WebSocket-Key"]
            assert len(base64.b64decode(key, validate=True)) == 16
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "websocket")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            for chunk in case.writes:
                self.wfile.write(chunk)
            # The peer validates every automatic pong, including empty and
            # maximum-length controls. read_frame also requires client masking.
            for index, expected in enumerate(case.pongs):
                actual = read_frame(self.rfile)
                assert actual == expected, (index, actual, expected)
            actual = read_frame(self.rfile)
            assert actual == (8, b"\x03\xe8generated", True), actual
            self.wfile.write(frame(8, actual[1]))
        except Exception as error:
            case.errors.append(error)
        finally:
            case.done.set()


@pytest.fixture(scope="module", params=[False, True], ids=["ws", "wss"])
def generated_peer(request, tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("generated-wss")) if request.param else (None, "")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GeneratedPeer)
    server.daemon_threads = True
    server.cases = {}
    if context:
        context.set_alpn_protocols(["http/1.1"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    scheme = "wss" if context else "ws"
    yield server, f"{scheme}://127.0.0.1:{server.server_port}", ca
    server.shutdown()
    server.server_close()
    worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize("seed", [6455, 16384, 131071, 262144])
def test_generated_sequences_preserve_bytes_order_and_controls(generated_peer, seed):
    server, url, ca = generated_peer
    case = sequence(seed)
    path = f"/sequence-{seed}"
    server.cases[path] = case
    with WebSocket(url + path, ca_bundle=ca, max_body_bytes=max(SIZES)) as websocket:
        assert websocket.error == 0
        for index, (kind, payload) in enumerate(case.messages):
            actual = websocket.recv(timeout=10000)
            assert actual == (0, kind, payload), (seed, index, kind, len(payload), actual[:2])
        assert websocket.close(reason=b"generated", timeout=10000) == 0
    assert case.done.wait(10), f"peer did not finish seed {seed}"
    assert not case.errors, case.errors
