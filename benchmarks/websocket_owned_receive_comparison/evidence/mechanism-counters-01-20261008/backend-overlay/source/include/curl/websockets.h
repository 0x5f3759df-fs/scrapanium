#ifndef CURLINC_WEBSOCKETS_H
#define CURLINC_WEBSOCKETS_H

#include <stdint.h>
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

#ifdef __cplusplus
extern "C" {
#endif

struct curl_ws_frame {
  int age;              /* zero */
  int flags;            /* See the CURLWS_* defines */
  curl_off_t offset;    /* the offset of this data into the frame */
  curl_off_t bytesleft; /* number of pending bytes left of the payload */
  size_t len;           /* size of the current data chunk */
};

/* flag bits */
#define CURLWS_TEXT       (1 << 0)
#define CURLWS_BINARY     (1 << 1)
#define CURLWS_CONT       (1 << 2)
#define CURLWS_CLOSE      (1 << 3)
#define CURLWS_PING       (1 << 4)
#define CURLWS_OFFSET     (1 << 5)

/*
 * NAME curl_ws_recv()
 *
 * DESCRIPTION
 *
 * Receives data from the websocket connection. Use after successful
 * curl_easy_perform() with CURLOPT_CONNECT_ONLY option.
 */
CURL_EXTERN CURLcode curl_ws_recv(CURL *curl, void *buffer, size_t buflen,
                                  size_t *recv,
                                  const struct curl_ws_frame **metap);

/* Experimental owned-payload receive API. A returned payload owns an
 * immutable reference to the decoded plaintext queue chunk and survives
 * subsequent receives and easy-handle cleanup until explicitly released.
 * max_allocation bounds the cURL-side bytes retained by one payload; an
 * allocation failure or insufficient bound returns CURLE_OUT_OF_MEMORY
 * without consuming that payload, so callers can fall back to curl_ws_recv. */
#define CURLWS_OWNED_CHUNKS_API 1
struct curl_ws_payload;
CURL_EXTERN CURLcode curl_ws_recv_owned(CURL *curl, size_t max_allocation,
                                        struct curl_ws_payload **payload,
                                        const struct curl_ws_frame **metap);
CURL_EXTERN size_t curl_ws_recv_owned_reservation(CURL *curl);
CURL_EXTERN const unsigned char *curl_ws_payload_data(
  const struct curl_ws_payload *payload);
CURL_EXTERN size_t curl_ws_payload_size(const struct curl_ws_payload *payload);
CURL_EXTERN size_t curl_ws_payload_allocation_size(
  const struct curl_ws_payload *payload);
CURL_EXTERN void curl_ws_payload_free(struct curl_ws_payload *payload);

/* Private diagnostic extension for the isolated owned-receive census.
 * Counters are process-wide cumulative totals. The snapshot has
 * curl_ws_diag_snapshot_count() unsigned 64-bit values; names are stable by
 * index for this instrumented backend build. Snapshot values are read
 * independently and are not a transactional freeze during concurrent use. */
CURL_EXTERN size_t curl_ws_diag_snapshot_count(void);
CURL_EXTERN const char *curl_ws_diag_counter_name(size_t index);
CURL_EXTERN void curl_ws_diag_snapshot(uint64_t *values, size_t capacity);

/* flags for curl_ws_send() */
#define CURLWS_PONG       (1 << 6)

/*
 * NAME curl_ws_send()
 *
 * DESCRIPTION
 *
 * Sends data over the websocket connection. Use after successful
 * curl_easy_perform() with CURLOPT_CONNECT_ONLY option.
 */
CURL_EXTERN CURLcode curl_ws_send(CURL *curl, const void *buffer_arg,
                                  size_t buflen, size_t *sent,
                                  curl_off_t fragsize,
                                  unsigned int flags);

/*
 * NAME curl_ws_start_frame()
 *
 * DESCRIPTION
 *
 * Buffers a websocket frame header with the given flags and length.
 * Errors when a previous frame is not complete, e.g. not all its
 * payload has been added.
 */
CURL_EXTERN CURLcode curl_ws_start_frame(CURL *curl,
                                         unsigned int flags,
                                         curl_off_t frame_len);

/* bits for the CURLOPT_WS_OPTIONS bitmask: */
#define CURLWS_RAW_MODE   (1L << 0)
#define CURLWS_NOAUTOPONG (1L << 1)

CURL_EXTERN const struct curl_ws_frame *curl_ws_meta(CURL *curl);

#ifdef __cplusplus
}
#endif

#endif /* CURLINC_WEBSOCKETS_H */
