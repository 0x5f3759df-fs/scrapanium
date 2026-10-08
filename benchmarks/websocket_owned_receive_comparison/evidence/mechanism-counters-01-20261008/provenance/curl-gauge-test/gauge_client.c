#define _GNU_SOURCE
#include <curl/curl.h>
#include <curl/websockets.h>
#include <dlfcn.h>
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_COUNTERS 128
#define MAX_HELD 128
#define MAX_FRAME 4096

struct snapshot {
  size_t count;
  uint64_t values[MAX_COUNTERS];
};

struct held_payload {
  struct curl_ws_payload *payload;
  unsigned char *copy;
  size_t size;
  size_t frame;
};

static void fail(const char *message) {
  fprintf(stderr, "FAIL: %s\n", message);
  exit(2);
}

#define REQUIRE(cond, message) do { if(!(cond)) fail(message); } while(0)

static uint64_t metric(const struct snapshot *snapshot, const char *name) {
  for(size_t i = 0; i < snapshot->count; i++) {
    const char *candidate = curl_ws_diag_counter_name(i);
    if(candidate && !strcmp(candidate, name))
      return snapshot->values[i];
  }
  fprintf(stderr, "FAIL: missing diagnostic counter %s\n", name);
  exit(2);
}

static void take_snapshot(struct snapshot *snapshot) {
  snapshot->count = curl_ws_diag_snapshot_count();
  REQUIRE(snapshot->count <= MAX_COUNTERS, "counter snapshot exceeds test capacity");
  memset(snapshot->values, 0, sizeof(snapshot->values));
  curl_ws_diag_snapshot(snapshot->values, snapshot->count);
}

static uint64_t delta(const struct snapshot *before,
                      const struct snapshot *after, const char *name) {
  uint64_t a = metric(before, name), b = metric(after, name);
  REQUIRE(b >= a, "counter unexpectedly decreased");
  return b - a;
}

static long long monotonic_ms(void) {
  struct timespec ts;
  if(clock_gettime(CLOCK_MONOTONIC, &ts))
    fail("clock_gettime failed");
  return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void wait_readable(CURL *curl) {
  curl_socket_t socket_fd = CURL_SOCKET_BAD;
  CURLcode code = curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &socket_fd);
  REQUIRE(code == CURLE_OK && socket_fd != CURL_SOCKET_BAD,
          "could not get active WebSocket socket");
  struct pollfd pfd = {.fd = (int)socket_fd, .events = POLLIN};
  int ready;
  do {
    ready = poll(&pfd, 1, 250);
  } while(ready < 0 && errno == EINTR);
  REQUIRE(ready >= 0, "poll failed while waiting for WebSocket data");
}

static void pattern(unsigned char *out, size_t size, unsigned mul, unsigned add) {
  for(size_t i = 0; i < size; i++)
    out[i] = (unsigned char)(i * mul + add);
}

static void receive_frame(CURL *curl, const unsigned char *expected,
                          size_t expected_size, size_t frame_index,
                          struct held_payload held[MAX_HELD],
                          size_t *held_count) {
  unsigned char assembled[MAX_FRAME];
  size_t assembled_size = 0;
  int first = 1;
  long long deadline = monotonic_ms() + 15000;

  REQUIRE(expected_size <= sizeof(assembled), "test frame too large");
  while(1) {
    REQUIRE(monotonic_ms() < deadline, "timed out receiving WebSocket frame");

    size_t reservation = curl_ws_recv_owned_reservation(curl);
    REQUIRE(reservation > 0, "owned receive reservation is unavailable");

    struct curl_ws_payload *payload = NULL;
    const struct curl_ws_frame *meta = NULL;
    CURLcode code = curl_ws_recv_owned(curl, reservation, &payload, &meta);
    if(code == CURLE_AGAIN) {
      REQUIRE(payload == NULL && meta == NULL,
              "AGAIN returned a payload or frame metadata");
      wait_readable(curl);
      continue;
    }
    if(code != CURLE_OK) {
      fprintf(stderr, "FAIL: curl_ws_recv_owned: %s (%d)\n",
              curl_easy_strerror(code), (int)code);
      exit(2);
    }

    REQUIRE(payload != NULL && meta != NULL,
            "successful owned receive omitted payload or metadata");
    REQUIRE(*held_count < MAX_HELD, "too many owned payload pieces");
    size_t payload_size = curl_ws_payload_size(payload);
    const unsigned char *payload_data = curl_ws_payload_data(payload);
    REQUIRE(payload_data != NULL && payload_size > 0,
            "successful receive has empty/null payload");
    REQUIRE(meta->len == payload_size, "frame length differs from owned payload size");
    REQUIRE(meta->offset >= 0 && meta->bytesleft >= 0,
            "negative frame offset/remaining length");
    REQUIRE((size_t)meta->offset == assembled_size,
            "frame offset does not continue the accumulated payload");
    REQUIRE(payload_size <= expected_size - assembled_size,
            "received more bytes than expected frame");
    REQUIRE((size_t)meta->offset + payload_size + (size_t)meta->bytesleft
              == expected_size,
            "frame metadata does not describe expected total size");
    if(first) {
      REQUIRE(meta->flags & CURLWS_BINARY, "first frame is not binary");
      first = 0;
    }

    memcpy(assembled + assembled_size, payload_data, payload_size);
    struct held_payload *item = &held[(*held_count)++];
    item->payload = payload;
    item->copy = malloc(payload_size);
    REQUIRE(item->copy != NULL, "could not allocate test copy");
    memcpy(item->copy, payload_data, payload_size);
    item->size = payload_size;
    item->frame = frame_index;
    assembled_size += payload_size;

    if(meta->bytesleft == 0)
      break;
  }

  REQUIRE(assembled_size == expected_size,
          "received frame did not reach its declared size");
  REQUIRE(memcmp(assembled, expected, expected_size) == 0,
          "assembled binary frame differs from expected bytes");
}

