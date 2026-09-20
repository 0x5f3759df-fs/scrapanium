/* RFC 6455's challenge checksum is SHA-1(key || GUID). It is NOT used for TLS,
 * signatures, password hashing, or authentication. The input is always 60
 * bytes (24-byte base64 nonce plus the 36-byte protocol GUID). */
#include <sys/random.h>
static uint32_t sp_ws_rotl(uint32_t x, unsigned n) { return (x << n) | (x >> (32 - n)); }
static void sp_ws_base64(const unsigned char *data, size_t size, char *out) {
  static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  size_t at = 0;
  for (size_t i = 0; i < size; i += 3) {
    uint32_t v = (uint32_t)data[i] << 16;
    if (i + 1 < size) v |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < size) v |= data[i + 2];
    out[at++] = alphabet[v >> 18]; out[at++] = alphabet[(v >> 12) & 63];
    out[at++] = i + 1 < size ? alphabet[(v >> 6) & 63] : '=';
    out[at++] = i + 2 < size ? alphabet[v & 63] : '=';
  }
  out[at] = 0;
}
static void sp_ws_accept(const char key[24], char out[29]) {
  unsigned char blocks[128] = {0}, digest[20];
  memcpy(blocks, key, 24); memcpy(blocks + 24, "258EAFA5-E914-47DA-95CA-C5AB0DC85B11", 36);
  blocks[60] = 0x80; blocks[126] = 1; blocks[127] = 0xe0; /* 60 * 8 bits */
  uint32_t h[] = {0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0};
  for (unsigned block = 0; block < 2; block++) {
    uint32_t schedule[80]; const unsigned char *p = blocks + block * 64;
    for (unsigned i = 0; i < 16; i++) schedule[i] = (uint32_t)p[4*i] << 24 | (uint32_t)p[4*i+1] << 16 | (uint32_t)p[4*i+2] << 8 | p[4*i+3];
    for (unsigned i = 16; i < 80; i++) schedule[i] = sp_ws_rotl(schedule[i-3] ^ schedule[i-8] ^ schedule[i-14] ^ schedule[i-16], 1);
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
    for (unsigned i = 0; i < 80; i++) {
      uint32_t f = i < 20 ? (b & c) | (~b & d) : i < 40 ? b ^ c ^ d : i < 60 ? (b & c) | (b & d) | (c & d) : b ^ c ^ d;
      uint32_t k = i < 20 ? 0x5a827999 : i < 40 ? 0x6ed9eba1 : i < 60 ? 0x8f1bbcdc : 0xca62c1d6;
      uint32_t temp = sp_ws_rotl(a, 5) + f + e + k + schedule[i];
      e = d; d = c; c = sp_ws_rotl(b, 30); b = a; a = temp;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
  }
  for (unsigned i = 0; i < 20; i++) digest[i] = (unsigned char)(h[i / 4] >> (24 - 8 * (i % 4)));
  sp_ws_base64(digest, sizeof digest, out);
}
static int sp_ws_nonce(char key[25]) {
  unsigned char bytes[16]; size_t at = 0;
  while (at < sizeof bytes) {
    ssize_t n = getrandom(bytes + at, sizeof bytes - at, 0);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return SP_BACKEND;
    at += (size_t)n;
  }
  sp_ws_base64(bytes, sizeof bytes, key); return SP_OK;
}
static int sp_ws_protocol_offered(const sp_request *q, const char *value, size_t size) {
  for (size_t i = 0; i < q->header_count; i++) {
    const char *h = q->headers[i];
    if (strncasecmp(h, "Sec-WebSocket-Protocol:", 23)) continue;
    const char *p = h + 23;
    while (*p) {
      while (*p == ' ' || *p == '\t') p++;
      const char *end = strchr(p, ','); if (!end) end = p + strlen(p);
      const char *trim = end; while (trim > p && (trim[-1] == ' ' || trim[-1] == '\t')) trim--;
      if ((size_t)(trim - p) == size && !memcmp(p, value, size)) return 1;
      p = *end ? end + 1 : end;
    }
  }
  return 0;
}
static int sp_ws_verify_handshake(const sp_request *q, const sp_buffer *headers, const char expected[29]) {
  const char *start = (const char *)headers->data;
  int accepts = 0, protocols = 0;
  if (!start) return SP_PROTOCOL;
  const char *end = start + headers->size;
  /* Ignore earlier CONNECT / informational response blocks. */
  for (const char *p = start; p < end;) {
    const char *line = memchr(p, '\n', (size_t)(end - p)); if (!line) return SP_PROTOCOL;
    if (line - p >= 9 && !memcmp(p, "HTTP/1.", 7)) { start = line + 1; accepts = 0; protocols = 0; }
    p = line + 1;
  }
  for (const char *p = start; p < end;) {
    const char *line = memchr(p, '\n', (size_t)(end - p)); if (!line) return SP_PROTOCOL;
    const char *stop = line; if (stop > p && stop[-1] == '\r') stop--;
    if (stop == p) break;
    const char *colon = memchr(p, ':', (size_t)(stop - p)); if (!colon) return SP_PROTOCOL;
    const char *value = colon + 1; while (value < stop && (*value == ' ' || *value == '\t')) value++;
    while (stop > value && (stop[-1] == ' ' || stop[-1] == '\t')) stop--;
    size_t name = (size_t)(colon - p), n = (size_t)(stop - value);
    if (name == 20 && !strncasecmp(p, "Sec-WebSocket-Accept", 20)) {
      if (++accepts != 1 || n != 28 || memcmp(value, expected, 28)) return SP_PROTOCOL;
    } else if (name == 24 && !strncasecmp(p, "Sec-WebSocket-Extensions", 24)) return SP_PROTOCOL;
    else if (name == 22 && !strncasecmp(p, "Sec-WebSocket-Protocol", 22)) {
      if (++protocols != 1 || !n || !sp_ws_protocol_offered(q, value, n)) return SP_PROTOCOL;
    }
    p = line + 1;
  }
  return accepts == 1 ? SP_OK : SP_PROTOCOL;
}
