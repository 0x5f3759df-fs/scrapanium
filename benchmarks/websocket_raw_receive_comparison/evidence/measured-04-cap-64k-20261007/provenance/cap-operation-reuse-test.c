/* Drive the real operation lifecycle with deterministic raw nonblocking input.
 * Network tests separately exercise framing, TLS, and client masking. */
#define _POSIX_C_SOURCE 200809L
#include <stdlib.h>
#include <assert.h>

#define TEST_WIRE_CAPACITY 140000u
static size_t calloc_calls;
static void *counted_calloc(size_t count, size_t size) {
  calloc_calls++; return calloc(count, size);
}
#define calloc counted_calloc
#define curl_easy_recv controlled_easy_recv
#define curl_ws_send controlled_ws_send
#include "../native/scrapanium.c"
#undef calloc
#undef curl_easy_recv
#undef curl_ws_send

typedef struct {
  unsigned char wire[TEST_WIRE_CAPACITY];
  size_t wire_size, wire_offset, recv_chunk_limit, recv_calls, pause_recv_call;
  size_t terminal_call, terminal_bytes, direct_recv_calls, direct_short_reads;
  size_t max_direct_capacity;
  void *read_ahead_buffer;
  CURLcode terminal_code;
  const unsigned char *send_data;
  size_t send_size, sent, send_calls;
  unsigned send_flags;
  int pause_send;
} Transport;

static void append_bytes(Transport *t, const unsigned char *data, size_t size) {
  assert(size <= sizeof t->wire - t->wire_size);
  if (size) memcpy(t->wire + t->wire_size, data, size);
  t->wire_size += size;
}

static void append_frame(Transport *t, unsigned opcode, const unsigned char *data,
                         size_t size, int final) {
  unsigned char header[10];
  size_t n = 0;
  header[n++] = (unsigned char)((final ? 0x80 : 0) | opcode);
  if (size < 126) header[n++] = (unsigned char)size;
  else if (size <= 65535) {
    header[n++] = 126;
    header[n++] = (unsigned char)(size >> 8);
    header[n++] = (unsigned char)size;
  } else {
    header[n++] = 127;
    for (unsigned i = 0; i < 8; i++)
      header[n++] = (unsigned char)((uint64_t)size >> (56 - 8 * i));
  }
  append_bytes(t, header, n);
  append_bytes(t, data, size);
}

CURLcode controlled_easy_recv(CURL *easy, void *buffer, size_t capacity,
                              size_t *received) {
  Transport *t = (Transport *)easy;
  *received = 0;
  size_t call = ++t->recv_calls;
  int direct = 0;
  if (!t->read_ahead_buffer) t->read_ahead_buffer = buffer;
  else if (buffer != t->read_ahead_buffer) {
    direct = 1;
    t->direct_recv_calls++;
    if (capacity > t->max_direct_capacity) t->max_direct_capacity = capacity;
  }
  if (call == t->pause_recv_call) return CURLE_AGAIN;

  if (call == t->terminal_call) {
    size_t count = t->terminal_bytes;
    if (count > capacity) count = capacity;
    if (count > t->wire_size - t->wire_offset) count = t->wire_size - t->wire_offset;
    if (count) memcpy(buffer, t->wire + t->wire_offset, count);
    t->wire_offset += count;
    *received = count;
    return t->terminal_code;
  }

  if (t->wire_offset == t->wire_size) return CURLE_AGAIN;
  size_t count = t->wire_size - t->wire_offset;
  if (count > capacity) count = capacity;
  if (t->recv_chunk_limit && count > t->recv_chunk_limit)
    count = t->recv_chunk_limit;
  if (direct && count < capacity) t->direct_short_reads++;
  memcpy(buffer, t->wire + t->wire_offset, count);
  t->wire_offset += count;
  *received = count;
  return CURLE_OK;
}

