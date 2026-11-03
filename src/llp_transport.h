/*
  llp_transport.h — LLP over UART transport layer for Grbl
  Part of Grbl-LLP

  Copyright (c) 2024

  Grbl-LLP is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  Grbl-LLP is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with Grbl-LLP.  If not, see <http://www.gnu.org/licenses/>.
*/

#ifndef llp_transport_h
#define llp_transport_h

#include "llp_protocol.h"

#define LLP_TX_LINE_BUF_SIZE  48

// A line is flushed to a frame every LLP_TX_CHUNK_SIZE bytes. The whole
// worst-case frame must fit in the TX ring, because llp_transport_tx_flush()
// reserves it before writing anything (all-or-nothing, no partial frames).
// grbl's own LINE_BUFFER_SIZE is 48, so in practice every line is one frame.
#define LLP_TX_CHUNK_SIZE     LLP_TX_LINE_BUF_SIZE

// The line buffer must be able to hold a full chunk before the flush happens.
typedef char llp_tx_chunk_fits_line_buf[(LLP_TX_LINE_BUF_SIZE >= LLP_TX_CHUNK_SIZE) ? 1 : -1];

void llp_transport_init(void);
void llp_transport_rx_byte(uint8_t byte);
void llp_transport_tx_byte(uint8_t byte);
void llp_transport_tx_flush(void);
void llp_transport_tx_reset(void);

typedef struct {
    uint16_t rx_frames;
    uint16_t rx_errors;
    uint16_t rx_timeouts;
    uint16_t rx_dropped;
    uint16_t tx_dropped;
    uint16_t rx_raw_bytes;
} llp_stats_t;

void llp_transport_get_stats(llp_stats_t *stats);
void llp_transport_reset_stats(void);
unsigned long llp_transport_get_ms(void);
uint8_t llp_transport_get_tx_line_idx(void);

#endif