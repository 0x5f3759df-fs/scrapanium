"""Exercise the patched backend itself, including duplication and reuse."""
from contextlib import closing
import io
import pytest
from curl_cffi import Curl, CurlError, CurlOpt
from binding import lib
from fingerprint import capture

pytestmark = pytest.mark.skipif(b'2.2.3-scrapanium.1' not in lib.sp_backend_version(),
                               reason='requires the optional browser backend')
OPTIONS = {'grease': 1042, 'padding': 1043, 'window': 1044, 'stream': 1045}


@pytest.mark.parametrize('name,valid,invalid', [
    ('grease', [0, 1], [-1, 2]), ('padding', [-1, 0, 65535], [-2, 65536]),
    ('window', [0, 1, 2147483647], [-1, 2147483648]),
    ('stream', [0, 1, 3, 2147483647], [-1, 2, 2147483648]),
])
def test_option_boundaries(name, valid, invalid):
    with closing(Curl()) as curl:
        for value in valid:
            assert curl.setopt(OPTIONS[name], value) == 0
        for value in invalid:
            with pytest.raises(CurlError) as error:
                curl.setopt(OPTIONS[name], value)
            assert error.value.code == 43  # CURLE_BAD_FUNCTION_ARGUMENT


def hello(curl):
    def send(url):
        curl.setopt(CurlOpt.URL, url)
        curl.setopt(CurlOpt.TIMEOUT_MS, 5000)
        curl.setopt(CurlOpt.PROXY, '')
        with pytest.raises(CurlError):
            curl.perform()
    return capture(send, detailed=True)


def test_tls_options_survive_duplication_and_clear_on_reset():
    with closing(Curl()) as original:
        original.impersonate('chrome150')
        original.setopt(OPTIONS['grease'], 1)
        original.setopt(OPTIONS['padding'], 0)
        with closing(original.duphandle()) as duplicate:
            for curl in (original, duplicate):
                got = hello(curl)
                assert got['grease_signature_algorithms'] == 1
                assert got['static_extension_payloads']['4832'] == '0000'
            duplicate.reset()
            duplicate.impersonate('chrome150')
            got = hello(duplicate)
            assert '4832' not in got['static_extension_payloads']
            assert got.get('grease_signature_algorithms', 0) == 0
        # Resetting the duplicate must leave the original's options intact.
        assert hello(original)['static_extension_payloads']['4832'] == '0000'


def test_changed_tls_controls_do_not_reuse_old_connection(https):
    url, ca = https
    with closing(Curl(cacert=ca)) as curl:
        curl.impersonate('chrome150')
        curl.setopt(CurlOpt.URL, url + '/echo')
        curl.setopt(CurlOpt.PROXY, '')
        from curl_cffi import CurlInfo
        for grease, padding in [(0, -1), (1, -1), (1, 0), (0, 0), (0, -1)]:
            curl.setopt(OPTIONS['grease'], grease)
            curl.setopt(OPTIONS['padding'], padding)
            # Low-level Curl.perform releases its Python callback handles.
            # Supply a fresh retained writer for every perform on this handle.
            curl.setopt(CurlOpt.WRITEDATA, io.BytesIO())
            curl.perform()
            # The final configuration may legitimately reuse the first one.
            if (grease, padding) != (0, -1):
                assert curl.getinfo(CurlInfo.NUM_CONNECTS) == 1
            curl.setopt(CurlOpt.WRITEDATA, io.BytesIO())
            curl.perform()
            assert curl.getinfo(CurlInfo.NUM_CONNECTS) == 0


def test_stream_window_below_settings_is_rejected_before_headers(h2server):
    server, url, ca = h2server
    before = len(server.headers)
    with closing(Curl(cacert=ca)) as curl:
        curl.impersonate('firefox147')
        curl.setopt(CurlOpt.URL, url)
        curl.setopt(CurlOpt.PROXY, '')
        curl.setopt(CurlOpt.WRITEDATA, io.BytesIO())
        curl.setopt(OPTIONS['window'], 1)
        with pytest.raises(CurlError) as error:
            curl.perform()
        assert error.value.code == 43
    assert len(server.headers) == before
