#define _POSIX_C_SOURCE 200809L
#include <curl/curl.h>
#include <curl/easy.h>
#include <curl/websockets.h>
#include <poll.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PAYLOAD_SIZE (1024u * 1024u)
#define GUARD_SIZE 16u

typedef struct curl_ws_frame ws_meta;

static void fail(const char *what, CURLcode code) {
  fprintf(stderr, "%s: %d (%s)\n", what, (int)code, curl_easy_strerror(code));
  exit(2);
}

static void require(int ok, const char *what) {
  if(!ok) {
    fprintf(stderr, "assertion failed: %s\n", what);
    exit(2);
  }
}

static curl_socket_t connect_ws(const char *url, const char *ca, CURL **out, int no_autopong) {
  CURL *curl = curl_easy_init();
  require(curl != NULL, "curl_easy_init");
  CURLcode code = curl_easy_impersonate(curl, "chrome146", 0);
  if(code) fail("curl_easy_impersonate(chrome146)", code);
  if((code = curl_easy_setopt(curl, CURLOPT_URL, url))) fail("CURLOPT_URL", code);
  if((code = curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L))) fail("CURLOPT_CONNECT_ONLY", code);
  if((code = curl_easy_setopt(curl, CURLOPT_HTTP_VERSION, (long)CURL_HTTP_VERSION_1_1))) fail("CURLOPT_HTTP_VERSION", code);
  if((code = curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L))) fail("CURLOPT_SSL_VERIFYPEER", code);
  if((code = curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L))) fail("CURLOPT_SSL_VERIFYHOST", code);
  if((code = curl_easy_setopt(curl, CURLOPT_CAINFO, ca))) fail("CURLOPT_CAINFO", code);
  if(no_autopong && (code = curl_easy_setopt(curl, CURLOPT_WS_OPTIONS, (long)CURLWS_NOAUTOPONG))) fail("CURLOPT_WS_OPTIONS", code);
  if((code = curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L))) fail("CURLOPT_CONNECTTIMEOUT_MS", code);
  if((code = curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L))) fail("CURLOPT_TIMEOUT_MS", code);
  code = curl_easy_perform(curl);
  if(code) fail("curl_easy_perform(WebSocket upgrade)", code);
  curl_socket_t fd = CURL_SOCKET_BAD;
  if((code = curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &fd))) fail("CURLINFO_ACTIVESOCKET", code);
  require(fd != CURL_SOCKET_BAD, "active WebSocket socket");
  *out = curl;
  return fd;
}

static void wait_ready(curl_socket_t fd, short events) {
  struct pollfd pfd = {.fd = fd, .events = events};
  int rc;
  do { rc = poll(&pfd, 1, 5000); } while(rc < 0 && errno == EINTR);
  require(rc == 1, "WebSocket socket became ready within 5 seconds");
  require(!(pfd.revents & (POLLERR | POLLNVAL)), "socket has no poll error");
}

static CURLcode recv_one(CURL *curl, curl_socket_t fd, void *buffer, size_t capacity,
                         size_t *got, const ws_meta **meta) {
  for(unsigned attempt = 0; attempt < 10000; ++attempt) {
    CURLcode code = curl_ws_recv(curl, buffer, capacity, got, meta);
    if(code != CURLE_AGAIN) return code;
    require(*got == 0 && *meta == NULL, "CURLE_AGAIN carries no caller-visible bytes or metadata");
    wait_ready(fd, POLLIN);
  }
  fprintf(stderr, "too many CURLE_AGAIN results\n");
  exit(2);
}

static void send_all(CURL *curl, curl_socket_t fd, const unsigned char *data,
                     size_t size, unsigned flags) {
  size_t offset = 0;
  do {
    size_t sent = 0;
    CURLcode code = curl_ws_send(curl, data ? data + offset : NULL,
                                 size - offset, &sent, 0, flags);
    offset += sent;
    if(code == CURLE_AGAIN) {
      wait_ready(fd, POLLOUT);
      continue;
    }
    if(code) fail("curl_ws_send", code);
    require(sent || offset == size, "successful send made progress");
  } while(offset < size);
}

