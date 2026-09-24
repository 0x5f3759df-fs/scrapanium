#include <stddef.h>
#include <string.h>

#if defined(__GNUC__)
#define SP_EXPORT __attribute__((visibility("default")))
#define SP_NOINLINE __attribute__((noinline))
#else
#define SP_EXPORT
#define SP_NOINLINE
#endif

/* Unknown size keeps the operation out-of-line; this source is identical in
 * baseline and candidate builds. */
SP_EXPORT SP_NOINLINE
void *sp_memcpy_probe_callsite(void *dst, const void *src, size_t size)
{
  return memcpy(dst, src, size);
}
