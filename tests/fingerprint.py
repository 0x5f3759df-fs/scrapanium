"""Parse actual ClientHello records, normalizing only ephemeral data/GREASE.

This is structural parity evidence against the pinned reference, not a claim
that any reference profile exactly matches a current real browser.
"""
import socket
import struct
import threading

def grease(n): return (n & 0x0f0f) == 0x0a0a and n >> 8 == n & 255
def words(data): return list(struct.unpack(">" + "H" * (len(data) // 2), data))
def stable(values): return [x for x in values if not grease(x)]

def parse_hello(data):
    assert data[0] == 1, "expected ClientHello"
    body = data[4:]
    version = int.from_bytes(body[:2], "big")
    pos = 34
    pos += 1 + body[pos]
    n = int.from_bytes(body[pos:pos + 2], "big"); pos += 2
    raw_ciphers = words(body[pos:pos + n])
    ciphers = stable(raw_ciphers); pos += n
    pos += 1 + body[pos]
    n = int.from_bytes(body[pos:pos + 2], "big"); pos += 2
    end = pos + n
    extensions, details, grease_extensions, grease_signatures = [], {}, 0, 0
    while pos < end:
        kind, length = struct.unpack(">HH", body[pos:pos + 4]); pos += 4
        payload = body[pos:pos + length]; pos += length
        if grease(kind):
            grease_extensions += 1
            continue
        extensions.append(kind)
        if kind in (10, 13, 50):
            details[str(kind)] = stable(words(payload[2:]))
            if kind == 13: grease_signatures = sum(grease(x) for x in words(payload[2:]))
        elif kind == 43: details[str(kind)] = stable(words(payload[1:]))
        elif kind == 51:
            groups, p = [], 2
            while p < len(payload):
                group, size = struct.unpack(">HH", payload[p:p + 4]); p += 4 + size
                if not grease(group): groups.append(group)
            details[str(kind)] = groups
        elif kind in (11, 16, 27, 28, 35, 45, 17513, 17613, 65281):
            details[str(kind)] = payload.hex()
        elif kind == 51764:
            anchors, at = [], 2
            assert int.from_bytes(payload[:2], "big") == len(payload) - 2
            while at < len(payload):
                size = payload[at]; at += 1
                anchors.append(payload[at:at + size].hex()); at += size
            details[str(kind)] = sorted(anchors)
        # SNI, session ID, random, key-share bytes, ECH and padding vary by request.
    result = {"legacy_version": version, "ciphers": ciphers,
            "extension_order": extensions, "extensions": sorted(extensions), "details": details,
            "grease_ciphers": sum(grease(x) for x in raw_ciphers), "grease_extensions": grease_extensions}
    if grease_signatures: result["grease_signature_algorithms"] = grease_signatures
    return result

def capture(call):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen(); listener.settimeout(10)
    out, failures = [], []
    def receive():
        try:
            with listener.accept()[0] as conn:
                conn.settimeout(10)
                def exact(n):
                    parts = bytearray()
                    while len(parts) < n:
                        block = conn.recv(n - len(parts))
                        if not block: raise EOFError()
                        parts.extend(block)
                    return bytes(parts)
                data = bytearray()
                while len(data) < 4 or len(data) < 4 + int.from_bytes(data[1:4], "big"):
                    header = exact(5)
                    assert header[0] == 22
                    data.extend(exact(int.from_bytes(header[3:], "big")))
                out.append(parse_hello(bytes(data)))
        except BaseException as error: failures.append(error)
    thread = threading.Thread(target=receive, daemon=True); thread.start()
    try:
        call(f"https://localhost:{listener.getsockname()[1]}/")
        thread.join(11)
        if failures: raise failures[0]
        assert out, "ClientHello capture did not finish"
        return out[0]
    finally: listener.close()

def normalized(hello):
    out = {k: v for k, v in hello.items() if k != "extension_order"}
    # RFC 7685 padding is conditional on ClientHello length. ECH GREASE varies
    # that length even for the same backend/profile; preserve it in raw captures.
    out["extensions"] = [x for x in hello["extensions"] if x != 21]
    return out
