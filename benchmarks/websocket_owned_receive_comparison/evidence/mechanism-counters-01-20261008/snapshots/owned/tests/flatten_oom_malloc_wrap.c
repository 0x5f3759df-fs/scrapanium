#define _GNU_SOURCE
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

void *__real_malloc(size_t size);

static uintptr_t flatten_begin;
static uintptr_t flatten_end;
static size_t flatten_size;
static int fail_enabled;
static int fail_done;

static uintptr_t env_number(const char *name)
{
  const char *value = getenv(name);
  char *end = NULL;
  unsigned long long parsed;
  if(!value || !*value)
    return 0;
  parsed = strtoull(value, &end, 0);
  return (end && !*end) ? (uintptr_t)parsed : 0;
}

__attribute__((constructor))
static void configure_flatten_oom(void)
{
  flatten_begin = env_number("SCRAPANIUM_FAIL_FLATTEN_BEGIN");
  flatten_end = env_number("SCRAPANIUM_FAIL_FLATTEN_END");
  flatten_size = (size_t)env_number("SCRAPANIUM_FAIL_FLATTEN_SIZE");
  fail_enabled = flatten_begin && flatten_end > flatten_begin && flatten_size;
}

void *__wrap_malloc(size_t size)
{
  uintptr_t caller = (uintptr_t)__builtin_return_address(0);
  if(fail_enabled && !fail_done && size == flatten_size &&
     caller >= flatten_begin && caller < flatten_end) {
    static const char message[] = "flatten-oom-injected-at-owned-bytes-allocation\n";
    fail_done = 1;
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    return NULL;
  }
  return __real_malloc(size);
}