static void check_guards(const unsigned char *storage, size_t capacity) {
  for(size_t i = 0; i < GUARD_SIZE; ++i) {
    require(storage[i] == 0xa5, "leading caller-buffer guard unchanged");
    require(storage[GUARD_SIZE + capacity + i] == 0x5a,
            "trailing caller-buffer guard unchanged");
  }
}

static void recv_large_and_boundary(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  unsigned char storage[65536 + 2 * GUARD_SIZE];
  size_t offset = 0, calls = 0;
  memset(storage, 0xa5, GUARD_SIZE);
  memset(storage + GUARD_SIZE, 0xcc, 65536);
  memset(storage + GUARD_SIZE + 65536, 0x5a, GUARD_SIZE);

  /* A zero-capacity query must report metadata without consuming a nonempty frame. */
  size_t got = 99;
  const ws_meta *meta = NULL;
  CURLcode code = recv_one(curl, fd, NULL, 0, &got, &meta);
  if(code) fail("zero-capacity curl_ws_recv", code);
  require(meta != NULL && got == 0, "zero-capacity call returns metadata and no bytes");
  require((meta->flags & CURLWS_BINARY) != 0, "large frame opcode is binary");
  require(meta->offset == 0 && meta->bytesleft == PAYLOAD_SIZE,
          "zero-capacity query preserves the first frame offset and remainder");

  const size_t caps[] = {1, 13, 125, 16384, 32768, 65536, 7, 8191};
  while(offset < PAYLOAD_SIZE) {
    size_t cap = caps[calls % (sizeof(caps) / sizeof(caps[0]))];
    if(cap > PAYLOAD_SIZE - offset) cap = PAYLOAD_SIZE - offset;
    memset(storage + GUARD_SIZE, 0xcc, cap);
    memset(storage + GUARD_SIZE + cap, 0x5a, GUARD_SIZE);
    got = 0; meta = NULL;
    code = recv_one(curl, fd, storage + GUARD_SIZE, cap, &got, &meta);
    if(code) fail("curl_ws_recv large frame", code);
    require(meta != NULL, "successful data call has metadata");
    require(got <= cap, "curl_ws_recv respects caller capacity");
    require(meta->len == got, "metadata length matches returned bytes");
    require(meta->offset == (curl_off_t)offset, "metadata offset advances exactly");
    require(meta->bytesleft == (curl_off_t)(PAYLOAD_SIZE - offset - got),
            "metadata remainder stays within this frame");
    require((meta->flags & CURLWS_BINARY) != 0, "all chunks retain binary opcode");
    require(got || !cap, "positive-capacity call made progress");
    for(size_t i = 0; i < got; ++i)
      require(storage[GUARD_SIZE + i] == (unsigned char)((offset + i) & 255),
              "large-frame payload byte matches expected sequence");
    check_guards(storage, cap);
    offset += got;
    calls++;
  }
  require(calls > 8, "test exercised multiple caller capacities");
  require(meta && meta->bytesleft == 0, "large frame completed exactly");

  /* A later distinct message checks that metadata resets for the next message. */
  static const unsigned char next[] = {'n', 'e', 'x', 't', 0, 255};
  send_all(curl, fd, next, sizeof next, CURLWS_BINARY);
  unsigned char next_buf[sizeof next + 2 * GUARD_SIZE];
  memset(next_buf, 0xa5, GUARD_SIZE);
  memset(next_buf + GUARD_SIZE, 0xcc, sizeof next);
  memset(next_buf + GUARD_SIZE + sizeof next, 0x5a, GUARD_SIZE);
  offset = 0;
  while(offset < sizeof next) {
    size_t cap = sizeof next - offset;
    got = 0; meta = NULL;
    code = recv_one(curl, fd, next_buf + GUARD_SIZE + offset, cap, &got, &meta);
    if(code) fail("curl_ws_recv boundary message", code);
    require(meta != NULL && meta->offset == (curl_off_t)offset,
            "next frame begins at offset zero and advances independently");
    require((meta->flags & CURLWS_BINARY) && meta->bytesleft == (curl_off_t)(sizeof next - offset - got),
            "next frame metadata is independent from previous frame");
    offset += got;
  }
  require(!memcmp(next_buf + GUARD_SIZE, next, sizeof next),
          "next frame payload is exact and not mixed with previous frame");
  check_guards(next_buf, sizeof next);

  /* Empty payload is consumed by a zero-capacity metadata call. */
  send_all(curl, fd, NULL, 0, CURLWS_BINARY);
  got = 99; meta = NULL;
  code = recv_one(curl, fd, NULL, 0, &got, &meta);
  if(code) fail("curl_ws_recv empty frame", code);
  require(meta && got == 0 && (meta->flags & CURLWS_BINARY),
          "zero-capacity call consumes an empty binary frame with metadata");
  require(meta->offset == 0 && meta->bytesleft == 0 && meta->len == 0,
          "empty frame metadata has zero offset, length, and remainder");

  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  curl_easy_cleanup(curl);
  puts("direct receive: capacities, metadata, canaries, empty frame, and frame boundary passed");
}