CURLcode controlled_ws_send(CURL *easy, const void *buffer, size_t size,
                            size_t *sent, curl_off_t fragment_size,
                            unsigned int flags) {
  (void)fragment_size;
  Transport *t = (Transport *)easy;
  assert(flags == t->send_flags && size == t->send_size - t->sent);
  assert(!memcmp(buffer, t->send_data + t->sent, size));
  t->send_calls++;
  *sent = t->pause_send && size ? 1 : size;
  t->sent += *sent;
  if (t->pause_send) { t->pause_send = 0; return CURLE_AGAIN; }
  return CURLE_OK;
}

typedef struct { Transport transport; sp_slot slot; sp_session session; sp_ws socket; } Fixture;

static void setup(Fixture *f) {
  memset(f, 0, sizeof *f);
  f->slot.easy = (CURL *)&f->transport;
  f->session.slots = &f->slot; f->session.config.max_body_bytes = 1048576;
  f->socket.session = &f->session;
  assert(!pthread_mutex_init(&f->socket.gate, NULL));
}
static void teardown(Fixture *f) {
  assert(!f->socket.operation.message.data);
  free(f->socket.input);
  assert(!pthread_mutex_destroy(&f->socket.gate));
}

static sp_ws_io *start(Fixture *f, unsigned operation, unsigned kind,
                       const unsigned char *data, size_t size, sp_cancel *cancel) {
  size_t before = calloc_calls;
  int error = -1;
  sp_ws_io *io = sp_ws_io_start(&f->socket, operation, kind, data, size, 5000,
                                cancel, &error);
  assert(io && error == SP_OK && calloc_calls == before);
  assert(io == &f->socket.operation);
  assert(!io->offset && !io->message.size && !io->message.cap &&
         !io->message.data && !io->message_kind);
  if (operation != SP_WS_CLOSE) assert(!io->control_size && !io->after_send);
  return io;
}
static void dispose(sp_ws_io *io) {
  sp_ws *owner = io->socket;
  sp_ws_io_free(io);
  assert(!owner->operation.message.data && !owner->operation.cancel &&
         !owner->operation.out && !owner->operation.socket);
  assert(!pthread_mutex_trylock(&owner->gate));
  pthread_mutex_unlock(&owner->gate);
}
static int receive_step(sp_ws_io *io, unsigned *kind, unsigned char **data,
                       size_t *size) {
  return sp_ws_io_step(io, kind, data, size);
}
static int drain_receive(sp_ws_io *io, unsigned *kind, unsigned char **data,
                         size_t *size) {
  for (unsigned steps = 0; steps < 10000; steps++) {
    int result = receive_step(io, kind, data, size);
    if (result != SP_WS_PENDING) return result;
  }
  assert(!"raw receive did not finish within the deterministic test bound");
  return SP_TRANSPORT;
}
static void expect_message(Fixture *f, unsigned expected_kind,
                           const unsigned char *expected, size_t expected_size) {
  sp_ws_io *io = start(f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(drain_receive(io, &kind, &data, &size) == SP_OK);
  assert(kind == expected_kind && size == expected_size);
  assert(!size || !memcmp(data, expected, size));
  free(data); dispose(io);
}
static void busy_preserves(sp_ws_io *io) {
  sp_ws_io snapshot; memcpy(&snapshot, io, sizeof snapshot);
  int error = -1;
  assert(!sp_ws_io_start(io->socket, SP_WS_RECEIVE, 0, NULL, 0, 5000,
                         NULL, &error));
  assert(error == SP_BUSY && !memcmp(io, &snapshot, sizeof snapshot));
}

static void successful_reuse(void) {
  Fixture first, second; setup(&first); setup(&second);
  const unsigned char sending[] = "send\0bytes";
  static const unsigned char left[] = "left", right[] = "right", next[] = "next";
  static const unsigned char ping[] = {'p', 0, 255};
  static const unsigned char closing[] = {3, 232, 'o', 'k'};
  append_frame(&first.transport, 2, left, sizeof left - 1, 0);
  append_frame(&first.transport, 9, ping, sizeof ping, 1);
  append_frame(&first.transport, 0, right, sizeof right - 1, 1);
  append_frame(&first.transport, 2, next, sizeof next - 1, 1);
  append_frame(&first.transport, 8, closing, sizeof closing, 1);
  first.transport.recv_chunk_limit = 6;
  first.transport.pause_recv_call = 2;
  first.transport.send_data = sending;
  first.transport.send_size = sizeof sending - 1;
  first.transport.send_flags = CURLWS_BINARY;
  first.transport.pause_send = 1;

  sp_cancel *cancel = sp_cancel_new(); assert(cancel);
  sp_ws_io *original = start(&first, SP_WS_SEND, 2, sending,
                             sizeof sending - 1, cancel);
  assert(atomic_load(&cancel->refs) == 2);
  assert(sp_ws_io_step(original, NULL, NULL, NULL) == SP_WS_PENDING);
  assert(original->offset == 1 && original->events == POLLOUT);
  busy_preserves(original);
  assert(sp_ws_io_step(original, NULL, NULL, NULL) == SP_OK);
  assert(first.transport.sent == sizeof sending - 1);
  dispose(original); assert(atomic_load(&cancel->refs) == 1);
  first.transport.sent = 0; first.transport.send_calls = 0;
  first.transport.send_data = ping; first.transport.send_size = sizeof ping;
  first.transport.send_flags = CURLWS_PONG; first.transport.pause_send = 1;

  sp_ws_io *io = start(&first, SP_WS_RECEIVE, 0, NULL, 0, cancel);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING);
  assert(io->message.size == 4 && io->events == POLLIN && !data);
  busy_preserves(io);

  /* A second socket may progress without moving or resetting the parked one. */
  static const unsigned char other[] = "other";
  append_frame(&second.transport, 1, other, sizeof other - 1, 1);
  sp_ws_io snapshot; memcpy(&snapshot, io, sizeof snapshot);
  sp_ws_io *parallel = start(&second, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned other_kind = 0; unsigned char *other_data = NULL; size_t other_size = 0;
  assert(drain_receive(parallel, &other_kind, &other_data, &other_size) == SP_OK);
  assert(other_kind == 1 && other_size == sizeof other - 1 &&
         !memcmp(other_data, other, sizeof other - 1));
  dispose(parallel); free(other_data);
  assert(!memcmp(io, &snapshot, sizeof snapshot));

  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING);
  assert(io->sending && io->offset == 1 && io->events == POLLOUT &&
         io->out == io->control);
  busy_preserves(io);
  assert(drain_receive(io, &kind, &data, &size) == SP_OK);
  assert(kind == 2 && size == sizeof left + sizeof right - 2 &&
         !memcmp(data, "leftright", size));
  assert(first.transport.sent == sizeof ping && !io->message.data);
  dispose(io); assert(atomic_load(&cancel->refs) == 1);

  /* The tail of the continuation header and the next full message were read
   * ahead. The next operation must consume that cached message in order. */
  unsigned char *first_message = data;
  expect_message(&first, 2, next, sizeof next - 1);
  assert(!memcmp(first_message, "leftright", sizeof left + sizeof right - 2));
  free(first_message);

  /* An invalid pre-I/O request neither locks nor marks the socket broken. */
  int error;
  assert(!sp_ws_io_start(&first.socket, SP_WS_SEND, 0, NULL, 0, 5000,
                         NULL, &error));
  assert(error == SP_INVALID && !first.socket.broken);
  assert(!pthread_mutex_trylock(&first.socket.gate));
  pthread_mutex_unlock(&first.socket.gate);

  first.transport.send_data = closing; first.transport.send_size = sizeof closing;
  first.transport.sent = 0; first.transport.send_calls = 0;
  first.transport.send_flags = CURLWS_CLOSE; first.transport.pause_send = 1;
  io = start(&first, SP_WS_CLOSE, 1000, (const unsigned char *)"ok", 2, NULL);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_WS_PENDING);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_OK);
  dispose(io); assert(first.socket.closed);
  size_t recv_calls = first.transport.recv_calls, send_calls = first.transport.send_calls;
  io = start(&first, SP_WS_CLOSE, 1000, NULL, 0, NULL);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_OK); dispose(io);
  assert(first.transport.recv_calls == recv_calls &&
         first.transport.send_calls == send_calls);
  sp_cancel_free(cancel); teardown(&first); teardown(&second);
}

