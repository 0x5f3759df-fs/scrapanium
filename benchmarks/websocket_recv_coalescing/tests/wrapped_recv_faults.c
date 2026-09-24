/* Deterministic test-only transport faults around the frozen candidate archive.
 * The real Curl WebSocket decoder, queue, and TLS path remain linked unchanged. */
#define main direct_receive_unused_main
#include "direct_ws_recv.c"
#undef main

#include <stdbool.h>
#include <stdint.h>

struct Curl_easy;
extern CURLcode __real_Curl_easy_recv(struct Curl_easy *, void *, size_t, size_t *);
extern bool __real_Curl_conn_data_pending(struct Curl_easy *, int8_t);

enum fault_mode {
  FAULT_NONE,
  FAULT_PENDING_FALSE,
  FAULT_STALE_AGAIN,
  FAULT_EMPTY_EOF,
  FAULT_FATAL,
  FAULT_DRAIN_BOUND
};

static struct {
  enum fault_mode mode;
  struct Curl_easy *target;
  unsigned active;
  unsigned easy_recv_calls;
  unsigned real_data_reads;
  unsigned pending_calls;
  unsigned forced_pending;
  unsigned recv_calls_at_false_hint;
  unsigned injected_again;
  unsigned injected_eof;
  unsigned injected_fatal;
  unsigned injected;
  unsigned drain_successful_reads;
  unsigned drain_started;
  unsigned drain_extra_calls;
  unsigned drain_extra_reads;
  size_t drain_initial_bytes;
  unsigned drain_unexpected_result;
} hook;

static void arm_hook(enum fault_mode mode, CURL *curl) {
  memset(&hook, 0, sizeof(hook));
  hook.mode = mode;
  hook.target = (struct Curl_easy *)curl;
  hook.active = 1;
}

static enum fault_mode parse_mode(const char *name) {
  if(!strcmp(name, "pending-false")) return FAULT_PENDING_FALSE;
  if(!strcmp(name, "stale-again")) return FAULT_STALE_AGAIN;
  if(!strcmp(name, "empty-eof")) return FAULT_EMPTY_EOF;
  if(!strcmp(name, "fatal")) return FAULT_FATAL;
  if(!strcmp(name, "drain-bound")) return FAULT_DRAIN_BOUND;
  fprintf(stderr, "unknown fault mode: %s\n", name);
  exit(2);
}

static const char *mode_name(enum fault_mode mode) {
  switch(mode) {
    case FAULT_PENDING_FALSE: return "pending-false";
    case FAULT_STALE_AGAIN: return "stale-again";
    case FAULT_EMPTY_EOF: return "empty-eof";
    case FAULT_FATAL: return "fatal";
    case FAULT_DRAIN_BOUND: return "drain-bound";
    default: return "none";
  }
}

CURLcode __wrap_Curl_easy_recv(struct Curl_easy *data, void *buffer,
                               size_t buflen, size_t *nread) {
  if(!hook.active || data != hook.target)
    return __real_Curl_easy_recv(data, buffer, buflen, nread);
  ++hook.easy_recv_calls;

  if(hook.mode == FAULT_DRAIN_BOUND) {
    size_t cap = hook.drain_started ? 16 : 68;
    CURLcode code;
    if(hook.drain_started)
      ++hook.drain_extra_calls;
    if(cap > buflen)
      cap = buflen;
    code = __real_Curl_easy_recv(data, buffer, cap, nread);
    if(code == CURLE_OK && *nread) {
      ++hook.real_data_reads;
      ++hook.drain_successful_reads;
      if(hook.drain_successful_reads == 1)
        hook.drain_initial_bytes = *nread;
      else {
        ++hook.drain_extra_reads;
        if(*nread != 16)
          hook.drain_unexpected_result = 1;
      }
    }
    else if(code != CURLE_AGAIN || *nread)
      hook.drain_unexpected_result = 1;
    return code;
  }

  if(hook.forced_pending && !hook.injected &&
     hook.mode != FAULT_PENDING_FALSE) {
    hook.injected = 1;
    *nread = 0;
    if(hook.mode == FAULT_STALE_AGAIN) {
      ++hook.injected_again;
      return CURLE_AGAIN;
    }
    if(hook.mode == FAULT_EMPTY_EOF) {
      ++hook.injected_eof;
      return CURLE_OK;
    }
    if(hook.mode == FAULT_FATAL) {
      ++hook.injected_fatal;
      return CURLE_RECV_ERROR;
    }
  }
  CURLcode code = __real_Curl_easy_recv(data, buffer, buflen, nread);
  if(code == CURLE_OK && *nread)
    ++hook.real_data_reads;
  return code;
}

