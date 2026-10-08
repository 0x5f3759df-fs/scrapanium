/***************************************************************************
 *                                  _   _ ____  _
 *  Project                     ___| | | |  _ \| |
 *                             / __| | | | |_) | |
 *                            | (__| |_| |  _ <| |___
 *                             \___|\___/|_| \_\_____|
 *
 * Copyright (C) Daniel Stenberg, <daniel@haxx.se>, et al.
 *
 * This software is licensed as described in the file COPYING, which
 * you should have received as part of this distribution. The terms
 * are also available at https://curl.se/docs/copyright.html.
 *
 * You may opt to use, copy, modify, merge, publish, distribute and/or sell
 * copies of the Software, and permit persons to whom the Software is
 * furnished to do so, under the terms of the COPYING file.
 *
 * This software is distributed on an "AS IS" basis, WITHOUT WARRANTY OF ANY
 * KIND, either express or implied.
 *
 * SPDX-License-Identifier: curl
 *
 ***************************************************************************/
#include "curl_setup.h"

#include "bufq.h"
#include "ws_diag.h"

struct bufq_pin {
  struct buf_chunk *owner;
  const uint8_t *data;
  size_t len;
  bool diag_ws_recv;
};

static void chunk_ref_init(struct buf_chunk *chunk)
{
  atomic_init(&chunk->refs, 1);
}

static bool chunk_ref_acquire(struct buf_chunk *chunk)
{
  unsigned int refs = atomic_load_explicit(&chunk->refs, memory_order_relaxed);
  while(refs && refs < UINT_MAX) {
    if(atomic_compare_exchange_weak_explicit(&chunk->refs, &refs, refs + 1,
                                             memory_order_acquire,
                                             memory_order_relaxed))
      return TRUE;
  }
  return FALSE;
}

static bool chunk_ref_shared(const struct buf_chunk *chunk)
{
  return atomic_load_explicit(&chunk->refs, memory_order_acquire) > 1;
}

static void chunk_ref_release(struct buf_chunk *chunk)
{
  if(atomic_fetch_sub_explicit(&chunk->refs, 1, memory_order_acq_rel) == 1) {
    if(chunk->diag_ws_recv_chunk) {
      uint64_t bytes = sizeof(*chunk) + chunk->dlen;
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_PHYSICAL_FREES, 1);
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_PHYSICAL_FREE_REQUEST_BYTES, bytes);
      Curl_ws_diag_sub(CURL_WS_DIAG_CHUNKS_LIVE, 1);
      Curl_ws_diag_sub(CURL_WS_DIAG_CHUNKS_LIVE_REQUEST_BYTES, bytes);
    }
    curlx_free(chunk);
  }
}

static bool chunk_is_empty(const struct buf_chunk *chunk)
{
  return chunk->r_offset >= chunk->w_offset;
}

static bool chunk_is_full(const struct buf_chunk *chunk)
{
  return chunk->w_offset >= chunk->dlen;
}

static size_t chunk_len(const struct buf_chunk *chunk)
{
  return chunk->w_offset - chunk->r_offset;
}

static void chunk_reset(struct buf_chunk *chunk)
{
  chunk->next = NULL;
  chunk->r_offset = chunk->w_offset = 0;
}

static size_t chunk_append(struct buf_chunk *chunk,
                           const uint8_t *buf, size_t len)
{
  uint8_t *p = &chunk->x.data[chunk->w_offset];
  size_t n = chunk->dlen - chunk->w_offset;
  DEBUGASSERT(chunk->dlen >= chunk->w_offset);
  if(n) {
    n = CURLMIN(n, len);
    memcpy(p, buf, n);
    chunk->w_offset += n;
  }
  return n;
}

static size_t chunk_read(struct buf_chunk *chunk,
                         uint8_t *buf, size_t len)
{
  uint8_t *p = &chunk->x.data[chunk->r_offset];
  size_t n = chunk->w_offset - chunk->r_offset;
  DEBUGASSERT(chunk->w_offset >= chunk->r_offset);
  if(!n) {
    return 0;
  }
  else if(n <= len) {
    memcpy(buf, p, n);
    chunk->r_offset = chunk->w_offset = 0;
    return n;
  }
  else {
    memcpy(buf, p, len);
    chunk->r_offset += len;
    return len;
  }
}

