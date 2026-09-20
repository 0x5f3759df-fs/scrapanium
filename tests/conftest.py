import pytest
from lab import certificate, start_http, start_proxy, start_h2

@pytest.fixture(scope="session")
def http():
    server, url = start_http()
    other, other_url = start_http()
    server.cross_url = other_url
    yield url
    server.shutdown(); server.server_close(); other.shutdown(); other.server_close()

@pytest.fixture(scope="session")
def https(tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("tls"))
    server, url = start_http(context)
    yield url, ca
    server.shutdown(); server.server_close()

@pytest.fixture(scope="session")
def h2server(tmp_path_factory):
    context, ca = certificate(tmp_path_factory.mktemp("h2"))
    server, url = start_h2(context)
    yield server, url, ca
    server.shutdown(); server.server_close()

@pytest.fixture
def proxy():
    server, url = start_proxy()
    yield server, url
    server.shutdown(); server.server_close()
