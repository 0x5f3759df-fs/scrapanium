"""Local HTTP/2 capture peer for actual browser navigation."""
import socketserver
import ssl
import threading


def browser_certificate(directory):
    """Use a separate CA and leaf: Firefox rejects a CA as an end entity."""
    import datetime
    import ipaddress
    from pathlib import Path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    path = Path(directory)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Scrapanium test CA')])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    def builder(subject, issuer, key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1)))
    ca = (builder(root_name, root_name, root_key)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .sign(root_key, hashes.SHA256()))
    leaf = (builder(leaf_name, root_name, leaf_key)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(root_key, hashes.SHA256()))
    ca_path, leaf_path, key_path = path / 'ca.pem', path / 'leaf.pem', path / 'key.pem'
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf_path, key_path)
    return context, ca_path, leaf_path


class BrowserH2Handler(socketserver.BaseRequestHandler):
    def handle(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import RequestReceived, RemoteSettingsChanged, WindowUpdated, PingAckReceived
        record = {'settings': [], 'windows': [], 'headers': [], 'received_hex': []}
        probe, acknowledged, request_stream = b'scrapcap', False, None
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as conn:
                conn.settimeout(10)
                record['alpn'] = conn.selected_alpn_protocol()
                record['tls_version'] = conn.version()
                h2 = H2Connection(config=H2Configuration(client_side=False))
                h2.initiate_connection()
                conn.sendall(h2.data_to_send())
                total = 0
                while not acknowledged:
                    data = conn.recv(65536)
                    if not data:
                        return
                    total += len(data)
                    if total > 262144:
                        raise ValueError('browser request exceeds capture budget')
                    record['received_hex'].append(data.hex())
                    for event in h2.receive_data(data):
                        if isinstance(event, RemoteSettingsChanged):
                            record['settings'].append([(int(k), v.new_value) for k, v in event.changed_settings.items()])
                        elif isinstance(event, WindowUpdated):
                            record['windows'].append((event.stream_id, event.delta))
                        elif isinstance(event, RequestReceived):
                            if request_stream is not None:
                                raise ValueError('expected a single navigation request')
                            request_stream = event.stream_id
                            record['headers'].append([(k.decode('ascii'), v.decode('latin1')) for k, v in event.headers])
                            h2.send_headers(event.stream_id, [(':status', '200'), ('content-type', 'text/html')])
                            h2.send_data(event.stream_id, b'<html><link rel="icon" href="data:,"><body>capture complete</body></html>')
                            # Wait for an ordered acknowledgement instead of
                            # assuming HEADERS and WINDOW_UPDATE share a recv.
                            # Keep the response open so clients continue IO.
                            h2.ping(probe)
                        elif isinstance(event, PingAckReceived) and event.ping_data == probe:
                            acknowledged = True
                            h2.send_data(request_stream, b'', end_stream=True)
                    conn.sendall(h2.data_to_send())
                record['capture_boundary'] = 'ACK of server PING after request headers, before response END_STREAM'
                with self.server.result_lock:
                    if not self.server.received.is_set():
                        self.server.result = record
                        self.server.received.set()
        except (ConnectionError, TimeoutError, ssl.SSLError) as error:
            self.server.errors.append(str(error))
            return


class BrowserH2Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_browser_h2(context):
    context.set_alpn_protocols(['h2'])
    server = BrowserH2Server(('127.0.0.1', 0), BrowserH2Handler)
    server.context = context
    server.received = threading.Event()
    server.result_lock = threading.Lock()
    server.result = None
    server.errors = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'https://localhost:{server.server_address[1]}/'
