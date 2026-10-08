#include "curl_setup.h"
#include "bufq.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void *test_calloc(size_t count, size_t size) { return calloc(count, size); }
static void test_free(void *ptr) { free(ptr); }
curl_calloc_callback Curl_ccalloc = test_calloc;
curl_free_callback Curl_cfree = test_free;

#define CHECK(condition) do { \
  if(!(condition)) { \
    fprintf(stderr, "check failed at %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    return 1; \
  } \
} while(0)

static void *release_pin(void *argument)
{
  Curl_bufq_pin_release((struct bufq_pin *)argument);
  return NULL;
}

static int test_nonpooled_queue(void)
{
  struct bufq queue;
  struct bufq_pin *pin = NULL, *duplicate = NULL;
  uint8_t input[48], replacement[32];
  size_t written = 0;

  for(size_t i = 0; i < sizeof(input); i++)
    input[i] = (uint8_t)(0xD3u ^ (uint8_t)(i * 29u));
  memset(replacement, 0x5A, sizeof(replacement));
  Curl_bufq_init2(&queue, 16, 4, BUFQ_OPT_NONE);
  CHECK(Curl_bufq_enable_pins(&queue));
  CHECK(Curl_bufq_write(&queue, input, sizeof(input), &written) == CURLE_OK);
  CHECK(written == sizeof(input));
  CHECK(Curl_bufq_pin_head(&queue, 16, &pin) == CURLE_OK);
  CHECK(Curl_bufq_pin_head(&queue, 16, &duplicate) == CURLE_OK);
  CHECK(Curl_bufq_pin_data(pin) == Curl_bufq_pin_data(duplicate));
  CHECK(!memcmp(Curl_bufq_pin_data(pin), input, 16));

  Curl_bufq_skip(&queue, 16);
  Curl_bufq_reset(&queue);
  CHECK(!memcmp(Curl_bufq_pin_data(pin), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(duplicate), input, 16));
  Curl_bufq_free(&queue);

  /* A new unpooled queue cannot recycle pinned storage from the old queue. */
  struct bufq next;
  Curl_bufq_init2(&next, 16, 2, BUFQ_OPT_NONE);
  CHECK(Curl_bufq_write(&next, replacement, sizeof(replacement), &written) == CURLE_OK);
  CHECK(written == sizeof(replacement));
  CHECK(!memcmp(Curl_bufq_pin_data(pin), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(duplicate), input, 16));
  Curl_bufq_free(&next);

  Curl_bufq_pin_release(duplicate);
  CHECK(!memcmp(Curl_bufq_pin_data(pin), input, 16));
  Curl_bufq_pin_release(pin);
  return 0;
}

int main(void)
{
  struct bufc_pool pool;
  struct bufq queue;
  struct bufq_pin *first = NULL, *first_again = NULL, *second = NULL;
  uint8_t input[48], replacement[32], copy[16];
  size_t written = 0, read = 0;
  pthread_t releaser;

  for(size_t i = 0; i < sizeof(input); i++)
    input[i] = (uint8_t)(i * 17u + 3u);
  memset(replacement, 0xA5, sizeof(replacement));
  Curl_bufcp_init(&pool, 16, 4);
  Curl_bufq_initp(&queue, &pool, 4, BUFQ_OPT_NONE);
  CHECK(Curl_bufq_enable_pins(&queue));
  CHECK(Curl_bufq_write(&queue, input, sizeof(input), &written) == CURLE_OK);
  CHECK(written == sizeof(input));
  CHECK(Curl_bufq_pin_head(&queue, 16, &first) == CURLE_OK);
  CHECK(Curl_bufq_pin_head(&queue, 16, &first_again) == CURLE_OK);
  CHECK(Curl_bufq_pin_data(first) == Curl_bufq_pin_data(first_again));
  CHECK(Curl_bufq_pin_len(first) == 16);
  CHECK(Curl_bufq_pin_allocated(first) >= Curl_bufq_pin_len(first));
  CHECK(Curl_bufq_pin_reservation(&queue) >= Curl_bufq_pin_allocated(first));

  Curl_bufq_skip(&queue, 16);
  CHECK(Curl_bufq_pin_head(&queue, 16, &second) == CURLE_OK);
  CHECK(!memcmp(Curl_bufq_pin_data(first), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(second), input + 16, 16));

  /* Reset releases queue ownership while both first- and second-chunk pins remain. */
  Curl_bufq_reset(&queue);
  CHECK(!memcmp(Curl_bufq_pin_data(first), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(first_again), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(second), input + 16, 16));
  Curl_bufq_free(&queue);
  Curl_bufcp_free(&pool);

  /* A fresh queue/pool can write without touching any pinned former chunks. */
  struct bufc_pool next_pool;
  struct bufq next_queue;
  Curl_bufcp_init(&next_pool, 16, 2);
  Curl_bufq_initp(&next_queue, &next_pool, 2, BUFQ_OPT_NONE);
  CHECK(Curl_bufq_write(&next_queue, replacement, sizeof(replacement), &written) == CURLE_OK);
  CHECK(written == sizeof(replacement));
  CHECK(!memcmp(Curl_bufq_pin_data(first), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(first_again), input, 16));
  CHECK(!memcmp(Curl_bufq_pin_data(second), input + 16, 16));
  CHECK(Curl_bufq_read(&next_queue, copy, sizeof(copy), &read) == CURLE_OK);
  CHECK(read == sizeof(copy) && !memcmp(copy, replacement, sizeof(copy)));
  Curl_bufq_free(&next_queue);
  Curl_bufcp_free(&next_pool);

  CHECK(pthread_create(&releaser, NULL, release_pin, first_again) == 0);
  pthread_join(releaser, NULL);
  CHECK(!memcmp(Curl_bufq_pin_data(first), input, 16));
  Curl_bufq_pin_release(first);
  CHECK(!memcmp(Curl_bufq_pin_data(second), input + 16, 16));
  CHECK(pthread_create(&releaser, NULL, release_pin, second) == 0);
  pthread_join(releaser, NULL);
  CHECK(test_nonpooled_queue() == 0);
  puts("patched-bufq-pins-asan-ubsan-ok");
  return 0;
}