static CURLcode chunk_slurpn(struct buf_chunk *chunk, size_t max_len,
                             Curl_bufq_reader *reader,
                             void *reader_ctx, size_t *pnread)
{
  uint8_t *p = &chunk->x.data[chunk->w_offset];
  size_t n = chunk->dlen - chunk->w_offset; /* free amount */
  CURLcode result;

  *pnread = 0;
  DEBUGASSERT(chunk->dlen >= chunk->w_offset);
  if(!n)
    return CURLE_AGAIN;
  if(max_len && n > max_len)
    n = max_len;
  result = reader(reader_ctx, p, n, pnread);
  if(!result) {
    DEBUGASSERT(*pnread <= n);
    chunk->w_offset += *pnread;
  }
  return result;
}

static void chunk_peek(const struct buf_chunk *chunk,
                       const uint8_t **pbuf, size_t *plen)
{
  DEBUGASSERT(chunk->w_offset >= chunk->r_offset);
  *pbuf = &chunk->x.data[chunk->r_offset];
  *plen = chunk->w_offset - chunk->r_offset;
}

static void chunk_peek_at(const struct buf_chunk *chunk, size_t offset,
                          const uint8_t **pbuf, size_t *plen)
{
  offset += chunk->r_offset;
  DEBUGASSERT(chunk->w_offset >= offset);
  *pbuf = &chunk->x.data[offset];
  *plen = chunk->w_offset - offset;
}

static size_t chunk_skip(struct buf_chunk *chunk, size_t amount)
{
  size_t n = chunk->w_offset - chunk->r_offset;
  DEBUGASSERT(chunk->w_offset >= chunk->r_offset);
  if(n) {
    n = CURLMIN(n, amount);
    chunk->r_offset += n;
    if(chunk->r_offset == chunk->w_offset)
      chunk->r_offset = chunk->w_offset = 0;
  }
  return n;
}

static void chunk_list_free(struct buf_chunk **anchor)
{
  struct buf_chunk *chunk;
  while(*anchor) {
    chunk = *anchor;
    *anchor = chunk->next;
    chunk_ref_release(chunk);
  }
}

void Curl_bufcp_init(struct bufc_pool *pool,
                     size_t chunk_size, size_t spare_max)
{
  DEBUGASSERT(chunk_size > 0);
  DEBUGASSERT(spare_max > 0);
  memset(pool, 0, sizeof(*pool));
  pool->chunk_size = chunk_size;
  pool->spare_max = spare_max;
}

static CURLcode bufcp_take(struct bufc_pool *pool,
                           struct buf_chunk **pchunk)
{
  struct buf_chunk *chunk = NULL;

  if(pool->spare) {
    chunk = pool->spare;
    pool->spare = chunk->next;
    --pool->spare_count;
    chunk_reset(chunk);
    *pchunk = chunk;
    return CURLE_OK;
  }

  /* Check for integer overflow before allocation */
  if(pool->chunk_size > SIZE_MAX - sizeof(*chunk)) {
    *pchunk = NULL;
    return CURLE_OUT_OF_MEMORY;
  }

  chunk = curlx_calloc(1, sizeof(*chunk) + pool->chunk_size);
  if(!chunk) {
    *pchunk = NULL;
    return CURLE_OUT_OF_MEMORY;
  }
  chunk->dlen = pool->chunk_size;
  chunk_ref_init(chunk);
  *pchunk = chunk;
  return CURLE_OK;
}

static void bufcp_put(struct bufc_pool *pool,
                      struct buf_chunk *chunk)
{
  if(pool->spare_count >= pool->spare_max) {
    chunk_ref_release(chunk);
  }
  else {
    chunk_reset(chunk);
    chunk->next = pool->spare;
    pool->spare = chunk;
    ++pool->spare_count;
  }
}

void Curl_bufcp_free(struct bufc_pool *pool)
{
  chunk_list_free(&pool->spare);
  pool->spare_count = 0;
}

static void bufq_init(struct bufq *q, struct bufc_pool *pool,
                      size_t chunk_size, size_t max_chunks, int opts)
{
  DEBUGASSERT(chunk_size > 0);
  DEBUGASSERT(max_chunks > 0);
  memset(q, 0, sizeof(*q));
  q->chunk_size = chunk_size;
  q->max_chunks = max_chunks;
  q->pool = pool;
  q->opts = opts;
}

void Curl_bufq_init2(struct bufq *q, size_t chunk_size, size_t max_chunks,
                     int opts)
{
  bufq_init(q, NULL, chunk_size, max_chunks, opts);
}

