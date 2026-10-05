#define _GNU_SOURCE
#include <curl/curl.h>
#include <curl/easy.h>
#include <curl/websockets.h>
#include <dlfcn.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static unsigned long recv_calls = 0, recv_again = 0;

static int wait_ready(CURL *curl, short events) {
  curl_socket_t fd = CURL_SOCKET_BAD;
  if (curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &fd) != CURLE_OK ||
      fd == CURL_SOCKET_BAD) {
    fprintf(stderr, "CURLINFO_ACTIVESOCKET failed\n");
    return 0;
  }
  struct pollfd pfd = { (int)fd, events, 0 };
  for (int i = 0; i < 40; i++) {
    int rc = poll(&pfd, 1, 250);
    if (rc > 0) {
      if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
        fprintf(stderr, "socket poll failure revents=%x\n", pfd.revents);
        return 0;
      }
      if (pfd.revents & events) return 1;
    }
    else if (rc < 0) {
      perror("poll");
      return 0;
    }
  }
  fprintf(stderr, "socket readiness timeout events=%x\n", events);
  return 0;
}

static int recv_exact(CURL *curl, unsigned char *out, size_t length,
                      const char *label) {
  size_t offset = 0;
  while (offset < length) {
    size_t got = 0;
    CURLcode code = curl_easy_recv(curl, out + offset, length - offset, &got);
    recv_calls++;
    if (code == CURLE_AGAIN) {
      recv_again++;
      if (!wait_ready(curl, POLLIN)) return 0;
      continue;
    }
    if (code != CURLE_OK || got == 0 || got > length - offset) {
      fprintf(stderr, "%s recv failed code=%d got=%zu offset=%zu/%zu\n",
              label, (int)code, got, offset, length);
      return 0;
    }
    offset += got;
  }
  return 1;
}

static int send_frame(CURL *curl, const unsigned char *data, size_t length,
                      unsigned int flags, const char *label) {
  size_t offset = 0;
  while (offset < length) {
    size_t sent = 0;
    CURLcode code = curl_ws_send(curl, data + offset, length - offset,
                                 &sent, 0, flags);
    if (sent > length - offset) {
      fprintf(stderr, "%s sent count overflow\n", label);
      return 0;
    }
    offset += sent;
    if (code == CURLE_AGAIN) {
      if (!wait_ready(curl, POLLOUT)) return 0;
      continue;
    }
    if (code != CURLE_OK || sent == 0) {
      fprintf(stderr, "%s send failed code=%d sent=%zu offset=%zu/%zu\n",
              label, (int)code, sent, offset, length);
      return 0;
    }
  }
  return 1;
}

static int check_bytes(const unsigned char *actual,
                       const unsigned char *expected, size_t length,
                       const char *label) {
  if (memcmp(actual, expected, length) == 0) return 1;
  size_t at = 0;
  while (at < length && actual[at] == expected[at]) at++;
  fprintf(stderr, "%s mismatch at %zu/%zu: got=%02x expected=%02x\n",
          label, at, length, at < length ? actual[at] : 0,
          at < length ? expected[at] : 0);
  return 0;
}

static int fail(CURL *curl, struct curl_slist *headers, const char *message) {
  if (message) fprintf(stderr, "%s\n", message);
  if (headers) curl_slist_free_all(headers);
  if (curl) curl_easy_cleanup(curl);
  curl_global_cleanup();
  return 1;
}

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s wss-url ca-file\n", argv[0]);
    return 2;
  }
  CURL *curl = NULL;
  struct curl_slist *headers = NULL;
  CURLcode code = curl_global_init(CURL_GLOBAL_DEFAULT);
  if (code != CURLE_OK) {
    fprintf(stderr, "curl_global_init: %s\n", curl_easy_strerror(code));
    return 1;
  }
  curl = curl_easy_init();
  if (!curl) return fail(curl, headers, "curl_easy_init failed");

  code = curl_easy_impersonate(curl, "chrome146", 0);
  if (code != CURLE_OK) {
    fprintf(stderr, "curl_easy_impersonate(chrome146): %s\n",
            curl_easy_strerror(code));
    return fail(curl, headers, NULL);
  }

  const char *key = "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==";
  headers = curl_slist_append(headers, key);
  if (!headers) return fail(curl, headers, "curl_slist_append failed");

