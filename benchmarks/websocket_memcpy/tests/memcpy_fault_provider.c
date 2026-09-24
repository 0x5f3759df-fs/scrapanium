#define _GNU_SOURCE
#include <stddef.h>
#include <stdio.h>

#ifndef FAULT_KIND
#define FAULT_KIND 0
#endif

extern void *sp_glibc_memcpy(void *destination, const void *source, size_t length);
__asm__(".symver sp_glibc_memcpy,memcpy@GLIBC_2.14");

__attribute__((visibility("default"), noinline))
void *sp_wss_memcpy_test(void *destination, const void *source, size_t length) {
  static int announced;
  if (!announced) {
    fprintf(stderr, "fault_provider_kind=%d\n", FAULT_KIND);
    announced = 1;
  }
#if FAULT_KIND == 5
  volatile unsigned char before =
      ((const volatile unsigned char *)source)[-1];
  (void)before;
#endif
  void *result = sp_glibc_memcpy(destination, source, length);
#if FAULT_KIND == 1
  if (length) ((volatile unsigned char *)destination)[length / 2] ^= 0x01;
#elif FAULT_KIND == 2
  return (unsigned char *)result + 1;
#elif FAULT_KIND == 3
  ((volatile unsigned char *)destination)[length + 128] = 0x5c;
#elif FAULT_KIND == 4
  if (length) ((volatile unsigned char *)source)[0] ^= 0x80;
#elif FAULT_KIND == 6
  ((volatile unsigned char *)destination)[length] = 0x5c;
#endif
  return result;
}