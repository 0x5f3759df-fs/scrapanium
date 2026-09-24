#include <stddef.h>

/* Explicitly bind this one path to the oldest supported glibc memcpy ABI. */
extern void *sp_wss_memcpy_libc(void *, const void *, size_t);
__asm__(".symver sp_wss_memcpy_libc,memcpy@GLIBC_2.14");

#if defined(__GNUC__)
#define SP_EXPORT __attribute__((visibility("default")))
#define SP_NOINLINE __attribute__((noinline))
#else
#define SP_EXPORT
#define SP_NOINLINE
#endif

/* Exported so an independent dlopen fixture can exercise this exact body. */
SP_EXPORT SP_NOINLINE
void *sp_wss_memcpy_test(void *dst, const void *src, size_t size)
{
  return sp_wss_memcpy_libc(dst, src, size);
}

/* A strong hidden definition redirects ordinary intra-DSO calls to the same
 * body without a forwarding function or an interposable exported memcpy. */
extern __typeof(sp_wss_memcpy_test) memcpy
  __attribute__((alias("sp_wss_memcpy_test"), visibility("hidden")));
