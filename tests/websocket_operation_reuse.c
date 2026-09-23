/* Drive the real operation lifecycle with deterministic nonblocking transport
 * results. Network tests separately exercise framing/TLS and Bend callbacks. */
#define _POSIX_C_SOURCE 200809L
#include <stdlib.h>
#include <assert.h>
static size_t calloc_calls;
static void *counted_calloc(size_t count, size_t size) {
  calloc_calls++; return calloc(count, size);
}
#define calloc counted_calloc
#define curl_ws_recv controlled_ws_recv
#define curl_ws_send controlled_ws_send
#include "../native/scrapanium.c"
#undef calloc
#undef curl_ws_recv
#undef curl_ws_send

typedef struct { const unsigned char *data; size_t size; unsigned flags; } Frame;
typedef struct {
  const Frame *frames;
  size_t frame_count, frame_index, frame_offset, recv_calls, pause_recv_call;
  const unsigned char *send_data;
  size_t send_size, sent, send_calls;
  unsigned send_flags;
  int pause_send;
} Transport;

CURLcode controlled_ws_recv(CURL *easy, void *buffer, size_t capacity,
                            size_t *received, const struct curl_ws_frame **metadata) {
  Transport *t = (Transport *)easy;
  static struct curl_ws_frame meta;
  *received = 0; *metadata = NULL;
  if (++t->recv_calls == t->pause_recv_call) return CURLE_AGAIN;
  assert(t->frame_index < t->frame_count);
  const Frame *frame = t->frames + t->frame_index;
  size_t count = frame->size - t->frame_offset;
  if (count > capacity) count = capacity;
  memcpy(buffer, frame->data + t->frame_offset, count);
  meta = (struct curl_ws_frame){.flags = (int)frame->flags,
    .offset = (curl_off_t)t->frame_offset,
    .bytesleft = (curl_off_t)(frame->size - t->frame_offset - count), .len = count};
  t->frame_offset += count;
  if (t->frame_offset == frame->size) { t->frame_index++; t->frame_offset = 0; }
  *metadata = &meta; *received = count; return CURLE_OK;
}