#define SETOPT(opt, value) do { \
    code = curl_easy_setopt(curl, (opt), (value)); \
    if (code != CURLE_OK) { \
      fprintf(stderr, "%s: %s\n", #opt, curl_easy_strerror(code)); \
      return fail(curl, headers, NULL); \
    } \
  } while (0)
  SETOPT(CURLOPT_URL, argv[1]);
  SETOPT(CURLOPT_CONNECT_ONLY, 2L);
  SETOPT(CURLOPT_HTTP_VERSION, (long)CURL_HTTP_VERSION_1_1);
  SETOPT(CURLOPT_HTTPHEADER, headers);
  SETOPT(CURLOPT_SSL_VERIFYPEER, 1L);
  SETOPT(CURLOPT_SSL_VERIFYHOST, 2L);
  SETOPT(CURLOPT_CAINFO, argv[2]);
  SETOPT(CURLOPT_PROXY, "");
  SETOPT(CURLOPT_NOPROXY, "*");
  SETOPT(CURLOPT_WS_OPTIONS, (long)CURLWS_NOAUTOPONG);
  SETOPT(CURLOPT_TIMEOUT_MS, 15000L);
#undef SETOPT

  code = curl_easy_perform(curl);
  if (code != CURLE_OK) {
    fprintf(stderr, "curl_easy_perform: %s (%d)\n",
            curl_easy_strerror(code), (int)code);
    return fail(curl, headers, NULL);
  }
  long status = 0;
  if (curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status) != CURLE_OK ||
      status != 101) {
    fprintf(stderr, "unexpected handshake status %ld\n", status);
    return fail(curl, headers, NULL);
  }

  Dl_info info;
  memset(&info, 0, sizeof(info));
  if (!dladdr((void *)(uintptr_t)curl_easy_perform, &info) ||
      !info.dli_fname) {
    return fail(curl, headers, "dladdr could not identify libcurl");
  }
  printf("response_code=101\n");
  printf("curl_version=%s\n", curl_version());
  printf("backend_path=%s\n", info.dli_fname);
  printf("profile=chrome146\n");
  printf("ws_options=NOAUTOPONG,RAW_MODE_OFF\n");

  unsigned char b = 0;
  if (!recv_exact(curl, &b, 1, "coalesced-first-octet") || b != 0x02) {
    fprintf(stderr, "first coalesced WebSocket octet mismatch: %02x\n", b);
    return fail(curl, headers, NULL);
  }

  unsigned char gated_probe = 0;
  size_t gated_got = SIZE_MAX;
  CURLcode gated_code = curl_easy_recv(curl, &gated_probe, 1, &gated_got);
  recv_calls++;
  if (gated_code == CURLE_AGAIN) recv_again++;
  if (gated_code != CURLE_AGAIN || gated_got != 0) {
    fprintf(stderr, "gated empty read expected CURLE_AGAIN, code=%d got=%zu\n",
            (int)gated_code, gated_got);
    return fail(curl, headers, NULL);
  }

  static const unsigned char ready[] = "ready";
  if (!send_frame(curl, ready, sizeof(ready) - 1, CURLWS_BINARY, "ready")) {
    return fail(curl, headers, NULL);
  }

  unsigned char second = 0, first_payload[1], rest_payload[2];
  if (!recv_exact(curl, &second, 1, "data-header-tail") || second != 0x03 ||
      !recv_exact(curl, first_payload, sizeof(first_payload), "data-payload-1") ||
      !recv_exact(curl, rest_payload, sizeof(rest_payload), "data-payload-2") ||
      first_payload[0] != 'a' || rest_payload[0] != 'b' ||
      rest_payload[1] != 'c') {
    return fail(curl, headers, "fragmented data-frame byte check failed");
  }

  unsigned char ping_header[2], ping_payload[4];
  if (!recv_exact(curl, ping_header, 1, "ping-header-1") ||
      !recv_exact(curl, ping_header + 1, 1, "ping-header-2") ||
      !recv_exact(curl, ping_payload, 2, "ping-payload-1") ||
      !recv_exact(curl, ping_payload + 2, 2, "ping-payload-2") ||
      ping_header[0] != 0x89 || ping_header[1] != 0x04 ||
      memcmp(ping_payload, "ping", 4) != 0) {
    return fail(curl, headers, "interleaved PING raw-byte check failed");
  }
  if (!send_frame(curl, ping_payload, sizeof(ping_payload),
                  CURLWS_PONG, "pong")) {
    return fail(curl, headers, NULL);
  }

  unsigned char continuation[5];
  if (!recv_exact(curl, continuation, sizeof(continuation), "continuation") ||
      continuation[0] != 0x80 || continuation[1] != 0x03 ||
      memcmp(continuation + 2, "def", 3) != 0) {
    return fail(curl, headers, "continuation raw-byte check failed");
  }

  unsigned char large_header[10];
  const unsigned char expected_header[10] =
      { 0x82, 0x7f, 0, 0, 0, 0, 0, 1, 0, 0 };
  if (!recv_exact(curl, large_header, sizeof(large_header), "large-header") ||
      !check_bytes(large_header, expected_header, sizeof(expected_header),
                   "large-header")) {
    return fail(curl, headers, NULL);
  }
  const size_t large_length = 65536;
  unsigned char *large = (unsigned char *)malloc(large_length);
  if (!large) return fail(curl, headers, "large allocation failed");
  int ok = recv_exact(curl, large, large_length, "large-payload");
  if (ok) {
    for (size_t i = 0; i < large_length; i++) {
      if (large[i] != (unsigned char)(i & 0xff)) {
        fprintf(stderr, "large payload mismatch at %zu\n", i);
        ok = 0;
        break;
      }
    }
  }
  free(large);
  if (!ok) return fail(curl, headers, NULL);

  static const unsigned char client_data[] = "client-data";
  if (!send_frame(curl, client_data, sizeof(client_data) - 1,
                  CURLWS_BINARY, "client-data")) {
    return fail(curl, headers, NULL);
  }
  const unsigned char close_payload[] = { 0x03, 0xe8, 'd', 'o', 'n', 'e' };
  if (!send_frame(curl, close_payload, sizeof(close_payload),
                  CURLWS_CLOSE, "close")) {
    return fail(curl, headers, NULL);
  }

  unsigned char close_frame[8];
  const unsigned char expected_close[8] =
      { 0x88, 0x06, 0x03, 0xe8, 'd', 'o', 'n', 'e' };
  if (!recv_exact(curl, close_frame, sizeof(close_frame), "close-echo") ||
      !check_bytes(close_frame, expected_close, sizeof(expected_close),
                   "close-echo")) {
    return fail(curl, headers, NULL);
  }

  long verify_result = -1;
  code = curl_easy_getinfo(curl, CURLINFO_SSL_VERIFYRESULT, &verify_result);
  if (code != CURLE_OK || verify_result != 0) {
    fprintf(stderr, "TLS verification result failed code=%d result=%ld\n",
            (int)code, verify_result);
    return fail(curl, headers, NULL);
  }
  printf("ssl_verify_result=%ld\n", verify_result);
  printf("raw_frames=1_fragmented_binary+interleaved_ping+continuation+64KiB_binary+close\n");
  printf("client_frames=ready_binary+pong+client_binary+close\n");
  printf("all_exact_raw_bytes_verified=yes\n");
  printf("curl_easy_recv_calls=%lu\n", recv_calls);
  printf("curl_easy_recv_again=%lu\n", recv_again);
  printf("gated_empty_recv=CURLE_AGAIN\n");

  curl_easy_cleanup(curl);
  curl_slist_free_all(headers);
  curl_global_cleanup();
  return 0;
}