static void recv_queued_frames(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  static const unsigned char first[] = "queued-first";
  static const unsigned char second[] = "queued-second-frame";
  unsigned char buffer[65536 + 2 * GUARD_SIZE];
  const unsigned char *expected[] = {first, second};
  const size_t lengths[] = {sizeof(first) - 1, sizeof(second) - 1};
  for(size_t frame = 0; frame < 2; ++frame) {
    memset(buffer, 0xa5, GUARD_SIZE);
    memset(buffer + GUARD_SIZE, 0xcc, 65536);
    memset(buffer + GUARD_SIZE + 65536, 0x5a, GUARD_SIZE);
    size_t got = 0; const ws_meta *meta = NULL;
    CURLcode code = recv_one(curl, fd, buffer + GUARD_SIZE, 65536, &got, &meta);
    if(code) fail("curl_ws_recv prequeued frames", code);
    require(meta && got == lengths[frame], "one call returns exactly one completed frame");
    require(meta->offset == 0 && meta->bytesleft == 0 && meta->len == got,
            "prequeued frame metadata terminates at its own boundary");
    require((meta->flags & CURLWS_BINARY) != 0, "queued frame opcode remains binary");
    require(!memcmp(buffer + GUARD_SIZE, expected[frame], got),
            "prequeued frame payload is not merged with its neighbor");
    check_guards(buffer, 65536);
  }
  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  unsigned char close_echo[8]; size_t close_got = 0; const ws_meta *close_meta = NULL;
  CURLcode close_code = recv_one(curl, fd, close_echo, sizeof close_echo,
                                 &close_got, &close_meta);
  if(close_code) fail("curl_ws_recv close echo after queued-frame test", close_code);
  require(close_meta && (close_meta->flags & CURLWS_CLOSE) && close_got == sizeof close_payload &&
          close_meta->offset == 0 && close_meta->bytesleft == 0 &&
          !memcmp(close_echo, close_payload, sizeof close_payload),
          "queued-frame path completes the WebSocket close handshake");
  curl_easy_cleanup(curl);
  puts("direct receive: two prequeued frames stay separate with spare caller capacity");
}

static void recv_split_ping_autopong(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  static const unsigned char expected[] = "after-auto-pong";
  unsigned char buffer[64], ping[16]; size_t got = 0, ping_seen = 0;
  const ws_meta *meta = NULL; CURLcode code = CURLE_OK; int got_data = 0;
  for(unsigned attempt = 0; attempt < 32 && !got_data; ++attempt) {
    got = 0; meta = NULL;
    code = recv_one(curl, fd, buffer, sizeof buffer, &got, &meta);
    if(code) fail("curl_ws_recv after split PING", code);
    require(meta && got <= sizeof buffer && meta->len == got,
            "split-control receive returns coherent metadata");
    if(meta->flags & CURLWS_PING) {
      require(meta->offset == (curl_off_t)ping_seen &&
              meta->bytesleft == (curl_off_t)(10 - ping_seen - got) &&
              ping_seen + got <= 10,
              "visible split PING chunks have exact offsets and remainder");
      require(ping_seen + got <= sizeof ping, "split PING fits control buffer");
      memcpy(ping + ping_seen, buffer, got);
      for(size_t i = 0; i < got; ++i)
        require(buffer[i] == (unsigned char)"split-ping"[ping_seen + i],
                "visible split PING bytes preserve payload order");
      ping_seen += got;
      continue;
    }
    require((meta->flags & CURLWS_BINARY) != 0,
            "only the split PING and following binary message are returned");
    require(got == sizeof(expected) - 1 && meta->offset == 0 &&
            meta->bytesleft == 0,
            "following binary frame is complete and starts at offset zero");
    require(!memcmp(buffer, expected, sizeof(expected) - 1),
            "message after split PING is exact");
    got_data = 1;
  }
  require(got_data, "split PING processing reaches following binary frame");
  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  unsigned char close_echo[8]; got = 0; meta = NULL;
  code = recv_one(curl, fd, close_echo, sizeof close_echo, &got, &meta);
  if(code) fail("curl_ws_recv close echo after split-PING test", code);
  require(meta && (meta->flags & CURLWS_CLOSE) && got == sizeof close_payload &&
          meta->offset == 0 && meta->bytesleft == 0 &&
          !memcmp(close_echo, close_payload, sizeof close_payload),
          "split-PING path completes the WebSocket close handshake");
  curl_easy_cleanup(curl);
  printf("direct receive: split-PING auto-PONG observation only (caller saw %zu/10 PING bytes; no conformance claim)\n",
         ping_seen);
}

