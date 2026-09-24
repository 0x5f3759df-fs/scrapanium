#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

typedef void *(*copy_fn)(void *, const void *, size_t);

typedef struct {
  unsigned char *mapping;
  unsigned char *bytes;
  size_t mapping_size;
  size_t bytes_size;
} arena;

static int fail(const char *message) {
  fprintf(stderr, "FAIL: %s\n", message);
  return 1;
}

static arena arena_new(size_t minimum) {
  arena result = {0};
  long page_value = sysconf(_SC_PAGESIZE);
  if (page_value <= 0) return result;
  size_t page = (size_t)page_value;
  size_t body = ((minimum + page - 1) / page) * page;
  if (body == 0) body = page;
  result.mapping_size = body + 2 * page;
  result.mapping = mmap(NULL, result.mapping_size, PROT_NONE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (result.mapping == MAP_FAILED) {
    result.mapping = NULL;
    return result;
  }
  result.bytes = result.mapping + page;
  result.bytes_size = body;
  if (mprotect(result.bytes, body, PROT_READ | PROT_WRITE) != 0) {
    munmap(result.mapping, result.mapping_size);
    result.mapping = NULL;
    result.bytes = NULL;
    return result;
  }
  return result;
}

static void arena_free(arena *value) {
  if (value->mapping) munmap(value->mapping, value->mapping_size);
  value->mapping = NULL;
}

static unsigned char source_byte(size_t index, size_t length, unsigned seed) {
  return (unsigned char)((index * 131u + (index >> 3) * 17u +
                          length * 3u + seed * 29u) & 0xffu);
}

static int check_outside_destination(const unsigned char *region,
                                    size_t region_size, size_t offset,
                                    size_t length, unsigned char canary) {
  for (size_t i = 0; i < region_size; ++i) {
    if (i >= offset && i - offset < length) continue;
    if (region[i] != canary) return 0;
  }
  return 1;
}

static int regular_case(copy_fn copy, arena *source, arena *destination,
                        size_t length, size_t source_align,
                        size_t destination_align, size_t case_number) {
  const size_t pad = 128;
  const unsigned char canary = 0xa7;
  if (length + pad * 2 + 64 >= source->bytes_size ||
      length + pad * 2 + 64 >= destination->bytes_size)
    return fail("regular case exceeds arena");

  size_t source_offset = pad + source_align;
  size_t destination_offset = pad + destination_align;
  unsigned char *src = source->bytes + source_offset;
  unsigned char *dst = destination->bytes + destination_offset;
  for (size_t i = 0; i < destination->bytes_size; ++i)
    destination->bytes[i] = canary;
  unsigned char *expected = malloc(length ? length : 1);
  if (!expected) return fail("expected-byte allocation failed");

  for (size_t i = 0; i < length; ++i) {
    src[i] = source_byte(i, length, (unsigned)case_number);
    expected[i] = src[i];
  }
  if (mprotect(source->bytes, source->bytes_size, PROT_READ) != 0) {
    free(expected);
    return fail("could not make source read-only");
  }

  void *returned = copy(dst, src, length);

  if (mprotect(source->bytes, source->bytes_size, PROT_READ | PROT_WRITE) != 0) {
    free(expected);
    return fail("could not restore source arena");
  }
  if (returned != dst) {
    free(expected);
    return fail("copy did not return destination");
  }
  for (size_t i = 0; i < length; ++i) {
    if (dst[i] != expected[i]) {
      free(expected);
      return fail("copied bytes differ from source");
    }
    if (src[i] != expected[i]) {
      free(expected);
      return fail("source bytes changed");
    }
  }
  if (!check_outside_destination(destination->bytes, destination->bytes_size,
                                 destination_offset, length, canary)) {
    free(expected);
    return fail("destination canary changed outside requested range");
  }
  free(expected);
  return 0;
}

static int guarded_edge_case(copy_fn copy, size_t length, int source_at_end,
                             int destination_at_end, size_t case_number) {
  long page_value = sysconf(_SC_PAGESIZE);
  if (page_value <= 0) return fail("invalid guarded-edge page size");
  size_t page = (size_t)page_value;
  arena source = arena_new(length + page);
  arena destination = arena_new(length + page);
  if (!source.mapping || !destination.mapping) {
    arena_free(&source);
    arena_free(&destination);
    return fail("guarded-edge mmap failed");
  }

  for (size_t i = 0; i < source.bytes_size; ++i)
    source.bytes[i] = (unsigned char)(0x53u ^ (unsigned char)(i * 11u));
  for (size_t i = 0; i < destination.bytes_size; ++i)
    destination.bytes[i] = 0xd3;

  size_t source_offset = source_at_end ? source.bytes_size - length : 0;
  size_t destination_offset = destination_at_end
      ? destination.bytes_size - length : 0;
  unsigned char *src = source.bytes + source_offset;
  unsigned char *dst = destination.bytes + destination_offset;
  unsigned char *expected = malloc(length ? length : 1);
  if (!expected) {
    arena_free(&source);
    arena_free(&destination);
    return fail("edge expected-byte allocation failed");
  }
  for (size_t i = 0; i < length; ++i) {
    src[i] = source_byte(i, length, (unsigned)case_number);
    expected[i] = src[i];
  }
  if (mprotect(source.bytes, source.bytes_size, PROT_READ) != 0) {
    free(expected);
    arena_free(&source);
    arena_free(&destination);
    return fail("could not protect edge source");
  }

  void *returned = copy(dst, src, length);

  if (mprotect(source.bytes, source.bytes_size, PROT_READ | PROT_WRITE) != 0) {
    free(expected);
    arena_free(&source);
    arena_free(&destination);
    return fail("could not restore edge source");
  }
  if (returned != dst) {
    free(expected);
    arena_free(&source);
    arena_free(&destination);
    return fail("edge copy did not return destination");
  }
  for (size_t i = 0; i < length; ++i) {
    if (dst[i] != expected[i] || src[i] != expected[i]) {
      free(expected);
      arena_free(&source);
      arena_free(&destination);
      return fail("edge payload mismatch or source changed");
    }
  }
  if (!check_outside_destination(destination.bytes, destination.bytes_size,
                                 destination_offset, length, 0xd3)) {
    free(expected);
    arena_free(&source);
    arena_free(&destination);
    return fail("edge destination canary changed");
  }
  free(expected);
  arena_free(&source);
  arena_free(&destination);
  return 0;
}

static const char *basename_of(const char *path) {
  const char *slash = strrchr(path, '/');
  return slash ? slash + 1 : path;
}

static int path_matches(const char *requested, const char *actual) {
  char requested_real[4096], actual_real[4096];
  if (realpath(requested, requested_real) && realpath(actual, actual_real))
    return strcmp(requested_real, actual_real) == 0;
  return strcmp(basename_of(requested), basename_of(actual)) == 0;
}

int main(int argc, char **argv) {
  if (argc < 3 || argc > 4) {
    fprintf(stderr, "usage: %s DSO EXPORTED_TEST_SYMBOL [all|regular|edges|edge-dst-end|edge-src-start]\n", argv[0]);
    return 2;
  }
  const char *mode = argc == 4 ? argv[3] : "all";
  if (strcmp(mode, "all") && strcmp(mode, "regular") && strcmp(mode, "edges") &&
      strcmp(mode, "edge-dst-end") && strcmp(mode, "edge-src-start")) {
    fprintf(stderr, "unknown test mode: %s\n", mode);
    return 2;
  }

  void *handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (!handle) {
    fprintf(stderr, "dlopen: %s\n", dlerror());
    return 2;
  }
  dlerror();
  void *raw = dlsym(handle, argv[2]);
  const char *symbol_error = dlerror();
  if (symbol_error || !raw) {
    fprintf(stderr, "dlsym(%s): %s\n", argv[2],
            symbol_error ? symbol_error : "null symbol");
    dlclose(handle);
    return 2;
  }
  copy_fn copy = (copy_fn)raw;
  Dl_info info;
  if (!dladdr(raw, &info) || !info.dli_fname ||
      !path_matches(argv[1], info.dli_fname)) {
    fprintf(stderr, "test symbol did not resolve inside requested DSO\n");
    dlclose(handle);
    return 2;
  }
  printf("test_symbol=%s\nresolved_dso=%s\nmode=%s\n",
         argv[2], info.dli_fname, mode);
  fflush(stdout);

  const size_t lengths[] = {
    0, 1, 2, 3, 7, 8, 15, 16, 17, 31, 32, 63, 64, 65,
    127, 128, 129, 255, 256, 257, 1023, 1024, 1025,
    16383, 16384, 16385, 65535, 65536, 65537,
    131071, 131072, 131073, 1048575, 1048576, 1048577
  };
  const size_t selected_alignments[] = {0, 1, 7, 8, 15, 16, 31, 32, 63};
  size_t cases = 0;
  if (!strcmp(mode, "all") || !strcmp(mode, "regular")) {
    for (size_t li = 0; li < sizeof(lengths) / sizeof(lengths[0]); ++li) {
      size_t length = lengths[li];
      arena source = arena_new(length + 256);
      arena destination = arena_new(length + 256);
      if (!source.mapping || !destination.mapping) {
        arena_free(&source);
        arena_free(&destination);
        dlclose(handle);
        return fail("regular mmap failed");
      }
      if (length <= 257) {
        for (size_t sa = 0; sa < 64; ++sa) {
          for (size_t da = 0; da < 64; ++da) {
            if (regular_case(copy, &source, &destination, length, sa, da,
                             cases++) != 0) {
              arena_free(&source);
              arena_free(&destination);
              dlclose(handle);
              return 1;
            }
          }
        }
      } else {
        for (size_t sa = 0; sa < sizeof(selected_alignments) /
                                sizeof(selected_alignments[0]); ++sa) {
          for (size_t da = 0; da < sizeof(selected_alignments) /
                                  sizeof(selected_alignments[0]); ++da) {
            if (regular_case(copy, &source, &destination, length,
                             selected_alignments[sa], selected_alignments[da],
                             cases++) != 0) {
              arena_free(&source);
              arena_free(&destination);
              dlclose(handle);
              return 1;
            }
          }
        }
      }
      arena_free(&source);
      arena_free(&destination);
    }
  }

  const size_t edge_lengths[] = {
    1, 15, 16, 17, 16383, 16384, 16385, 65535, 65536, 65537,
    131071, 131072, 131073, 1048575, 1048576, 1048577
  };
  size_t edge_cases = 0;
  if (strcmp(mode, "all") && strcmp(mode, "edges") &&
      strcmp(mode, "edge-dst-end") && strcmp(mode, "edge-src-start")) {
    printf("PASS: %zu alignment/size cases\n", cases);
    dlclose(handle);
    return 0;
  }
  for (size_t i = 0; i < sizeof(edge_lengths) / sizeof(edge_lengths[0]); ++i) {
    size_t length = edge_lengths[i];
    for (int source_end = 0; source_end <= 1; ++source_end) {
      for (int destination_end = 0; destination_end <= 1; ++destination_end) {
        if (!strcmp(mode, "edge-dst-end") && destination_end != 1) continue;
        if (!strcmp(mode, "edge-src-start") && source_end != 0) continue;
        if (guarded_edge_case(copy, length, source_end, destination_end,
                              edge_cases++) != 0) {
          dlclose(handle);
          return 1;
        }
      }
    }
  }

  printf("PASS: %zu alignment/size cases, %zu guarded-edge cases; "
         "source was read-only during calls\n", cases, edge_cases);
  dlclose(handle);
  return 0;
}