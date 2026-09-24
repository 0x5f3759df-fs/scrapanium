"""Small TLS peer for direct curl_ws_recv boundary/pending fixtures.

Frame encoding and masked-client decoding are reused from tests/ws_lab.py.
"""
import base64
import hashlib
import http.server
import socket
import threading

from ws_lab import frame, read_frame


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(400)
            return
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "websocket")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(5)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            if self.path == "/queued":
                # Both frames are put in one TLS application write. A caller
                # with room for both must still receive one frame per API call.
                wire = frame(2, b"queued-first") + frame(2, b"queued-second-frame")
                self.wfile.write(wire)
                self.wfile.flush()
            elif self.path == "/drain":
                # Keep a complete patterned frame available so the wrapped
                # client can cap the initial read and several follow-up reads
                # without stopping because the peer ran out of input.
                payload = bytes(i & 255 for i in range(2048))
                self.wfile.write(frame(2, payload))
                self.wfile.flush()
            elif self.path == "/stale":
                payload = bytes(i & 255 for i in range(2048))
                wire = frame(2, payload)
                header_size = len(wire) - len(payload)
                self.wfile.write(wire[:header_size + 64])
                self.wfile.flush()
                # Keep the remainder unavailable until the client has made a
                # separate API call and sends this marker PING.
                kind, marker, final = read_frame(self.rfile)
                if (kind, marker, final) != (9, b"resume", True):
                    raise AssertionError((kind, marker, final))
                self.server.frames.append((kind, marker, final))
                self.wfile.write(wire[header_size + 64:])
                self.wfile.write(frame(10, marker))
                self.wfile.flush()
            elif self.path == "/fatal":
                wire = frame(2, b"abcdefgh")[:-3]
                self.wfile.write(wire)
                self.wfile.flush()
                # Close the underlying TCP transport without a WebSocket close
                # frame or TLS close_notify, exercising a partial fatal read.
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            elif self.path == "/split-ping":
                ping = b"split-ping"
                wire = frame(9, ping)
                # Split the control header and payload across flushed TLS
                # writes, then observe the backend's default auto-PONG bytes.
                for part in (wire[:2], wire[2:5], wire[5:8], wire[8:]):
                    self.wfile.write(part)
                    self.wfile.flush()
                self.wfile.write(frame(2, b"after-auto-pong"))
                self.wfile.flush()
            while True:
                kind, payload, final = read_frame(self.rfile)
                self.server.frames.append((kind, payload, final))
                if kind == 8:
                    self.wfile.write(frame(8, payload))
                    self.wfile.flush()
                    return
                if kind == 9:
                    self.wfile.write(frame(10, payload))
                    self.wfile.flush()
        except (OSError, EOFError, AssertionError) as error:
            self.server.errors.append(repr(error))


class Peer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def handle_error(self, *_):
        pass


def start_peer(context):
    server = Peer(("127.0.0.1", 0), Handler)
    server.frames = []
    server.errors = []
    context.set_alpn_protocols(["http/1.1"])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"wss://127.0.0.1:{server.server_port}"