void Curl_bufq_init(struct bufq *q, size_t chunk_size, size_t max_chunks)
{
  bufq_init(q, NULL, chunk_size, max_chunks, BUFQ_OPT_NONE);
}

void Curl_bufq_initp(struct bufq *q, struct bufc_pool *pool,
                     size_t max_chunks, int opts)
{
  bufq_init(q, pool, pool->chunk_size, max_chunks, opts);
}

void Curl_bufq_free(struct bufq *q)
{
  if(q->diag_ws_recv) {
    const struct buf_chunk *chunk;
    for(chunk = q->head; chunk; chunk = chunk->next)
      Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_FREE_CHUNK_REFS, 1);
    for(chunk = q->spare; chunk; chunk = chunk->next)
      Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_FREE_CHUNK_REFS, 1);
  }
  chunk_list_free(&q->head);
  chunk_list_free(&q->spare);
  q->tail = NULL;
  q->chunk_count = 0;
}

void Curl_bufq_reset(struct bufq *q)
{
  struct buf_chunk *chunk;
  while(q->head) {
    chunk = q->head;
    q->head = chunk->next;
    if(q->pins_enabled && chunk_ref_shared(chunk)) {
      if(q->diag_ws_recv)
        Curl_ws_diag_add(CURL_WS_DIAG_SHARED_DETACH_RESET, 1);
      chunk_ref_release(chunk);
      --q->chunk_count;
    }
    else {
      if(q->diag_ws_recv)
        Curl_ws_diag_add(CURL_WS_DIAG_SPARE_RETURN, 1);
      chunk->next = q->spare;
      q->spare = chunk;
    }
  }
  q->tail = NULL;
}

size_t Curl_bufq_len(const struct bufq *q)
{
  const struct buf_chunk *chunk = q->head;
  size_t len = 0;
  while(chunk) {
    len += chunk_len(chunk);
    chunk = chunk->next;
  }
  return len;
}

bool Curl_bufq_is_empty(const struct bufq *q)
{
  return !q->head || chunk_is_empty(q->head);
}

bool Curl_bufq_is_full(const struct bufq *q)
{
  if(!q->tail || q->spare)
    return FALSE;
  if(q->chunk_count < q->max_chunks)
    return FALSE;
  if(q->chunk_count > q->max_chunks)
    return TRUE;
  /* we have no spares and cannot make more, is the tail full? */
  return chunk_is_full(q->tail);
}

static struct buf_chunk *get_spare(struct bufq *q)
{
  struct buf_chunk *chunk = NULL;

  if(q->spare) {
    chunk = q->spare;
    q->spare = chunk->next;
    chunk->diag_ws_recv_chunk = q->diag_ws_recv;
    if(q->diag_ws_recv)
      Curl_ws_diag_add(CURL_WS_DIAG_SPARE_REUSE, 1);
    chunk_reset(chunk);
    return chunk;
  }

  if(q->chunk_count >= q->max_chunks && (!(q->opts & BUFQ_OPT_SOFT_LIMIT)))
    return NULL;

  if(q->pool) {
    if(bufcp_take(q->pool, &chunk))
      return NULL;
    chunk->diag_ws_recv_chunk = q->diag_ws_recv;
    ++q->chunk_count;
    return chunk;
  }
  else {
    size_t alloc_size;
    /* Check for integer overflow before allocation */
    if(q->chunk_size > SIZE_MAX - sizeof(*chunk)) {
      return NULL;
    }
    alloc_size = sizeof(*chunk) + q->chunk_size;
    if(q->diag_ws_recv) {
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_ALLOC_ATTEMPTS, 1);
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_ALLOC_REQUEST_BYTES, alloc_size);
    }
    chunk = curlx_calloc(1, alloc_size);
    if(!chunk)
      return NULL;
    chunk->dlen = q->chunk_size;
    chunk->diag_ws_recv_chunk = q->diag_ws_recv;
    chunk_ref_init(chunk);
    ++q->chunk_count;
    if(q->diag_ws_recv) {
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_ALLOC_SUCCESSES, 1);
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNK_ALLOC_SUCCESS_BYTES, alloc_size);
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNKS_LIVE, 1);
      Curl_ws_diag_add(CURL_WS_DIAG_CHUNKS_LIVE_REQUEST_BYTES, alloc_size);
    }
    return chunk;
  }
}