static void recv_stale_pending(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  unsigned char first[128], rest[2048];
  size_t got = 0; const ws_meta *meta = NULL;
  CURLcode code = recv_one(curl, fd, first, sizeof first, &got, &meta);
  if(code) fail("curl_ws_recv first partial frame bytes", code);
  require(meta && got > 0 && got <= 64 && meta->offset == 0 && meta->bytesleft == (curl_off_t)(2048 - got),
          "partial bytes return once while the peer holds the frame remainder");
  size_t offset = got;
  for(size_t i = 0; i < got; ++i)
    require(first[i] == (unsigned char)(i & 255), "first partial payload exact");

  /* Drain the whole known-available prefix before requiring CURLE_AGAIN: TLS
   * may split the peer's 64-byte write across multiple caller-visible reads. */
  int saw_again = 0;
  for(unsigned attempt = 0; attempt < 8; ++attempt) {
    unsigned char available[64]; got = 99; meta = NULL;
    code = curl_ws_recv(curl, available, sizeof available, &got, &meta);
    if(code == CURLE_AGAIN) {
      require(got == 0 && meta == NULL, "CURLE_AGAIN exposes no bytes or metadata");
      saw_again = 1;
      break;
    }
    if(code) fail("curl_ws_recv gated prefix", code);
    require(meta && got > 0 && meta->offset == (curl_off_t)offset &&
            meta->bytesleft == (curl_off_t)(2048 - offset - got),
            "each gated prefix chunk has exact frame metadata");
    require(offset + got <= 64, "gated reads do not pass the peer's 64-byte prefix");
    for(size_t i = 0; i < got; ++i)
      require(available[i] == (unsigned char)((offset + i) & 255),
              "gated prefix bytes are exact and ordered");
    offset += got;
  }
  require(offset == 64 && saw_again,
          "all 64 available bytes drain before the incomplete frame returns CURLE_AGAIN");

  static const unsigned char marker[] = {'r', 'e', 's', 'u', 'm', 'e'};
  send_all(curl, fd, marker, sizeof marker, CURLWS_PING);
  /* The peer only sends the rest after this PING. Once the socket is readable,
   * the first fresh call must deliver frame bytes, not a replayed CURLE_AGAIN. */
  wait_ready(fd, POLLIN);
  got = 0; meta = NULL;
  code = curl_ws_recv(curl, rest, sizeof rest, &got, &meta);
  if(code) fail("curl_ws_recv immediately after gated input resumes", code);
  require(meta && got > 0 && meta->offset == (curl_off_t)offset &&
          meta->bytesleft == (curl_off_t)(2048 - offset - got),
          "first readable resumed call returns fresh bytes at the prior offset");
  for(size_t i = 0; i < got; ++i)
    require(rest[i] == (unsigned char)((offset + i) & 255),
            "first resumed payload chunk is exact");
  offset += got;
  while(offset < 2048) {
    size_t chunk = sizeof rest;
    if(chunk > 2048 - offset) chunk = 2048 - offset;
    got = 0; meta = NULL;
    code = recv_one(curl, fd, rest, chunk, &got, &meta);
    if(code) fail("curl_ws_recv after pending resumes", code);
    require(meta && got && meta->offset == (curl_off_t)offset &&
            meta->bytesleft == (curl_off_t)(2048 - offset - got),
            "resumed receive advances from the exact prior offset");
    for(size_t i = 0; i < got; ++i)
      require(rest[i] == (unsigned char)((offset + i) & 255),
              "resumed payload has no loss or duplication");
    offset += got;
  }
  unsigned char pong[16]; got = 0; meta = NULL;
  code = recv_one(curl, fd, pong, sizeof pong, &got, &meta);
  if(code) fail("curl_ws_recv marker PONG after stale-pending test", code);
  require(meta && (meta->flags & CURLWS_PONG) && got == sizeof marker &&
          !memcmp(pong, marker, sizeof marker),
          "stale-pending marker receives its exact PONG before close");
  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  unsigned char close_echo[8]; got = 0; meta = NULL;
  code = recv_one(curl, fd, close_echo, sizeof close_echo, &got, &meta);
  if(code) fail("curl_ws_recv close echo after stale-pending test", code);
  require(meta && (meta->flags & CURLWS_CLOSE) && got == sizeof close_payload &&
          meta->offset == 0 && meta->bytesleft == 0 &&
          !memcmp(close_echo, close_payload, sizeof close_payload),
          "stale-pending path completes the WebSocket close handshake");
  curl_easy_cleanup(curl);
  puts("direct receive: stale pending→AGAIN returns partial bytes once and resumes cleanly");
}

