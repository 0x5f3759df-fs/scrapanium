/* Private bridge API: one operation owns the socket until disposed. Calls on
 * an operation, including disposal, must stay on the thread that started it. */
#ifndef SCRAPANIUM_WS_ASYNC_H
#define SCRAPANIUM_WS_ASYNC_H
#include "scrapanium.h"
typedef struct sp_ws_io sp_ws_io;
enum { SP_WS_PENDING = -1, SP_WS_SEND = 1, SP_WS_RECEIVE = 2, SP_WS_CLOSE = 3 };
sp_ws_io *sp_ws_io_start(sp_ws *, unsigned operation, unsigned kind,
  const unsigned char *, size_t, uint32_t timeout, sp_cancel *, int *error);
int sp_ws_io_step(sp_ws_io *, unsigned *kind, unsigned char **data, size_t *size);
int sp_ws_io_poll(sp_ws_io *, int *fd, short *events, uint64_t *wake_ms);
void sp_ws_io_free(sp_ws_io *);
#endif