static void prune_head(struct bufq *q)
{
  struct buf_chunk *chunk;

  while(q->head && chunk_is_empty(q->head)) {
    chunk = q->head;
    q->head = chunk->next;
    if(q->tail == chunk)
      q->tail = q->head;
    if(q->pins_enabled && chunk_ref_shared(chunk)) {
      if(q->diag_ws_recv)
        Curl_ws_diag_add(CURL_WS_DIAG_SHARED_DETACH_PRUNE, 1);
      chunk_ref_release(chunk);
      --q->chunk_count;
    }
    else if(q->pool) {
      bufcp_put(q->pool, chunk);
      --q->chunk_count;
    }
    else if((q->chunk_count > q->max_chunks) ||
            (q->opts & BUFQ_OPT_NO_SPARES)) {
      if(q->diag_ws_recv)
        Curl_ws_diag_add(CURL_WS_DIAG_UNSHARED_DROP, 1);
      /* SOFT_LIMIT allowed us more than max. free spares until
       * we are at max again. Or free them if we are configured
       * to not use spares. */
      chunk_ref_release(chunk);
      --q->chunk_count;
    }
    else {
      if(q->diag_ws_recv)
        Curl_ws_diag_add(CURL_WS_DIAG_SPARE_RETURN, 1);
      chunk->next = q->spare;
      q->spare = chunk;
    }
  }
}

static struct buf_chunk *get_non_full_tail(struct bufq *q)
{
  struct buf_chunk *chunk;

  if(q->tail && !chunk_is_full(q->tail)) {
    if(q->diag_ws_recv)
      Curl_ws_diag_add(CURL_WS_DIAG_TAIL_REUSE, 1);
    return q->tail;
  }
  chunk = get_spare(q);
  if(chunk) {
    /* new tail, and possibly new head */
    if(q->tail) {
      q->tail->next = chunk;
      q->tail = chunk;
    }
    else {
      DEBUGASSERT(!q->head);
      q->head = q->tail = chunk;
    }
  }
  return chunk;
}

CURLcode Curl_bufq_write(struct bufq *q,
                         const uint8_t *buf, size_t len,
                         size_t *pnwritten)
{
  struct buf_chunk *tail;
  size_t n;

  DEBUGASSERT(q->max_chunks > 0);
  *pnwritten = 0;
  while(len) {
    tail = get_non_full_tail(q);
    if(!tail) {
      if((q->chunk_count < q->max_chunks) || (q->opts & BUFQ_OPT_SOFT_LIMIT))
        /* should have gotten a tail, but did not */
        return CURLE_OUT_OF_MEMORY;
      break;
    }
    n = chunk_append(tail, buf, len);
    if(!n)
      break;
    *pnwritten += n;
    buf += n;
    len -= n;
  }
  return (!*pnwritten && len) ? CURLE_AGAIN : CURLE_OK;
}

CURLcode Curl_bufq_cwrite(struct bufq *q,
                          const char *buf, size_t len,
                          size_t *pnwritten)
{
  return Curl_bufq_write(q, (const uint8_t *)buf, len, pnwritten);
}

CURLcode Curl_bufq_read(struct bufq *q, uint8_t *buf, size_t len,
                        size_t *pnread)
{
  *pnread = 0;
  while(len && q->head) {
    size_t n = chunk_read(q->head, buf, len);
    if(n) {
      *pnread += n;
      buf += n;
      len -= n;
    }
    prune_head(q);
  }
  return (!*pnread) ? CURLE_AGAIN : CURLE_OK;
}

CURLcode Curl_bufq_cread(struct bufq *q, char *buf, size_t len,
                         size_t *pnread)
{
  return Curl_bufq_read(q, (uint8_t *)buf, len, pnread);
}

bool Curl_bufq_peek(struct bufq *q,
                    const uint8_t **pbuf, size_t *plen)
{
  if(q->head && chunk_is_empty(q->head)) {
    prune_head(q);
  }
  if(q->head && !chunk_is_empty(q->head)) {
    chunk_peek(q->head, pbuf, plen);
    return TRUE;
  }
  *pbuf = NULL;
  *plen = 0;
  return FALSE;
}