static void recv_truncated_frame(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  unsigned char payload[16]; size_t total = 0; int terminal = 0, incomplete_seen = 0;
  for(unsigned attempt = 0; attempt < 100; ++attempt) {
    unsigned char chunk[16]; size_t got = 0; const ws_meta *meta = NULL;
    CURLcode code = curl_ws_recv(curl, chunk, sizeof chunk, &got, &meta);
    if(code == CURLE_AGAIN) {
      require(got == 0 && meta == NULL, "truncated CURLE_AGAIN has no caller-visible bytes");
      wait_ready(fd, POLLIN);
      continue;
    }
    if(code != CURLE_OK) { terminal = (int)code; break; }
    require(meta && (meta->flags & CURLWS_BINARY), "partial frame has binary metadata");
    require(total + got <= sizeof(payload), "truncated test data fits buffer");
    require(meta->offset == (curl_off_t)total, "partial frame offset is exact");
    require(meta->len == got, "partial frame metadata length is exact");
    if(meta->bytesleft) incomplete_seen = 1;
    memcpy(payload + total, chunk, got); total += got;
    require(got, "successful partial-frame call made progress");
  }
  require(terminal != 0, "truncated transport eventually reports a terminal error");
  require(incomplete_seen && total == 5 && !memcmp(payload, "abcde", 5),
          "partial bytes are delivered exactly once before EOF/fatal status");
  /* Record the later status without assuming that handler-close is a clean TLS
   * EOF or that curl must preserve one specific terminal code. */
  unsigned char ignored[1]; size_t got = 0; const ws_meta *meta = NULL;
  CURLcode later = curl_ws_recv(curl, ignored, sizeof ignored, &got, &meta);
  require(got == 0, "post-error call does not duplicate partial payload");
  printf("direct receive: handler-close truncated frame delivered exact partial bytes; terminal=%d next=%d\n",
         terminal, (int)later);
  curl_easy_cleanup(curl);
}

static const size_t fragments[] = {1, 124, 1, 1, 16257, 49151, 257};
#define FRAGMENT_COUNT (sizeof(fragments) / sizeof(fragments[0]))

static void send_pong(CURL *curl, curl_socket_t fd, const unsigned char *p, size_t n) {
  send_all(curl, fd, p, n, CURLWS_PONG);
}