static void verify_held_payloads(const struct held_payload *held,
                                 size_t held_count) {
  for(size_t i = 0; i < held_count; i++) {
    const unsigned char *data = curl_ws_payload_data(held[i].payload);
    REQUIRE(data != NULL, "retained payload became null");
    REQUIRE(curl_ws_payload_size(held[i].payload) == held[i].size,
            "retained payload size changed");
    REQUIRE(memcmp(data, held[i].copy, held[i].size) == 0,
            "retained payload bytes changed");
  }
}

static void check_live(const struct snapshot *snapshot,
                       uint64_t pins, uint64_t payloads) {
  REQUIRE(metric(snapshot, "pins_live") == pins, "pins_live gauge mismatch");
  REQUIRE(metric(snapshot, "payloads_live") == payloads,
          "payloads_live gauge mismatch");
}

static void print_gauges(const struct snapshot *snapshot) {
  printf("{\"chunks_live\":%llu,\"chunks_live_request_bytes\":%llu,"
         "\"pins_live\":%llu,\"payloads_live\":%llu}",
         (unsigned long long)metric(snapshot, "chunks_live"),
         (unsigned long long)metric(snapshot, "chunks_live_request_bytes"),
         (unsigned long long)metric(snapshot, "pins_live"),
         (unsigned long long)metric(snapshot, "payloads_live"));
}

