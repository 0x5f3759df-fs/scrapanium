"""Byte-exact HTTP/2 transfers across the new Firefox stream-window boundary."""
import socketserver
import threading
import pytest
from binding import Session, lib, request
from browser_lab import browser_certificate

pytestmark = pytest.mark.skipif(b'2.2.3-scrapanium.1' not in lib.sp_backend_version(),
                               reason='requires the optional browser backend')
SIZE = 13 * 1024 * 1024 + 17


class FlowHandler(socketserver.BaseRequestHandler):
    def handle(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import RequestReceived, WindowUpdated
        record = {'requests': [], 'windows': [], 'overlap': False, 'sent': {}}
        self.server.records.append(record)
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as conn:
                conn.settimeout(10)
                h2 = H2Connection(config=H2Configuration(client_side=False))
                h2.initiate_connection()
                conn.sendall(h2.data_to_send())
                pending = {}
                while True:
                    data = conn.recv(65536)
                    if not data:
                        return
                    for event in h2.receive_data(data):
                        if isinstance(event, WindowUpdated):
                            record['windows'].append((event.stream_id, event.delta))
                        elif isinstance(event, RequestReceived):
                            path = dict(event.headers)[b':path']
                            record['requests'].append(event.stream_id)
                            h2.send_headers(event.stream_id, [(':status', '200')])
                            if path == b'/warm':
                                h2.send_data(event.stream_id, b'warm', end_stream=True)
                            else:
                                # Different deterministic bytes on each stream
                                # detect stream-owner mixups, loss and corruption.
                                seed = int(path[1:])
                                pending[event.stream_id] = [seed, 0]
                    record['overlap'] |= len(pending) > 1
                    # Wait until all requested streams exist before sending;
                    # otherwise a serialized implementation could pass unnoticed.
                    if len(record['requests']) == 4:
                        while pending:
                            progressed = False
                            for stream, (seed, at) in list(pending.items()):
                                length = min(h2.local_flow_control_window(stream),
                                             h2.max_outbound_frame_size, SIZE - at)
                                if length <= 0:
                                    continue
                                chunk = bytes(((at + i + seed) % 251 for i in range(length)))
                                done = at + length == SIZE
                                h2.send_data(stream, chunk, end_stream=done)
                                record['sent'][stream] = at + length
                                if done:
                                    del pending[stream]
                                else:
                                    pending[stream][1] += length
                                progressed = True
                            conn.sendall(h2.data_to_send())
                            if not progressed:
                                break
                    conn.sendall(h2.data_to_send())
        except Exception as error:
            self.server.errors.append(repr(error))


class FlowServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def test_firefox_multiplexed_streams_exceed_initial_window_losslessly(tmp_path):
    context, ca, _ = browser_certificate(tmp_path)
    context.set_alpn_protocols(['h2'])
    server = FlowServer(('127.0.0.1', 0), FlowHandler)
    server.context, server.records, server.errors = context, [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'https://localhost:{server.server_address[1]}'
    try:
        with Session(profile='firefox156', ca_bundle=str(ca), concurrency=3,
                     timeout_ms=30000) as session:
            with session.send(url + '/warm') as response:
                assert response.error == 0 and response.body == b'warm'
            responses = session.batch([request(url + '/' + str(seed)) for seed in (1, 2, 3)])
            try:
                for seed, response in zip((1, 2, 3), responses):
                    assert response.error == 0, (response.message, server.errors)
                    block = bytes((i + seed) % 251 for i in range(251))
                    expected = (block * (SIZE // 251 + 1))[:SIZE]
                    assert response.body == expected
            finally:
                for response in responses:
                    response.close()
        assert not server.errors
        assert len(server.records) == 1, 'expected three multiplexed streams on the warmed connection'
        record = server.records[0]
        assert record['requests'] == [3, 5, 7, 9]
        assert record['overlap']
        for stream in record['requests']:
            assert (stream, 12451840) in record['windows']
        assert record['sent'] == {5: SIZE, 7: SIZE, 9: SIZE}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_backend_multiplexed_windows_belong_to_each_request(tmp_path):
    """Different targets expose accidentally using the connection's active easy."""
    import asyncio
    import io
    from curl_cffi import AsyncCurl, Curl, CurlOpt, CurlMOpt
    context, ca, _ = browser_certificate(tmp_path)
    context.set_alpn_protocols(['h2'])
    server = FlowServer(('127.0.0.1', 0), FlowHandler)
    server.context, server.records, server.errors = context, [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'https://localhost:{server.server_address[1]}'
    targets = [2 * 1024 * 1024, 6 * 1024 * 1024, 12 * 1024 * 1024]

    async def transfer():
        multi = AsyncCurl(cacert=str(ca))
        multi.setopt(CurlMOpt.MAX_HOST_CONNECTIONS, 1)
        handles, outputs = [], []
        def prepare(path, target):
            curl = Curl(cacert=str(ca))
            handles.append(curl)
            sink = io.BytesIO()
            curl.impersonate('firefox147')
            curl.setopt(CurlOpt.URL, url + path)
            curl.setopt(CurlOpt.PROXY, '')
            curl.setopt(CurlOpt.TIMEOUT_MS, 30000)
            curl.setopt(CurlOpt.WRITEDATA, sink)
            curl.setopt(1044, target)
            curl.setopt(1045, 3)
            return curl, sink
        try:
            warm, sink = prepare('/warm', targets[0])
            await multi.add_handle(warm)
            assert sink.getvalue() == b'warm'
            futures = []
            for seed, target in enumerate(targets, 1):
                curl, sink = prepare('/' + str(seed), target)
                outputs.append(sink)
                futures.append(multi.add_handle(curl))
            await asyncio.gather(*futures)
            for seed, sink in enumerate(outputs, 1):
                block = bytes((i + seed) % 251 for i in range(251))
                assert sink.getvalue() == (block * (SIZE // 251 + 1))[:SIZE]
        finally:
            await multi.close()
            for curl in handles:
                curl.close()
    try:
        asyncio.run(transfer())
        assert not server.errors
        assert len(server.records) == 1
        record = server.records[0]
        assert record['requests'] == [3, 5, 7, 9] and record['overlap']
        for stream, target in zip((5, 7, 9), targets):
            first = next(delta for sid, delta in record['windows'] if sid == stream)
            assert first == target - 131072
        assert record['sent'] == {5: SIZE, 7: SIZE, 9: SIZE}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
