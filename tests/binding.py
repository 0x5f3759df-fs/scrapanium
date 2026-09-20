"""ctypes test/benchmark harness; not the product API."""
import ctypes as C
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lib = C.CDLL(str(ROOT / "build/libscrapanium.so"))

class Fingerprint(C.Structure):
    _fields_ = [(x, C.c_char_p) for x in ("ciphers", "curves", "signature_algorithms", "extension_order",
        "http2_settings", "pseudo_header_order", "cert_compression")] + [("grease", C.c_int),
        ("permute_extensions", C.c_int), ("http2_window_update", C.c_uint32)]

class Config(C.Structure):
    _fields_ = [(x, C.c_char_p) for x in ("profile", "proxy", "ca_bundle")] + [
        (x, C.c_uint32) for x in ("timeout_ms", "connect_timeout_ms", "max_redirects", "concurrency")
    ] + [(x, C.c_size_t) for x in ("max_body_bytes", "max_header_bytes")] + [
        (x, C.c_int) for x in ("verify", "follow_redirects", "default_headers")] + [("fingerprint", Fingerprint)] + [
        (x, C.c_size_t) for x in ("max_batch_bytes", "max_batch_requests")]

class Request(C.Structure):
    _fields_ = [("method", C.c_char_p), ("url", C.c_char_p),
                ("headers", C.POINTER(C.c_char_p)), ("header_count", C.c_size_t),
                ("body", C.c_void_p), ("body_size", C.c_size_t)]

def declare(name, ret, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = ret, list(args)
    return fn

declare("sp_config_default", None, C.POINTER(Config))
declare("sp_session_new", C.c_void_p, C.POINTER(Config), C.POINTER(C.c_int))
declare("sp_session_free", None, C.c_void_p)
declare("sp_session_request", C.c_void_p, C.c_void_p, C.POINTER(Request))
declare("sp_session_batch", C.c_int, C.c_void_p, C.POINTER(Request), C.c_size_t, C.POINTER(C.c_void_p))
declare("sp_cancel_new", C.c_void_p)
for name in ("retain", "free", "trigger"):
    declare("sp_cancel_" + name, None, C.c_void_p)
declare("sp_cancel_is_triggered", C.c_int, C.c_void_p)
declare("sp_session_request_cancel", C.c_void_p, C.c_void_p, C.POINTER(Request), C.c_void_p)
declare("sp_session_batch_cancel", C.c_int, C.c_void_p, C.POINTER(Request), C.c_size_t, C.POINTER(C.c_void_p), C.c_void_p)
Sink = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_size_t, C.c_void_p)
declare("sp_session_stream", C.c_void_p, C.c_void_p, C.POINTER(Request), Sink, C.c_void_p, C.c_void_p)
declare("sp_session_download", C.c_void_p, C.c_void_p, C.POINTER(Request), C.c_char_p, C.c_void_p)
declare("sp_response_free", None, C.c_void_p)
for name in ("error", "curl_code"):
    declare("sp_response_" + name, C.c_int, C.c_void_p)
for name in ("status", "http_version", "new_connections"):
    declare("sp_response_" + name, C.c_long, C.c_void_p)
for name in ("message", "headers", "url"):
    declare("sp_response_" + name, C.c_char_p, C.c_void_p)
declare("sp_response_body", C.c_void_p, C.c_void_p)
declare("sp_response_size", C.c_size_t, C.c_void_p)
declare("sp_response_headers_size", C.c_size_t, C.c_void_p)
declare("sp_response_elapsed_us", C.c_uint64, C.c_void_p)
declare("sp_response_downloaded", C.c_uint64, C.c_void_p)
declare("sp_response_memory_bytes", C.c_size_t, C.c_void_p)
declare("sp_backend_version", C.c_char_p)

def request(url, method="GET", body=None, headers=()):
    data = body.encode() if isinstance(body, str) else body
    refs = [url.encode(), method.encode()]
    hs = (C.c_char_p * len(headers))(*(h.encode() for h in headers))
    buf = C.create_string_buffer(data) if data is not None else None
    q = Request(refs[1], refs[0], hs, len(headers), C.cast(buf, C.c_void_p) if buf else None, len(data or b""))
    q.refs = (refs, hs, buf)
    return q

class Response:
    def __init__(self, ptr):
        if not ptr:
            raise MemoryError("native response allocation failed")
        self.ptr = ptr
    def __getattr__(self, name):
        if name == "body":
            return C.string_at(lib.sp_response_body(self.ptr), self.size)
        return getattr(lib, "sp_response_" + name)(self.ptr)
    def close(self):
        if self.ptr:
            lib.sp_response_free(self.ptr)
            self.ptr = None
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

class Cancel:
    def __init__(self):
        self.ptr = lib.sp_cancel_new()
        if not self.ptr: raise MemoryError()
    def trigger(self): lib.sp_cancel_trigger(self.ptr)
    def close(self):
        lib.sp_cancel_free(self.ptr)
        self.ptr = None
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

class Session:
    def __init__(self, **options):
        config = Config()
        lib.sp_config_default(C.byref(config))
        for k, v in options.items():
            target = config.fingerprint if k.startswith("fp_") else config
            setattr(target, k.removeprefix("fp_"), v.encode() if isinstance(v, str) else v)
        error = C.c_int()
        self.ptr = lib.sp_session_new(C.byref(config), C.byref(error))
        if not self.ptr: raise ValueError(error.value)
    def send(self, url, cancel=None, **kw):
        q = request(url, **kw)
        return Response(lib.sp_session_request_cancel(self.ptr, C.byref(q), cancel.ptr if cancel else None))
    def stream(self, url, sink, cancel=None, **kw):
        q = request(url, **kw)
        callback = Sink(lambda data, n, _: sink(C.string_at(data, n)))
        return Response(lib.sp_session_stream(self.ptr, C.byref(q), callback, None, cancel.ptr if cancel else None))
    def download(self, url, path, cancel=None, **kw):
        q = request(url, **kw)
        return Response(lib.sp_session_download(self.ptr, C.byref(q), str(path).encode(), cancel.ptr if cancel else None))
    def batch(self, requests, cancel=None):
        qs = (Request * len(requests))(*requests)
        out = (C.c_void_p * len(requests))()
        error = lib.sp_session_batch_cancel(self.ptr, qs, len(qs), out, cancel.ptr if cancel else None)
        if error:
            for p in out: lib.sp_response_free(p)
            raise RuntimeError(error)
        return [Response(p) for p in out]
    def close(self):
        if self.ptr:
            lib.sp_session_free(self.ptr)
            self.ptr = None
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