CURLcode controlled_ws_send(CURL *easy, const void *buffer, size_t size,
                            size_t *sent, curl_off_t fragment_size, unsigned int flags) {
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
static void teardown(Fixture *f) { assert(!pthread_mutex_destroy(&f->socket.gate)); }

static sp_ws_io *start(Fixture *f, unsigned operation, unsigned kind,
                       const unsigned char *data, size_t size, sp_cancel *cancel) {
  size_t before = calloc_calls;
  int error = -1;
  sp_ws_io *io = sp_ws_io_start(&f->socket, operation, kind, data, size, 5000, cancel, &error);
  assert(io && error == SP_OK && calloc_calls == before);
  assert(io == &f->socket.operation);
  assert(!io->offset && !io->message.size && !io->message.cap && !io->message.data && !io->message_kind);
  if (operation != SP_WS_CLOSE) assert(!io->control_size && !io->after_send);
  return io;
}
static void dispose(sp_ws_io *io) {
  sp_ws *owner = io->socket;
  sp_ws_io_free(io);
  assert(!owner->operation.message.data && !owner->operation.cancel && !owner->operation.out && !owner->operation.socket);
  assert(!pthread_mutex_trylock(&owner->gate));
  pthread_mutex_unlock(&owner->gate);
}
static void busy_preserves(sp_ws_io *io) {
  sp_ws_io snapshot; memcpy(&snapshot, io, sizeof snapshot);
  int error = -1;
  assert(!sp_ws_io_start(io->socket, SP_WS_RECEIVE, 0, NULL, 0, 5000, NULL, &error));
  assert(error == SP_BUSY && !memcmp(io, &snapshot, sizeof snapshot));
}
static int receive_step(sp_ws_io *io, unsigned *kind, unsigned char **data, size_t *size) {
  return sp_ws_io_step(io, kind, data, size);
}

static void successful_reuse(void) {
  Fixture first, second; setup(&first); setup(&second);
  const unsigned char sending[] = "send\0bytes";
  first.transport = (Transport){.send_data = sending, .send_size = sizeof sending - 1,
    .send_flags = CURLWS_BINARY, .pause_send = 1};
  sp_cancel *cancel = sp_cancel_new(); assert(cancel);
  sp_ws_io *original = start(&first, SP_WS_SEND, 2, sending, sizeof sending - 1, cancel);
  assert(atomic_load(&cancel->refs) == 2);
  assert(sp_ws_io_step(original, NULL, NULL, NULL) == SP_WS_PENDING);
  assert(original->offset == 1 && original->events == POLLOUT);
  busy_preserves(original);
  assert(sp_ws_io_step(original, NULL, NULL, NULL) == SP_OK);
  assert(first.transport.sent == sizeof sending - 1);
  dispose(original); assert(atomic_load(&cancel->refs) == 1);

  static const unsigned char ping[] = {'p', 0, 255};
  const Frame fragments[] = {
    {(const unsigned char *)"left", 4, CURLWS_BINARY | CURLWS_CONT},
    {ping, sizeof ping, CURLWS_PING}, {(const unsigned char *)"right", 5, CURLWS_BINARY}};
  first.transport = (Transport){.frames = fragments, .frame_count = 3, .pause_recv_call = 2,
    .send_data = ping, .send_size = sizeof ping, .send_flags = CURLWS_PONG, .pause_send = 1};
  sp_ws_io *io = start(&first, SP_WS_RECEIVE, 0, NULL, 0, cancel);
  assert(io == original);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING);
  assert(io->message.size == 4 && io->events == POLLIN && !data);
  busy_preserves(io);

  /* A second socket may progress without moving or resetting the parked one. */
  const Frame other[] = {{(const unsigned char *)"other", 5, CURLWS_TEXT}};
  second.transport = (Transport){.frames = other, .frame_count = 1};
  sp_ws_io snapshot; memcpy(&snapshot, io, sizeof snapshot);
  sp_ws_io *parallel = start(&second, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned other_kind = 0; unsigned char *other_data = NULL; size_t other_size = 0;
  assert(parallel != io && receive_step(parallel, &other_kind, &other_data, &other_size) == SP_OK);
  assert(other_kind == 1 && other_size == 5 && !memcmp(other_data, "other", 5));
  dispose(parallel); free(other_data);
  assert(!memcmp(io, &snapshot, sizeof snapshot));

  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING);
  assert(io->sending && io->offset == 1 && io->events == POLLOUT && io->out == io->control);
  busy_preserves(io);
  assert(receive_step(io, &kind, &data, &size) == SP_OK);
  assert(kind == 2 && size == 9 && !memcmp(data, "leftright", 9));
  assert(first.transport.sent == sizeof ping && !io->message.data);
  dispose(io); assert(atomic_load(&cancel->refs) == 1);

  /* Caller-owned bytes survive later reuse, including a different frame type. */
  const Frame text[] = {{(const unsigned char *)"next", 4, CURLWS_TEXT}};
  first.transport = (Transport){.frames = text, .frame_count = 1};
  io = start(&first, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  unsigned next_kind = 0; unsigned char *next_data = NULL; size_t next_size = 0;
  assert(receive_step(io, &next_kind, &next_data, &next_size) == SP_OK);
  assert(next_kind == 1 && next_size == 4 && !memcmp(next_data, "next", 4));
  dispose(io);
  assert(!memcmp(data, "leftright", 9)); free(data); free(next_data);

  /* An invalid pre-I/O request neither locks nor marks the socket broken. */
  int error;
  assert(!sp_ws_io_start(&first.socket, SP_WS_SEND, 0, NULL, 0, 5000, NULL, &error));
  assert(error == SP_INVALID && !first.socket.broken);
  assert(!pthread_mutex_trylock(&first.socket.gate)); pthread_mutex_unlock(&first.socket.gate);

  const unsigned char closing[] = {3, 232, 'o', 'k'};
  const Frame ack[] = {{closing, sizeof closing, CURLWS_CLOSE}};
  first.transport = (Transport){.frames = ack, .frame_count = 1, .send_data = closing,
    .send_size = sizeof closing, .send_flags = CURLWS_CLOSE, .pause_send = 1};
  io = start(&first, SP_WS_CLOSE, 1000, (const unsigned char *)"ok", 2, NULL);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_WS_PENDING);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_OK);
  dispose(io); assert(first.socket.closed);
  size_t recv_calls = first.transport.recv_calls, send_calls = first.transport.send_calls;
  io = start(&first, SP_WS_CLOSE, 1000, NULL, 0, NULL);
  assert(sp_ws_io_step(io, NULL, NULL, NULL) == SP_OK); dispose(io);
  assert(first.transport.recv_calls == recv_calls && first.transport.send_calls == send_calls);
  sp_cancel_free(cancel); teardown(&first); teardown(&second);
}

static void failed_reuse(int cancellation) {
  Fixture f; setup(&f);
  const Frame partial[] = {{(const unsigned char *)"partial", 7, CURLWS_BINARY | CURLWS_CONT}};
  f.transport = (Transport){.frames = partial, .frame_count = 1, .pause_recv_call = 2};
  sp_cancel *cancel = sp_cancel_new(); assert(cancel);
  sp_ws_io *io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, cancel);
  unsigned kind = 0; unsigned char *data = NULL; size_t size = 0;
  assert(receive_step(io, &kind, &data, &size) == SP_WS_PENDING && io->message.data);
  if (cancellation) sp_cancel_trigger(cancel); else io->deadline = 0;
  assert(receive_step(io, &kind, &data, &size) == (cancellation ? SP_CANCELLED : SP_TIMEOUT));
  assert(!data && !size && f.socket.broken);
  dispose(io); assert(atomic_load(&cancel->refs) == 1);
  size_t calls = f.transport.recv_calls;
  io = start(&f, SP_WS_RECEIVE, 0, NULL, 0, NULL);
  assert(receive_step(io, &kind, &data, &size) == SP_CLOSED && !data && !size);
  assert(f.transport.recv_calls == calls); dispose(io);
  sp_cancel_free(cancel); teardown(&f);
}

int main(void) {
  sp_ws_io_free(NULL);
  for (unsigned i = 0; i < 32; i++) { successful_reuse(); failed_reuse(0); failed_reuse(1); }
  puts("operation reuse preserves parked state, ownership, busy gates, controls, and failure cleanup");
  return 0;
}
