"""Loopback HTTP/1, TLS, HTTP/2 and CONNECT fixtures. No external service."""
import datetime
import gzip
import http.server
import ipaddress
import json
import select
import socket
import socketserver
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    def log_message(self, *_): pass
    def do_HEAD(self): self.do_GET()
    def do_POST(self): self.do_GET()
    def do_PUT(self): self.do_GET()
    def do_PATCH(self): self.do_GET()
    def do_DELETE(self): self.do_GET()
    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = urlsplit(self.path).path
        code, hs, data = 200, [], b"ok"
        if path == "/echo":
            data = json.dumps({"method": self.command, "body": list(body), "path": self.path,
                "headers": list(self.headers.items()), "port": self.client_address[1]}).encode()
        elif path.startswith("/bytes/"):
            data = bytes(range(256)) * (int(path.rsplit("/", 1)[1]) // 256)
        elif path == "/binary": data = b"\x00\xff\xc0\x80hello\x00"
        elif path == "/unicode": data = "Bend: héllo 🌍".encode()
        elif path.startswith("/status/"): code = int(path.rsplit("/", 1)[1])
        elif path == "/redirect": code, hs, data = 302, [("Location", "/echo")], b"redirect"
        elif path == "/redirect307": code, hs = 307, [("Location", "/echo")]
        elif path == "/redirect303": code, hs = 303, [("Location", "/echo")]
        elif path == "/loop": code, hs = 302, [("Location", "/loop")]
        elif path == "/badredirect": code, hs = 302, [("Location", "file:///etc/passwd")]
        elif path == "/cross": code, hs = 302, [("Location", self.server.cross_url + "/echo")]
        elif path == "/setcookie": hs = [("Set-Cookie", "session=scrapanium; Path=/; HttpOnly")]
        elif path == "/duplicate": hs = [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        elif path == "/metadata": hs = [("X-Empty", ""), ("X-Value", "  a: b \t"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-Case", "first"), ("x-case", "second")]
        elif path == "/metadata-redirect": code, hs = 302, [("Location", "/metadata"), ("X-Old", "redirect")]
        elif path == "/early":
            self.wfile.write(b"HTTP/1.1 103 Early Hints\r\nX-Old: hints\r\n\r\n")
            hs = [("X-Final", "complete")]
        elif path == "/trailers":
            self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Phase, X-Tail\r\nX-Phase: header\r\n\r\n3\r\nabc\r\n0\r\nX-Phase: trailer\r\nX-Tail: yes\r\n\r\n")
            return
        elif path == "/folded":
            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Folded: one\r\n\ttwo\r\nContent-Length: 2\r\n\r\nok")
            return
        elif path == "/gzip": data, hs = gzip.compress(b"decompressed" * 100), [("Content-Encoding", "gzip")]
        elif path == "/bomb": data, hs = gzip.compress(b"x" * 65536), [("Content-Encoding", "gzip")]
        elif path == "/headers": hs = [("X-Large", "x" * 8192)]
        elif path.startswith("/slow/"): time.sleep(int(path.rsplit("/", 1)[1]) / 1000)
        elif path == "/paced":
            self.send_response(200); self.send_header("Content-Length", str(4096 * 64))
            self.end_headers()
            for _ in range(64):
                try: self.wfile.write(b"x" * 4096); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError): return
                time.sleep(0.01)
            return
        elif path == "/truncated":
            self.send_response(200); self.send_header("Content-Length", "100")
            self.end_headers(); self.wfile.write(b"short"); self.close_connection = True; return
        elif path == "/chunked":
            self.send_response(200); self.send_header("Transfer-Encoding", "chunked")
            self.end_headers(); self.wfile.write(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"); return
        self.send_response(code)
        for k, v in hs: self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass

class HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    def handle_error(self, *_): pass

def start_http(context=None):
    server = HTTPServer(("127.0.0.1", 0), Handler)
    if context: server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, ("https" if context else "http") + "://127.0.0.1:" + str(server.server_port)

def certificate(directory, mismatch=False, expired=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(hours=-1 if expired else 24))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("wrong.example")] if mismatch else
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, str(cert_path)

class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        first = self.rfile.readline().decode().strip()
        self.server.requests.append(first)
        method, target, version = first.split()
        headers = []
        while True:
            line = self.rfile.readline()
            if line == b"\r\n": break
            if not line: return
            headers.append(line)
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)), timeout=3)
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n"); self.wfile.flush()
        else:
            url = urlsplit(target)
            upstream = socket.create_connection((url.hostname, url.port or 80), timeout=3)
            upstream.sendall(f"{method} {url.path or '/'} {version}\r\n".encode() + b"".join(headers) + b"\r\n")
        with upstream:
            for sock in (upstream, self.connection): sock.settimeout(3)
            while True:
                ready, _, _ = select.select([upstream, self.connection], [], [], 2)
                if not ready: return
                for sock in ready:
                    try: data = sock.recv(65536)
                    except OSError: return
                    if not data: return
                    (self.connection if sock is upstream else upstream).sendall(data)

class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128
    def handle_error(self, *_): pass

def start_proxy():
    server = ProxyServer(("127.0.0.1", 0), Proxy)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"

class H2Handler(socketserver.BaseRequestHandler):
    def handle(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import RequestReceived, StreamEnded, DataReceived, RemoteSettingsChanged, WindowUpdated
        with self.server.context.wrap_socket(self.request, server_side=True) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(5)
            h = H2Connection(config=H2Configuration(client_side=False))
            paths = {}
            h.initiate_connection(); sock.sendall(h.data_to_send())
            while True:
                try: data = sock.recv(65536)
                except (TimeoutError, ConnectionError): return
                if not data: return
                for event in h.receive_data(data):
                    if isinstance(event, RemoteSettingsChanged):
                        self.server.settings.append([(int(k), v.new_value) for k, v in event.changed_settings.items()])
                    elif isinstance(event, WindowUpdated):
                        self.server.windows.append((event.stream_id, event.delta))
                    elif isinstance(event, RequestReceived):
                        self.server.headers.append(event.headers)
                        paths[event.stream_id] = dict(event.headers).get(b":path", b"/")
                    elif isinstance(event, DataReceived):
                        h.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, StreamEnded):
                        h.send_headers(event.stream_id, [(":status", "200"), ("content-length", "2")])
                        trailer = paths.pop(event.stream_id) == b"/trailers"
                        h.send_data(event.stream_id, b"h2", end_stream=not trailer)
                        if trailer: h.send_headers(event.stream_id, [("x-tail", "h2-trailer")], end_stream=True)
                sock.sendall(h.data_to_send())

def start_h2(context):
    context.set_alpn_protocols(["h2"])
    server = ProxyServer(("127.0.0.1", 0), H2Handler)
    server.context, server.settings, server.headers, server.windows = context, [], [], []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"https://127.0.0.1:{server.server_address[1]}"
