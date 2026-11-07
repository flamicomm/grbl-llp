/*
  llp_transport.h — LLP v4 (wire format 4.0.0) over UART for Grbl

  The framing layer lives in src/llp_v4.h (vendored copy of the llp-protocol
  library). This module adapts it to grbl:

    - RX: the UART interrupt feeds the parser; complete frames are processed
      byte by byte (realtime commands) or pushed into grbl's line buffer.
    - TX: a line buffer is framed and streamed straight into the UART ring,
      reserving the worst case first (all-or-nothing, no partial frames).
    - Time: Timer2 provides the 1 ms tick used for LLP timeouts.
*/

#ifndef llp_transport_h
#define llp_transport_h

#include "llp_v4.h"

/* One grbl line is at most LINE_BUFFER_SIZE (48) bytes, so a line maps to
 * exactly one frame in practice. */
#define LLP_TX_LINE_BUF_SIZE 48
#define LLP_TX_CHUNK_SIZE LLP_TX_LINE_BUF_SIZE

/* Channel used for the grbl command/report stream. */
#define LLP_TX_CHANNEL 0

/* Largest frame accepted from the host (payload capacity). */
#define LLP_RX_PAYLOAD_SIZE 64

/* Inter-byte timeout for partially received frames. */
#define LLP_RX_TIMEOUT_MS 2000

typedef struct {
    uint16_t rx_frames;
    uint16_t rx_errors;
    uint16_t rx_timeouts;
    uint16_t rx_dropped;
    uint16_t tx_dropped;
    uint16_t rx_raw_bytes;
} llp_stats_t;

void llp_transport_init(void);
void llp_transport_rx_byte(uint8_t byte);
void llp_transport_tx_byte(uint8_t byte);
void llp_transport_tx_flush(void);
void llp_transport_tx_reset(void);
void llp_transport_get_stats(llp_stats_t *stats);
void llp_transport_reset_stats(void);
unsigned long llp_transport_get_ms(void);
uint8_t llp_transport_get_tx_line_idx(void);

#endif