static void recv_fragmented_controls(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 1);
  unsigned char storage[16384 + 2 * GUARD_SIZE];
  size_t data_frame = 0, ping_frame = 0, frame_offset = 0, payload_offset = 0;
  unsigned char control[125]; size_t control_size = 0;
  unsigned current_is_ping = 0;
  const size_t caps[] = {0, 1, 3, 17, 16384, 7};
  size_t calls = 0;
  while(data_frame < FRAGMENT_COUNT || ping_frame < FRAGMENT_COUNT - 1) {
    size_t cap = caps[calls % (sizeof(caps) / sizeof(caps[0]))];
    memset(storage, 0xa5, GUARD_SIZE);
    memset(storage + GUARD_SIZE, 0xcc, cap);
    memset(storage + GUARD_SIZE + cap, 0x5a, GUARD_SIZE);
    size_t got = 0; const ws_meta *meta = NULL;
    CURLcode code = recv_one(curl, fd, cap ? storage + GUARD_SIZE : NULL, cap, &got, &meta);
    if(code) fail("curl_ws_recv fragmented sequence", code);
    require(meta != NULL && got <= cap && meta->len == got,
            "fragmented receive has bounded bytes and coherent metadata");
    require(got || !cap, "positive-capacity fragmented call made progress");
    require(meta->offset == (curl_off_t)frame_offset,
            "frame offset increments independently through fragments and controls");
    int is_ping = (meta->flags & CURLWS_PING) != 0;
    if(is_ping) require(ping_frame < FRAGMENT_COUNT - 1, "no unexpected PING frame");
    else require(data_frame < FRAGMENT_COUNT, "no unexpected data frame");
    size_t current_frame_size = is_ping ? 3 : fragments[data_frame];
    require(meta->bytesleft == (curl_off_t)(current_frame_size - frame_offset - got),
            "bytesleft describes only the current frame");
    check_guards(storage, cap);
    if(meta->flags & CURLWS_PING) {
      require((meta->flags & (CURLWS_TEXT | CURLWS_BINARY | CURLWS_PONG | CURLWS_CLOSE)) == 0,
              "control PING is not misreported as data");
      current_is_ping = 1;
      for(size_t i = 0; i < got; ++i) {
        size_t pos = frame_offset + i;
        unsigned char expected = pos == 0 ? (unsigned char)ping_frame : pos == 1 ? 0 : 255;
        require(storage[GUARD_SIZE + i] == expected, "split PING payload preserved");
        control[pos] = expected;
      }
      require(control_size + got <= sizeof(control), "PING payload stays within the control limit");
      control_size += got;
    }
    else {
      require((meta->flags & CURLWS_BINARY) != 0, "fragment sequence remains binary");
      require(((meta->flags & CURLWS_CONT) != 0) == (data_frame + 1 < FRAGMENT_COUNT),
              "CURLWS_CONT marks every non-final data frame");
      current_is_ping = 0;
      for(size_t i = 0; i < got; ++i)
        require(storage[GUARD_SIZE + i] == (unsigned char)((payload_offset + i) & 255),
                "fragmented payload byte and order preserved");
      payload_offset += got;
    }
    frame_offset += got;
    calls++;
    if(meta->bytesleft == 0) {
      if(current_is_ping) {
        require(control_size == 3, "complete PING has exact payload length");
        send_pong(curl, fd, control, control_size);
        ping_frame++;
        control_size = 0;
      }
      else data_frame++;
      frame_offset = 0;
      current_is_ping = 0;
    }
    require(calls < 10000, "fragmented read terminates without spinning");
  }
  require(payload_offset == 65792, "all fragmented message bytes assembled");
  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  curl_easy_cleanup(curl);
  puts("direct receive: fragmented data, split PING payloads, offsets, and PONG replies passed");
}

int main(int argc, char **argv) {
  if(argc != 4) {
    fprintf(stderr, "usage: %s (large|fragmented|queued|autopong|stale|truncated) WSS_URL CA_BUNDLE\n", argv[0]);
    return 2;
  }
  if(curl_global_init(CURL_GLOBAL_DEFAULT)) return 2;
  if(!strcmp(argv[1], "large")) recv_large_and_boundary(argv[2], argv[3]);
  else if(!strcmp(argv[1], "fragmented")) recv_fragmented_controls(argv[2], argv[3]);
  else if(!strcmp(argv[1], "queued")) recv_queued_frames(argv[2], argv[3]);
  else if(!strcmp(argv[1], "autopong")) recv_split_ping_autopong(argv[2], argv[3]);
  else if(!strcmp(argv[1], "stale")) recv_stale_pending(argv[2], argv[3]);
  else if(!strcmp(argv[1], "truncated")) recv_truncated_frame(argv[2], argv[3]);
  else { fprintf(stderr, "unknown mode: %s\n", argv[1]); return 2; }
  curl_global_cleanup();
  return 0;
}