static void failed_reuse(int cancellation) {
  Fixture f; setup(&f);
  static const unsigned char partial[] = "partial";
  append_frame(&f.transport, 2, partial, sizeof partial - 1, 0);
  sp_cancel *cancel = sp_cancel_new(); assert(cancel);
  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, cancel);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING &&
         io->message.data);
  if (cancellation) sp_cancel_trigger(cancel); else io->deadline = 0;
  assert(receive_step(io, &kind, &data, &size) ==
         (cancellation ? SP_CANCELLED : SP_TIMEOUT));
  assert(!data && !size && f.socket.broken);
  dispose(io); assert(atomic_load(&cancel->refs) == 1);
  size_t calls = f.transport.recv_calls;
  io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  assert(receive_step(io, &kind, &data, &size) == SP_CLOSED && !data && !size);
  assert(f.transport.recv_calls == calls); dispose(io);
  sp_cancel_free(cancel); teardown(&f);
}

static void cached_messages_and_malformed_tail(void) {
  Fixture f; setup(&f);
  static const unsigned char one[] = "one", two[] = "two", tiny[] = "x";
  append_frame(&f.transport, 2, one, sizeof one - 1, 1);
  append_frame(&f.transport, 1, two, sizeof two - 1, 1);
  append_frame(&f.transport, 2, NULL, 0, 1);
  append_frame(&f.transport, 2, tiny, sizeof tiny - 1, 1);
  /* Noncanonical 126 encoding of length 125, immediately after valid frames. */
  unsigned char malformed[4 + 125] = {0x82, 126, 0, 125};
  memset(malformed + 4, 'm', 125);
  append_bytes(&f.transport, malformed, sizeof malformed);

  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(drain_receive(io, &kind, &data, &size) == SP_OK);
  assert(kind == 2 && size == sizeof one - 1 && !memcmp(data, one, size));
  assert(f.transport.recv_calls == 1);
  free(data); dispose(io);

  expect_message(&f, 1, two, sizeof two - 1);
  assert(f.transport.recv_calls == 1);
  expect_message(&f, 2, NULL, 0);
  assert(f.transport.recv_calls == 1);
  expect_message(&f, 2, tiny, sizeof tiny - 1);
  assert(f.transport.recv_calls == 1);

  io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  kind = 0; data = NULL; size = 0;
  assert(drain_receive(io, &kind, &data, &size) == SP_PROTOCOL);
  assert(!data && !size && f.socket.broken && f.transport.recv_calls == 1);
  dispose(io);
  io = start(&f, SP_WS_SEND, 2, tiny, sizeof tiny - 1, NULL);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_CLOSED);
  assert(f.transport.send_calls == 0); dispose(io);
  teardown(&f);
}