bool __wrap_Curl_conn_data_pending(struct Curl_easy *data, int8_t sockindex) {
  if(!hook.active || data != hook.target)
    return __real_Curl_conn_data_pending(data, sockindex);
  ++hook.pending_calls;

  if(hook.mode == FAULT_DRAIN_BOUND && hook.drain_successful_reads) {
    hook.forced_pending = 1;
    hook.drain_started = 1;
    return true;
  }
  if(hook.real_data_reads && !hook.forced_pending) {
    hook.forced_pending = 1;
    if(hook.mode == FAULT_PENDING_FALSE) {
      hook.recv_calls_at_false_hint = hook.easy_recv_calls;
      return false;
    }
    return true;
  }
  return __real_Curl_conn_data_pending(data, sockindex);
}

static void check_payload(const unsigned char *bytes, size_t offset, size_t size) {
  for(size_t i = 0; i < size; ++i)
    require(bytes[i] == (unsigned char)((offset + i) & 255),
            "injected and TLS payload bytes remain ordered and exact");
}

static void check_frame_meta(const ws_meta *meta, size_t offset, size_t got) {
  require(meta && (meta->flags & CURLWS_BINARY) &&
          meta->offset == (curl_off_t)offset &&
          meta->len == got && meta->bytesleft == (curl_off_t)(2048 - offset - got),
          "fault-path metadata matches the original incomplete binary frame");
}

static void require_hook_shape(void) {
  require(hook.pending_calls > 0 && hook.forced_pending == 1,
          "linker wrapper intercepted exactly one candidate pending decision");
  require(hook.easy_recv_calls > 0 && hook.real_data_reads > 0,
          "linker wrapper forwarded the real TLS receive before injection");
  switch(hook.mode) {
    case FAULT_PENDING_FALSE:
      require(hook.injected == 0 && hook.injected_again == 0 &&
              hook.injected_eof == 0 && hook.injected_fatal == 0,
              "false pending hint does not inject a receive result");
      require(hook.recv_calls_at_false_hint > 0,
              "false pending hint follows a successful real read");
      break;
    case FAULT_STALE_AGAIN:
      require(hook.injected_again == 1 && hook.injected_fatal == 0,
              "one stale pending hint produces exactly one CURLE_AGAIN");
      break;
    case FAULT_EMPTY_EOF:
      require(hook.injected == 1 && hook.injected_fatal == 0,
              "EOF fixture injects one successful zero-byte read");
      break;
    case FAULT_FATAL:
      require(hook.injected_fatal == 1,
              "terminal fixture injects exactly one fatal read result");
      break;
    case FAULT_DRAIN_BOUND:
      require(hook.drain_initial_bytes >= 4 &&
              hook.drain_extra_calls == 4 &&
              hook.drain_extra_reads == 4 &&
              !hook.drain_unexpected_result,
              "four extra reads all succeeded while input remained available");
      break;
    default:
      fail("hook mode remained armed", CURLE_FAILED_INIT);
  }
}