bool Curl_bufq_peek_at(struct bufq *q, size_t offset,
                       const uint8_t **pbuf, size_t *plen)
{
  struct buf_chunk *c = q->head;
  size_t clen;

  while(c) {
    clen = chunk_len(c);
    if(!clen)
      break;
    if(offset >= clen) {
      offset -= clen;
      c = c->next;
      continue;
    }
    chunk_peek_at(c, offset, pbuf, plen);
    return TRUE;
  }
  *pbuf = NULL;
  *plen = 0;
  return FALSE;
}

void Curl_bufq_skip(struct bufq *q, size_t amount)
{
  size_t n;
  size_t requested = amount;

  if(q->diag_ws_recv) {
    Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_SKIP_CALLS, 1);
    Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_SKIP_REQUEST_BYTES, requested);
  }
  while(amount && q->head) {
    n = chunk_skip(q->head, amount);
    amount -= n;
    prune_head(q);
  }
}

bool Curl_bufq_enable_pins(struct bufq *q)
{
  if(!q || q->head || q->tail)
    return FALSE;
  q->pins_enabled = TRUE;
  q->diag_ws_recv = TRUE;
  return TRUE;
}

CURLcode Curl_bufq_pin_head(struct bufq *q, size_t len,
                            struct bufq_pin **ppin)
{
  const uint8_t *data;
  size_t available;
  struct bufq_pin *pin;
  if(ppin)
    *ppin = NULL;
  if(!q || !ppin || !q->pins_enabled || !len ||
     !Curl_bufq_peek(q, &data, &available) || len > available)
    return CURLE_BAD_FUNCTION_ARGUMENT;
  if(q->diag_ws_recv) {
    Curl_ws_diag_add(CURL_WS_DIAG_PIN_ALLOC_ATTEMPTS, 1);
    Curl_ws_diag_add(CURL_WS_DIAG_PIN_ALLOC_REQUEST_BYTES, sizeof(*pin));
  }
  pin = curlx_calloc(1, sizeof(*pin));
  if(!pin) {
    if(q->diag_ws_recv)
      Curl_ws_diag_add(CURL_WS_DIAG_PIN_ALLOC_FAILURES, 1);
    return CURLE_OUT_OF_MEMORY;
  }
  if(q->diag_ws_recv) {
    pin->diag_ws_recv = TRUE;
    Curl_ws_diag_add(CURL_WS_DIAG_PIN_ALLOC_SUCCESSES, 1);
    Curl_ws_diag_add(CURL_WS_DIAG_PINS_LIVE, 1);
  }
  if(!chunk_ref_acquire(q->head)) {
    if(q->diag_ws_recv) {
      Curl_ws_diag_add(CURL_WS_DIAG_PIN_ACQUIRE_FAILURES, 1);
      Curl_ws_diag_sub(CURL_WS_DIAG_PINS_LIVE, 1);
    }
    curlx_free(pin);
    return CURLE_OUT_OF_MEMORY;
  }
  pin->owner = q->head;
  pin->data = data;
  pin->len = len;
  *ppin = pin;
  return CURLE_OK;
}

const uint8_t *Curl_bufq_pin_data(const struct bufq_pin *pin)
{
  return pin ? pin->data : NULL;
}

size_t Curl_bufq_pin_len(const struct bufq_pin *pin)
{
  return pin ? pin->len : 0;
}

size_t Curl_bufq_pin_allocated(const struct bufq_pin *pin)
{
  return pin ? sizeof(*pin->owner) + pin->owner->dlen + sizeof(*pin) : 0;
}

size_t Curl_bufq_pin_reservation(const struct bufq *q)
{
  if(!q || q->chunk_size > SIZE_MAX - sizeof(struct buf_chunk) -
                              sizeof(struct bufq_pin))
    return 0;
  return sizeof(struct buf_chunk) + q->chunk_size + sizeof(struct bufq_pin);
}

void Curl_bufq_pin_release(struct bufq_pin *pin)
{
  if(pin) {
    if(pin->diag_ws_recv) {
      Curl_ws_diag_add(CURL_WS_DIAG_PIN_RELEASES, 1);
      Curl_ws_diag_sub(CURL_WS_DIAG_PINS_LIVE, 1);
    }
    chunk_ref_release(pin->owner);
    curlx_free(pin);
  }
}

size_t Curl_bufq_pin_struct_size(void)
{
  return sizeof(struct bufq_pin);
}