int main(int argc, char **argv) {
  REQUIRE(argc == 4, "usage: gauge_client WSS_URL CA_FILE EXPECTED_DSO");
  void *symbol = dlsym(RTLD_DEFAULT, "curl_ws_recv_owned");
  Dl_info info;
  REQUIRE(symbol != NULL && dladdr(symbol, &info) != 0 && info.dli_fname,
          "could not resolve loaded curl_ws_recv_owned DSO");
  char *loaded_path = realpath(info.dli_fname, NULL);
  char *expected_path = realpath(argv[3], NULL);
  REQUIRE(loaded_path && expected_path, "could not canonicalize DSO paths");
  if(strcmp(loaded_path, expected_path)) {
    fprintf(stderr, "FAIL: loaded DSO %s does not equal expected %s\n",
            loaded_path, expected_path);
    return 2;
  }

  REQUIRE(curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK,
          "curl_global_init failed");
  struct snapshot base, pre_receive, after_frames, after_cleanup, after_first_free, final;
  take_snapshot(&base);
  REQUIRE(metric(&base, "pins_live") == 0 &&
          metric(&base, "payloads_live") == 0 &&
          metric(&base, "chunks_live") == 0,
          "diagnostic live gauges were not empty before connection");

  CURL *curl = curl_easy_init();
  REQUIRE(curl != NULL, "curl_easy_init failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_URL, argv[1]) == CURLE_OK,
          "setting URL failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L) == CURLE_OK,
          "setting WebSocket connect-only mode failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_CAINFO, argv[2]) == CURLE_OK,
          "setting CA file failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_PROXY, "") == CURLE_OK,
          "disabling proxy failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_NOPROXY, "*") == CURLE_OK,
          "disabling proxy bypass failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 5000L) == CURLE_OK,
          "setting connect timeout failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L) == CURLE_OK,
          "setting operation timeout failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_HTTP_VERSION,
                           (long)CURL_HTTP_VERSION_1_1) == CURLE_OK,
          "setting HTTP/1.1 failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L) == CURLE_OK,
          "enabling peer verification failed");
  REQUIRE(curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L) == CURLE_OK,
          "enabling host verification failed");

  CURLcode code = curl_easy_perform(curl);
  if(code != CURLE_OK) {
    fprintf(stderr, "FAIL: WebSocket handshake: %s (%d)\n",
            curl_easy_strerror(code), (int)code);
    return 2;
  }
  long response = 0;
  REQUIRE(curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response) == CURLE_OK &&
          response == 101, "WebSocket handshake did not return 101");

  take_snapshot(&pre_receive);
  REQUIRE(metric(&pre_receive, "pins_live") == 0 &&
          metric(&pre_receive, "payloads_live") == 0,
          "unexpected live pins/payloads before receive");

  unsigned char frame0[53], frame1[79];
  pattern(frame0, sizeof(frame0), 17, 3);
  pattern(frame1, sizeof(frame1), 29, 11);
  struct held_payload held[MAX_HELD] = {{0}};
  size_t held_count = 0;
  receive_frame(curl, frame0, sizeof(frame0), 0, held, &held_count);
  receive_frame(curl, frame1, sizeof(frame1), 1, held, &held_count);
  take_snapshot(&after_frames);

  uint64_t pins_base = metric(&base, "pins_live");
  uint64_t payloads_base = metric(&base, "payloads_live");
  REQUIRE(delta(&pre_receive, &after_frames, "pin_alloc_successes") == held_count,
          "pin allocation successes do not match held payload pieces");
  REQUIRE(delta(&pre_receive, &after_frames, "payload_alloc_successes") == held_count,
          "payload allocation successes do not match held payload pieces");
  check_live(&after_frames, pins_base + held_count, payloads_base + held_count);
  REQUIRE(metric(&after_frames, "chunks_live") > metric(&base, "chunks_live"),
          "owned receives did not retain any tagged receive chunks");

  curl_easy_cleanup(curl);
  curl = NULL;
  take_snapshot(&after_cleanup);
  check_live(&after_cleanup, pins_base + held_count, payloads_base + held_count);
  REQUIRE(metric(&after_cleanup, "chunks_live") > metric(&base, "chunks_live"),
          "pinned receive chunks did not survive easy-handle cleanup");
  verify_held_payloads(held, held_count);

  uint64_t queue_refs_dropped =
    delta(&base, &after_cleanup, "shared_detach_prune") +
    delta(&base, &after_cleanup, "shared_detach_reset") +
    delta(&base, &after_cleanup, "unshared_drop") +
    delta(&base, &after_cleanup, "queue_free_chunk_refs");
  REQUIRE(queue_refs_dropped > 0,
          "no tagged receive-queue references were released");

  REQUIRE(held_count >= 2, "expected multiple owned payload pieces");
  curl_ws_payload_free(held[0].payload);
  held[0].payload = NULL;
  take_snapshot(&after_first_free);
  check_live(&after_first_free, pins_base + held_count - 1,
             payloads_base + held_count - 1);
  verify_held_payloads(held + 1, held_count - 1);

  for(size_t i = 1; i < held_count; i++) {
    curl_ws_payload_free(held[i].payload);
    held[i].payload = NULL;
  }
  take_snapshot(&final);
  check_live(&final, pins_base, payloads_base);
  REQUIRE(metric(&final, "chunks_live") == metric(&base, "chunks_live"),
          "tagged receive chunks remain live after all payload releases");
  REQUIRE(delta(&base, &final, "payload_frees") == held_count,
          "payload free count does not balance allocations");
  REQUIRE(delta(&base, &final, "pin_releases") == held_count,
          "pin release count does not balance allocations");
  REQUIRE(delta(&base, &final, "chunk_physical_frees") ==
            delta(&base, &final, "chunk_alloc_successes"),
          "physical chunk frees do not balance tagged chunk allocations");
  REQUIRE(delta(&base, &final, "chunk_physical_free_request_bytes") ==
            delta(&base, &final, "chunk_alloc_success_bytes"),
          "physical chunk free bytes do not balance allocation bytes");
  uint64_t final_queue_refs =
    delta(&base, &final, "shared_detach_prune") +
    delta(&base, &final, "shared_detach_reset") +
    delta(&base, &final, "unshared_drop") +
    delta(&base, &final, "queue_free_chunk_refs");
  REQUIRE(final_queue_refs == delta(&base, &final, "chunk_alloc_successes"),
          "queue-reference releases do not balance tagged chunk allocations");
  REQUIRE(delta(&base, &final, "pin_acquire_failures") == 0 &&
          delta(&base, &final, "pin_alloc_failures") == 0 &&
          delta(&base, &final, "payload_alloc_failures") == 0,
          "unexpected pin/payload allocation failure");
  curl_global_cleanup();
  for(size_t i = 0; i < held_count; i++)
    free(held[i].copy);
  printf("{\"status\":\"pass\",\"mapped_dso\":\"%s\",\"held_payload_pieces\":%zu,"
         "\"queue_reference_drops\":%llu,\"tagged_chunk_allocations\":%llu,"
         "\"payload_pin_gauges_returned_to_baseline\":true,"
         "\"gauges\":{\"before_connection\":",
         loaded_path, held_count,
         (unsigned long long)final_queue_refs,
         (unsigned long long)delta(&base, &final, "chunk_alloc_successes"));
  print_gauges(&base);
  printf(",\"after_easy_cleanup\":");
  print_gauges(&after_cleanup);
  printf(",\"after_all_releases\":");
  print_gauges(&final);
  printf("},\"performance_claims\":false}\n");
  free(loaded_path);
  free(expected_path);
  return 0;
}
