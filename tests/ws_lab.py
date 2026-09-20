"""Independent RFC 6455 loopback peer. Captures exact masked client frames."""
import base64
import hashlib
import http.server
import socket
import struct
import threading
import time


def frame(kind, data=b"", final=True):
    n = len(data)
    length = bytes([n]) if n < 126 else b"\x7e" + struct.pack("!H", n) if n < 65536 else b"\x7f" + struct.pack("!Q", n)
    return bytes([(128 if final else 0) | kind]) + length + data


def read_frame(stream):
    def read(n):
        data = stream.read(n)
        if len(data) != n: raise EOFError()
        return data
    a, b = read(2)
    assert b & 128, "client frames must be masked"
    n = b & 127
    if n == 126: n = struct.unpack("!H", read(2))[0]
    elif n == 127: n = struct.unpack("!Q", read(8))[0]
    assert n <= 16 * 1024 * 1024, "test peer input limit"
    mask, payload = read(4), read(n)
    return a & 15, bytes(v ^ mask[i % 4] for i, v in enumerate(payload)), bool(a & 128)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *_): pass
    def do_GET(self):
        self.server.handshakes.append(list(self.headers.items()))
        if self.path == "/reject":
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/echo"); self.send_header("Content-Length", "0"); self.end_headers(); return
        if self.path == "/stall-upgrade": time.sleep(1)
        key = self.headers.get("Sec-WebSocket-Key", "")
        assert len(base64.b64decode(key)) == 16
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101); self.send_header("Connection", "Upgrade"); self.send_header("Upgrade", "websocket")
        if self.path != "/missing-accept": self.send_header("Sec-WebSocket-Accept", "bad" if self.path == "/bad-accept" else accept)
        if self.path == "/duplicate-accept": self.send_header("Sec-WebSocket-Accept", accept)
        if self.path == "/extensions": self.send_header("Sec-WebSocket-Extensions", "permessage-deflate")
        if self.path == "/unsolicited-protocol": self.send_header("Sec-WebSocket-Protocol", "unknown")
        if self.headers.get("Sec-WebSocket-Protocol"): self.send_header("Sec-WebSocket-Protocol", "chat")
        self.end_headers(); self.close_connection = True
        self.connection.settimeout(4)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            if self.path == "/fragments":
                self.wfile.write(frame(1, b"hello \xf0\x9f", False) + frame(9, b"heartbeat") + frame(0, b"\x8c\x8d", True))
            elif self.path == "/large": self.wfile.write(frame(2, bytes(range(256)) * 4096))
            elif self.path == "/fragment-limit": self.wfile.write(frame(2, b"a" * 60, False) + frame(0, b"b" * 60))
            elif self.path == "/close": self.wfile.write(frame(8, b"\x03\xe8bye"))
            elif self.path == "/bad-utf8": self.wfile.write(frame(1, b"\xc0\x80"))
            elif self.path == "/bad-close": self.wfile.write(frame(8, b"\x03\xed"))
            elif self.path == "/truncated": self.wfile.write(frame(2, b"abcdefgh")[:-3]); return
            elif self.path == "/flood-ping":
                for _ in range(100): self.wfile.write(frame(9, b"tick")); time.sleep(.01)
                return
            elif self.path == "/slow-send": time.sleep(.25)
            while True:
                kind, data, final = read_frame(self.rfile)
                self.server.frames.append((kind, data, final))
                if kind == 8:
                    self.wfile.write(frame(8, data)); return
                if kind == 9: self.wfile.write(frame(10, data))
                elif kind in (1, 2): self.wfile.write(frame(kind, data))
        except (OSError, EOFError): pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    def handle_error(self, *_): pass


def start_ws(context=None):
    server = Server(("127.0.0.1", 0), Handler)
    server.handshakes, server.frames = [], []
    if context:
        context.set_alpn_protocols(["http/1.1"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ("wss" if context else "ws") + f"://127.0.0.1:{server.server_port}"