CURLcode Curl_bufq_pass(struct bufq *q, Curl_bufq_writer *writer,
                        void *writer_ctx, size_t *pwritten)
{
  const uint8_t *buf;
  size_t blen;
  CURLcode result = CURLE_OK;

  *pwritten = 0;
  while(Curl_bufq_peek(q, &buf, &blen)) {
    size_t chunk_written;

    result = writer(writer_ctx, buf, blen, &chunk_written);
    if(result) {
      if((result == CURLE_AGAIN) && *pwritten) {
        /* blocked on subsequent write, report success */
        result = CURLE_OK;
      }
      break;
    }
    if(!chunk_written) {
      if(!*pwritten) {
        /* treat as blocked */
        result = CURLE_AGAIN;
      }
      break;
    }
    *pwritten += chunk_written;
    Curl_bufq_skip(q, chunk_written);
  }
  return result;
}

CURLcode Curl_bufq_write_pass(struct bufq *q,
                              const uint8_t *buf, size_t len,
                              Curl_bufq_writer *writer, void *writer_ctx,
                              size_t *pwritten)
{
  CURLcode result = CURLE_OK;
  size_t n;

  *pwritten = 0;
  while(len) {
    if(Curl_bufq_is_full(q)) {
      /* try to make room in case we are full */
      result = Curl_bufq_pass(q, writer, writer_ctx, &n);
      if(result) {
        if(result != CURLE_AGAIN) {
          /* real error, fail */
          return result;
        }
        /* would block, bufq is full, give up */
        break;
      }
    }

    /* Add to bufq as much as there is room for */
    result = Curl_bufq_write(q, buf, len, &n);
    if(result) {
      if(result != CURLE_AGAIN)
        /* real error, fail */
        return result;
      /* result == CURLE_AGAIN */
      if(*pwritten)
        /* we did write successfully before */
        result = CURLE_OK;
      return result;
    }
    else if(n == 0)
      /* edge case of writer returning 0 (and len is >0)
       * break or we might enter an infinite loop here */
      break;

    /* Track what we added to bufq */
    buf += n;
    len -= n;
    *pwritten += n;
  }

  return (!*pwritten && len) ? CURLE_AGAIN : CURLE_OK;
}

CURLcode Curl_bufq_sipn(struct bufq *q, size_t max_len,
                        Curl_bufq_reader *reader, void *reader_ctx,
                        size_t *pnread)
{
  struct buf_chunk *tail = NULL;

  *pnread = 0;
  tail = get_non_full_tail(q);
  if(!tail) {
    if(q->chunk_count < q->max_chunks)
      return CURLE_OUT_OF_MEMORY;
    /* full, blocked */
    return CURLE_AGAIN;
  }

  return chunk_slurpn(tail, max_len, reader, reader_ctx, pnread);
}

/**
 * Read up to `max_len` bytes and append it to the end of the buffer queue.
 * if `max_len` is 0, no limit is imposed and the call behaves exactly
 * the same as `Curl_bufq_slurp()`.
 * Returns the total amount of buf read (may be 0) in `pnread` or error
 * Note that even in case of an error chunks may have been read and
 * the buffer queue will have different length than before.
 */
static CURLcode bufq_slurpn(struct bufq *q, size_t max_len,
                            Curl_bufq_reader *reader, void *reader_ctx,
                            size_t *pnread)
{
  CURLcode result;

  *pnread = 0;
  while(1) {
    size_t n;
    result = Curl_bufq_sipn(q, max_len, reader, reader_ctx, &n);
    if(result) {
      if(!*pnread || result != CURLE_AGAIN) {
        /* blocked on first read or real error, fail */
        return result;
      }
      result = CURLE_OK;
      break;
    }
    else if(n == 0) {
      /* eof, result remains CURLE_OK */
      break;
    }
    *pnread += n;
    if(max_len) {
      DEBUGASSERT(n <= max_len);
      max_len -= n;
      if(!max_len)
        break;
    }
    /* give up slurping when we get less bytes than we asked for */
    if(q->tail && !chunk_is_full(q->tail))
      break;
  }
  return result;
}

CURLcode Curl_bufq_slurp(struct bufq *q, Curl_bufq_reader *reader,
                         void *reader_ctx, size_t *pnread)
{
  CURLcode result = bufq_slurpn(q, 0, reader, reader_ctx, pnread);
  if(q->diag_ws_recv) {
    Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_SLURP_CALLS, 1);
    if(!result && pnread)
      Curl_ws_diag_add(CURL_WS_DIAG_QUEUE_SLURP_RETURN_BYTES, *pnread);
  }
  return result;
}
