#include "scrapanium.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
  assert(argc == 2);
  sp_config c; sp_config_default(&c); c.concurrency = 8;
  int error;
  for (int round = 0; round < 12; round++) {
    sp_session *s = sp_session_new(&c, &error); assert(s && !error);
    sp_request qs[64]; sp_response *rs[64];
    for (int i = 0; i < 64; i++)
      qs[i] = (sp_request){.method = "GET", .url = i % 9 ? argv[1] : "file:///no"};
    assert(!sp_session_batch(s, qs, 64, rs));
    for (int i = 0; i < 64; i++) {
      assert(sp_response_error(rs[i]) == (i % 9 ? SP_OK : SP_INVALID));
      if (i % 9) assert(sp_response_size(rs[i]) == 2);
      sp_response_free(rs[i]);
    }
    for (int byte = 1; byte < 256; byte++) {
      char header[] = {'X', '-', 'F', ':', ' ', (char)byte, 0};
      const char *headers[] = {header};
      sp_request q = {.method = "GET", .url = argv[1], .headers = headers, .header_count = 1};
      sp_response *r = sp_session_request(s, &q); assert(r);
      if ((byte < 32 && byte != 9) || byte == 127) assert(sp_response_error(r) == SP_INVALID);
      sp_response_free(r);
    }
    sp_session_free(s);
  }
  /* Failure after partially initialized sessions must free every resource. */
  for (int i = 0; i < 100; i++) {
    c.profile = "does-not-exist";
    assert(!sp_session_new(&c, &error) && error == SP_PROFILE);
  }
  puts("stress: passed"); return 0;
}