static void one_byte_header_boundaries(void) {
  Fixture f; setup(&f);
  static const size_t sizes[] = {0, 125, 126, 65535, 65536};
  unsigned char payload[65536];
  for (size_t i = 0; i < sizeof payload; i++)
    payload[i] = (unsigned char)(i * 131u + 17u);
  for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++)
    append_frame(&f.transport, 2, payload, sizes[i], 1);
  f.transport.recv_chunk_limit = 1;

  for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
    sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
    unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
    assert(drain_receive(io, &kind, &data, &size) == SP_OK);
    assert(kind == 2 && size == sizes[i]);
    assert(!size || !memcmp(data, payload, size));
    free(data); dispose(io);
  }
  teardown(&f);
}

static void small_final_message_keeps_exact_capacity(void) {
  Fixture f; setup(&f);
  static const unsigned char payload[] = "x";
  append_frame(&f.transport, 2, payload, sizeof payload - 1, 1);
  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  assert(sp_ws_io_advance(io) == SP_OK);
  assert(io->message.size == 1 && io->message.cap == 1);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(sp_ws_io_step(io, &kind, &data, &size) == SP_OK);
  assert(kind == 2 && size == 1 && data[0] == 'x');
  dispose(io); free(data); teardown(&f);
}

static void recv_error_bytes_are_never_returned(size_t terminal_call,
                                                CURLcode code,
                                                int expected_error,
                                                size_t prefix_bytes,
                                                size_t payload_size) {
  Fixture f; setup(&f);
  unsigned char payload[32768];
  memset(payload, 0x5a, sizeof payload);
  assert(payload_size <= sizeof payload);
  append_frame(&f.transport, 2, payload, payload_size, 1);
  f.transport.recv_chunk_limit = terminal_call == 2 ? 4 : 0;
  f.transport.terminal_call = terminal_call;
  f.transport.terminal_code = code;
  f.transport.terminal_bytes = prefix_bytes ? prefix_bytes : f.transport.wire_size;
  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(drain_receive(io, &kind, &data, &size) == expected_error);
  assert(!kind && !data && !size && f.socket.broken);
  if (terminal_call == 2) assert(f.transport.direct_recv_calls == 1);
  dispose(io);
  teardown(&f);
}
static void direct_cap_again_and_frame_boundary(void) {
  Fixture f; setup(&f);
  static const size_t payload_size = 131073;
  static const unsigned char tail[] = "next-frame";
  unsigned char payload[payload_size];
  for (size_t i = 0; i < sizeof payload; i++)
    payload[i] = (unsigned char)(i * 131u + 17u);
  append_frame(&f.transport, 2, payload, sizeof payload, 1);
  size_t first_frame_wire_size = f.transport.wire_size;
  append_frame(&f.transport, 1, tail, sizeof tail - 1, 1);

  /* The read-ahead fill is call 1; pause call 2 inside the direct-body path. */
  f.transport.pause_recv_call = 2;
  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING);
  assert(!data && !size && io->message.size == 16374);
  assert(io->frame_payload_left == payload_size - io->message.size);
  assert(f.transport.recv_calls == 2 && f.transport.direct_recv_calls == 1);
  assert(f.transport.max_direct_capacity == 65536u);
  assert(f.transport.wire_offset == 16384u && io->events == POLLIN);

  /* Resume with successful short reads, then finish exactly at this frame. */
  f.transport.recv_chunk_limit = 32768u;
  assert(drain_receive(io, &kind, &data, &size) == SP_OK);
  assert(kind == 2 && size == sizeof payload && !memcmp(data, payload, size));
  assert(f.transport.direct_recv_calls >= 5);
  assert(f.transport.direct_short_reads >= 3);
  assert(f.transport.max_direct_capacity == 65536u);
  /* The direct reads stop at the frame boundary; the following text frame remains. */
  assert(f.transport.wire_offset == first_frame_wire_size);
  free(data); dispose(io);

  expect_message(&f, 1, tail, sizeof tail - 1);
  assert(f.transport.wire_offset == f.transport.wire_size);
  teardown(&f);
}

int main(void) {
  sp_ws_io_free(NULL);
  for (unsigned i = 0; i < 32; i++) {
    successful_reuse(); failed_reuse(0); failed_reuse(1);
  }
  cached_messages_and_malformed_tail();
  one_byte_header_boundaries();
  direct_cap_again_and_frame_boundary();
  small_final_message_keeps_exact_capacity();
  recv_error_bytes_are_never_returned(1, CURLE_GOT_NOTHING, SP_CLOSED, 0, 19);
  recv_error_bytes_are_never_returned(1, CURLE_RECV_ERROR, SP_TRANSPORT, 0, 19);
  recv_error_bytes_are_never_returned(1, CURLE_AGAIN, SP_TRANSPORT, 0, 19);
  recv_error_bytes_are_never_returned(2, CURLE_RECV_ERROR, SP_TRANSPORT, 16384, 32768);
  puts("raw receive preserves cache order, fragment/control state, error isolation, and message ownership");
  return 0;
}