static void expect_again_while_gated(CURL *curl) {
  unsigned char out[1]; size_t got = 99; const ws_meta *meta = NULL;
  CURLcode code = curl_ws_recv(curl, out, sizeof out, &got, &meta);
  require(code == CURLE_AGAIN && got == 0 && meta == NULL,
          "gated next read returns fresh CURLE_AGAIN with no replayed bytes or metadata");
}

static size_t resume_after_marker(CURL *curl, curl_socket_t fd,
                                  size_t offset, int already_readable) {
  static const unsigned char marker[] = {'r','e','s','u','m','e'};
  send_all(curl, fd, marker, sizeof marker, CURLWS_PING);
  if(!already_readable)
    wait_ready(fd, POLLIN);
  unsigned char chunk[2048]; size_t got = 0; const ws_meta *meta = NULL;
  CURLcode code = curl_ws_recv(curl, chunk, sizeof chunk, &got, &meta);
  if(code) fail("curl_ws_recv first resumed data", code);
  check_frame_meta(meta, offset, got);
  check_payload(chunk, offset, got);
  offset += got;
  while(offset < 2048) {
    size_t cap = 2048 - offset;
    got = 0; meta = NULL;
    code = recv_one(curl, fd, chunk, cap, &got, &meta);
    if(code) fail("curl_ws_recv resumed tail", code);
    check_frame_meta(meta, offset, got);
    check_payload(chunk, offset, got);
    offset += got;
  }
  unsigned char pong[16]; got = 0; meta = NULL;
  code = recv_one(curl, fd, pong, sizeof pong, &got, &meta);
  if(code) fail("curl_ws_recv resume marker PONG", code);
  require(meta && (meta->flags & CURLWS_PONG) && got == sizeof marker &&
          !memcmp(pong, marker, sizeof marker),
          "resume marker receives its exact PONG before close");
  static const unsigned char close_payload[] = {0x03,0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  got = 0; meta = NULL;
  code = recv_one(curl, fd, chunk, sizeof chunk, &got, &meta);
  if(code) fail("curl_ws_recv close echo", code);
  require(meta && (meta->flags & CURLWS_CLOSE) && got == sizeof close_payload &&
          !memcmp(chunk, close_payload, sizeof close_payload),
          "fault path completes the WebSocket close handshake");
  return offset;
}

static void run_drain_bound(const char *url, const char *ca) {
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(url, ca, &curl, 0);
  unsigned char first[2048]; size_t got = 0; const ws_meta *meta = NULL;
  CURLcode code;

  arm_hook(FAULT_DRAIN_BOUND, curl);
  code = recv_one(curl, fd, first, sizeof first, &got, &meta);
  if(code) fail("curl_ws_recv bounded pending drains", code);
  require(hook.drain_initial_bytes >= 4,
          "initial wrapped read contains the complete WebSocket frame header");
  size_t initial_payload = hook.drain_initial_bytes - 4;
  require(got == initial_payload + 64,
          "initial payload plus four 16-byte drains is returned");
  check_frame_meta(meta, 0, got);
  check_payload(first, 0, got);
  require_hook_shape();
  require(hook.pending_calls == 4,
          "pending remains true through all four additional read decisions");
  unsigned calls_at_first_return = hook.easy_recv_calls;
  unsigned extra_calls_at_first_return = hook.drain_extra_calls;
  unsigned pending_at_first_return = hook.pending_calls;
  require(hook.drain_extra_reads == 4,
          "first API return follows four additional successful reads");
  require(hook.drain_successful_reads == 5,
          "only one initial read and four bounded extra reads succeeded");

  hook.active = 0;
  unsigned char tail[2048]; got = 0; meta = NULL;
  code = recv_one(curl, fd, tail, sizeof tail, &got, &meta);
  if(code) fail("curl_ws_recv after bounded pending drains", code);
  size_t first_payload = initial_payload + 64;
  require(got == 2048 - first_payload,
          "remaining frame payload is returned by the next API call");
  check_frame_meta(meta, first_payload, got);
  check_payload(tail, first_payload, got);

  static const unsigned char close_payload[] = {0x03, 0xe8};
  send_all(curl, fd, close_payload, sizeof close_payload, CURLWS_CLOSE);
  got = 0; meta = NULL;
  code = recv_one(curl, fd, tail, sizeof tail, &got, &meta);
  if(code) fail("curl_ws_recv bounded-drain close echo", code);
  require(meta && (meta->flags & CURLWS_CLOSE) &&
          got == sizeof close_payload && !memcmp(tail, close_payload, got),
          "bounded-drain fixture completes the WebSocket close handshake");

  curl_easy_cleanup(curl);
  printf("wrapped-drain: payload=%zu+%zu, initial_wire=%zu, extra_reads=%u, "
         "extra_calls=%u, pending_checks=%u, recv_calls_at_first_return=%u; "
         "exact frame and close passed\n",
         first_payload, (size_t)(2048 - first_payload), hook.drain_initial_bytes,
         hook.drain_extra_reads, extra_calls_at_first_return,
         pending_at_first_return, calls_at_first_return);
}

int main(int argc, char **argv) {
  if(argc != 4) {
    fprintf(stderr, "usage: %s (pending-false|stale-again|empty-eof|fatal|drain-bound) WSS_URL CA_BUNDLE\n", argv[0]);
    return 2;
  }
  enum fault_mode mode = parse_mode(argv[1]);
  if(mode == FAULT_DRAIN_BOUND) {
    if(curl_global_init(CURL_GLOBAL_DEFAULT)) return 2;
    run_drain_bound(argv[2], argv[3]);
    curl_global_cleanup();
    return 0;
  }

  if(curl_global_init(CURL_GLOBAL_DEFAULT)) return 2;
  CURL *curl = NULL;
  curl_socket_t fd = connect_ws(argv[2], argv[3], &curl, 0);
  arm_hook(mode, curl);

  unsigned char first[2048]; size_t got = 0; const ws_meta *meta = NULL;
  CURLcode code = recv_one(curl, fd, first, sizeof first, &got, &meta);
  if(code) fail("curl_ws_recv partial before deterministic fault", code);
  require(got == 64, "real TLS frame prefix contains exactly 64 bytes");
  check_frame_meta(meta, 0, got);
  check_payload(first, 0, got);
  require_hook_shape();

  size_t offset = got;
  require(mode != FAULT_PENDING_FALSE ||
          hook.easy_recv_calls == hook.recv_calls_at_false_hint,
          "false pending hint returns without an extra receive call");

  if(mode == FAULT_EMPTY_EOF || mode == FAULT_FATAL) {
    hook.active = 0;
    unsigned char ignored[1]; got = 99; meta = NULL;
    code = curl_ws_recv(curl, ignored, sizeof ignored, &got, &meta);
    CURLcode expected = mode == FAULT_EMPTY_EOF ? CURLE_GOT_NOTHING : CURLE_RECV_ERROR;
    require(code == expected && got == 0 && meta == NULL,
            "deferred terminal status appears only after accumulated bytes are returned");
    /* The deferred status is one-shot. With the peer still gated, the next
     * unhooked receive must reach the real transport and return CURLE_AGAIN. */
    expect_again_while_gated(curl);
  }
  else {
    hook.active = 0;
    expect_again_while_gated(curl);
  }

  if(mode == FAULT_PENDING_FALSE || mode == FAULT_STALE_AGAIN)
    offset = 64;
  offset = resume_after_marker(curl, fd, offset, 0);
  curl_easy_cleanup(curl);
  printf("wrapped-fault: mode=%s, pending=%u/%u, recv-wrap=%u, real-data=%u, "
         "again=%u eof=%u fatal=%u; exact frame and close passed\n",
         mode_name(mode), hook.forced_pending, hook.pending_calls,
         hook.easy_recv_calls, hook.real_data_reads, hook.injected_again,
         hook.injected_eof, hook.injected_fatal);
  curl_global_cleanup();
  return 0;
}
