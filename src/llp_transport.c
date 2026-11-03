/*
  llp_transport.c — LLP over UART transport layer for Grbl
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

#include "grbl.h"

static llp_parser_t llp_rx_parser;

static uint8_t llp_tx_line_buf[LLP_TX_LINE_BUF_SIZE];
static volatile uint8_t llp_tx_line_idx = 0;

static volatile unsigned long llp_ms_counter = 0;

static volatile uint16_t llp_rx_frame_count = 0;
static volatile uint16_t llp_rx_error_count = 0;
static volatile uint16_t llp_rx_timeout_count = 0;
static volatile uint16_t llp_rx_dropped_count = 0;
static volatile uint16_t llp_tx_dropped_count = 0;
static volatile uint16_t llp_rx_raw_bytes = 0;

// Static buffers for LLP frame construction (avoid large stack allocations that
// risk overflowing into BSS on RAM-constrained ATmega328P).
// llp_rx_byte() runs in ISR context; llp_transport_tx_flush() in main context.
// Note: RX uses llp_get_final_payload_ptr() for zero-copy reads directly from
// the parser's frame buffer, so no separate RX payload buffer is needed.
// TX streams the frame straight into the UART ring (see llp_transport_tx_flush),
// so there is no frame buffer either.

// Maximum time to wait for room in the TX ring before dropping a frame. The
// ring drains at ~86 us/byte (115200 baud), so a full 128-byte ring clears in
// ~11 ms; 10 ms covers all but the pathological case.
#define LLP_TX_WAIT_MS 10u

// Worst case for one line: magic (2, never stuffed) + stuffed LEN(2) + stuffed
// payload (FinalNode byte + line) + stuffed CRC(2).
#define LLP_TX_FRAME_MAX(line_len) \
    (2u + 2u * ((uint16_t)(line_len) + 1u + 4u))

// The worst-case frame must fit the ring; otherwise an all-or-nothing flush
// would never succeed. Enforced at compile time.
#define LLP_TX_RING_FREE_MAX ((uint16_t)(TX_RING_BUFFER - 1))
typedef char llp_tx_frame_fits_ring
    [(LLP_TX_FRAME_MAX(LLP_TX_LINE_BUF_SIZE) <= LLP_TX_RING_FREE_MAX) ? 1 : -1];

// Free bytes in the TX ring. `head` is only written from this (main) context;
// the UDRE ISR only advances `tail`, which can only increase the result.
static uint16_t llp_tx_ring_free(void)
{
    uint8_t head = serial_tx_buffer_head;
    uint8_t tail = serial_tx_buffer_tail;
    if (tail > head) {
        return (uint16_t)(tail - head - 1u);
    }
    return (uint16_t)((uint16_t)TX_RING_BUFFER - head + tail - 1u);
}

// Push one byte into the ring. Space was reserved beforehand, so no check.
static void llp_tx_push_raw(uint8_t byte)
{
    uint8_t next = serial_tx_buffer_head + 1u;
    if (next == TX_RING_BUFFER) { next = 0; }
    serial_tx_buffer[serial_tx_buffer_head] = byte;
    serial_tx_buffer_head = next;
}

// Emit a byte applying LLP byte stuffing.
static void llp_tx_push_stuffed(uint8_t byte)
{
    if (byte == LLP_MAGIC_1) {
        llp_tx_push_raw(LLP_MAGIC_1);
        llp_tx_push_raw(0x00u);
    } else {
        llp_tx_push_raw(byte);
    }
}

void llp_transport_init(void)
{
    llp_parser_init(&llp_rx_parser);
    llp_tx_line_idx = 0;

    TCCR2A = 0;
    TCCR2B = (1 << CS22);  // 1/64 prescaler -> 4us per tick at 16MHz
    TIMSK2 |= (1 << TOIE2);
}

ISR(TIMER2_OVF_vect)
{
    llp_ms_counter++;
}

static void llp_rx_buffer_write(uint8_t data)
{
    uint8_t head = serial_rx_buffer_head;
    uint8_t next_head = head + 1;
    if (next_head == RX_RING_BUFFER) { next_head = 0; }
    if (next_head != serial_rx_buffer_tail) {
        serial_rx_buffer[head] = data;
        serial_rx_buffer_head = next_head;
    } else {
        llp_rx_dropped_count++;
    }
}

static void llp_process_payload(const uint8_t *payload, uint16_t len)
{
    for (uint16_t i = 0; i < len; i++) {
        uint8_t c = payload[i];
        switch (c) {
            case CMD_RESET:
                system_set_reset_source(RESET_SRC_LLP);
                mc_reset();
                break;
            case CMD_STATUS_REPORT:
                system_set_exec_state_flag(EXEC_STATUS_REPORT);
                break;
            case CMD_CYCLE_START:
                system_set_exec_state_flag(EXEC_CYCLE_START);
                break;
            case CMD_FEED_HOLD:
                system_set_exec_state_flag(EXEC_FEED_HOLD);
                break;
            default:
                if (c > 0x7F) {
                    switch (c) {
                        case CMD_SAFETY_DOOR:
                            system_set_exec_state_flag(EXEC_SAFETY_DOOR);
                            break;
                        case CMD_JOG_CANCEL:
                            if (sys.state & STATE_JOG) {
                                system_set_exec_state_flag(EXEC_MOTION_CANCEL);
                            }
                            break;
                        #ifdef DEBUG
                            case CMD_DEBUG_REPORT: {
                                uint8_t sreg = SREG;
                                cli();
                                bit_true(sys_rt_exec_debug, EXEC_DEBUG_REPORT);
                                SREG = sreg;
                            } break;
                        #endif
                        case CMD_FEED_OVR_RESET:
                            system_set_exec_motion_override_flag(EXEC_FEED_OVR_RESET);
                            break;
                        case CMD_FEED_OVR_COARSE_PLUS:
                            system_set_exec_motion_override_flag(EXEC_FEED_OVR_COARSE_PLUS);
                            break;
                        case CMD_FEED_OVR_COARSE_MINUS:
                            system_set_exec_motion_override_flag(EXEC_FEED_OVR_COARSE_MINUS);
                            break;
                        case CMD_FEED_OVR_FINE_PLUS:
                            system_set_exec_motion_override_flag(EXEC_FEED_OVR_FINE_PLUS);
                            break;
                        case CMD_FEED_OVR_FINE_MINUS:
                            system_set_exec_motion_override_flag(EXEC_FEED_OVR_FINE_MINUS);
                            break;
                        case CMD_RAPID_OVR_RESET:
                            system_set_exec_motion_override_flag(EXEC_RAPID_OVR_RESET);
                            break;
                        case CMD_RAPID_OVR_MEDIUM:
                            system_set_exec_motion_override_flag(EXEC_RAPID_OVR_MEDIUM);
                            break;
                        case CMD_RAPID_OVR_LOW:
                            system_set_exec_motion_override_flag(EXEC_RAPID_OVR_LOW);
                            break;
                        case CMD_SPINDLE_OVR_RESET:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_RESET);
                            break;
                        case CMD_SPINDLE_OVR_COARSE_PLUS:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_COARSE_PLUS);
                            break;
                        case CMD_SPINDLE_OVR_COARSE_MINUS:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_COARSE_MINUS);
                            break;
                        case CMD_SPINDLE_OVR_FINE_PLUS:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_FINE_PLUS);
                            break;
                        case CMD_SPINDLE_OVR_FINE_MINUS:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_FINE_MINUS);
                            break;
                        case CMD_SPINDLE_OVR_STOP:
                            system_set_exec_accessory_override_flag(EXEC_SPINDLE_OVR_STOP);
                            break;
                        case CMD_COOLANT_FLOOD_OVR_TOGGLE:
                            system_set_exec_accessory_override_flag(EXEC_COOLANT_FLOOD_OVR_TOGGLE);
                            break;
                        #ifdef ENABLE_M7
                            case CMD_COOLANT_MIST_OVR_TOGGLE:
                                system_set_exec_accessory_override_flag(EXEC_COOLANT_MIST_OVR_TOGGLE);
                                break;
                        #endif
                    }
                } else {
                    llp_rx_buffer_write(c);
                }
                break;
        }
    }
}

void llp_transport_rx_byte(uint8_t byte)
{
    llp_rx_raw_bytes++;
    unsigned long now = llp_ms_counter;
    int result = llp_parser_process_byte(&llp_rx_parser, byte, now);

    if (result == 1) {
        uint16_t payload_len;
        const uint8_t *payload = llp_get_final_payload_ptr(&llp_rx_parser.frame, &payload_len);
        if (payload != NULL && payload_len > 0) {
            llp_rx_frame_count++;
            llp_process_payload(payload, payload_len);
        }
    } else if (result < 0) {
        llp_rx_error_count++;
        if (llp_rx_parser.error_code == LLP_ERR_TIMEOUT) {
            llp_rx_timeout_count++;
        }
    }
}

void llp_transport_tx_byte(uint8_t byte)
{
    if (llp_tx_line_idx < LLP_TX_LINE_BUF_SIZE) {
        llp_tx_line_buf[llp_tx_line_idx++] = byte;
    }

    // Flush on newline or when the line buffer is full. The chunk size equals
    // the line buffer size, so a full buffer always flushes; the payload
    // (FinalNode + chunk) must stay <= LLP_MAX_PAYLOAD and the whole frame must
    // fit the TX ring (both checked at compile time).
    if (byte == '\n' || llp_tx_line_idx >= LLP_TX_CHUNK_SIZE) {
        llp_transport_tx_flush();
    }
}

void llp_transport_tx_flush(void)
{
    uint8_t idx = llp_tx_line_idx;
    if (idx == 0) return;
    llp_tx_line_idx = 0;

    /* Payload = FinalNode layer byte (0x00) + the line. Worst-case frame:
     * magic(2, never stuffed) + stuffed LEN(2) + stuffed payload + stuffed
     * CRC(2). The whole frame is reserved in the ring before writing, so a
     * frame is either queued completely or not at all (no partial frames and
     * no torn headers on the wire). */
    uint16_t payload_len = (uint16_t)idx + 1u;
    uint16_t needed = 2u + 2u * (payload_len + 4u);

    /* A frame that cannot ever fit the ring is dropped instead of waiting. */
    if (needed > (uint16_t)LLP_TX_RING_FREE_MAX) {
        llp_tx_dropped_count++;
        return;
    }

    /* Wait (bounded) for space; the UDRE ISR drains meanwhile. */
    uint16_t wait_start = (uint16_t)llp_transport_get_ms();
    while (llp_tx_ring_free() < needed) {
        UCSR0B |= (1 << UDRIE0);
        if ((uint16_t)((uint16_t)llp_transport_get_ms() - wait_start) >= LLP_TX_WAIT_MS) {
            llp_tx_dropped_count++;
            return;
        }
    }

    /* CRC over MAGIC + LEN + PAYLOAD (unescaped), as defined by the protocol. */
    uint8_t len_lo = (uint8_t)(payload_len & 0xFFu);
    uint8_t len_hi = (uint8_t)(payload_len >> 8);
    uint16_t crc = 0xFFFFu;
    crc = llp_crc16_update(crc, LLP_MAGIC_1);
    crc = llp_crc16_update(crc, LLP_MAGIC_2);
    crc = llp_crc16_update(crc, len_lo);
    crc = llp_crc16_update(crc, len_hi);
    crc = llp_crc16_update(crc, LLP_LAYER_ID_FINAL);
    for (uint8_t i = 0; i < idx; i++) {
        crc = llp_crc16_update(crc, llp_tx_line_buf[i]);
    }

    llp_tx_push_raw(LLP_MAGIC_1);
    llp_tx_push_raw(LLP_MAGIC_2);
    llp_tx_push_stuffed(len_lo);
    llp_tx_push_stuffed(len_hi);
    llp_tx_push_stuffed(LLP_LAYER_ID_FINAL);
    for (uint8_t i = 0; i < idx; i++) {
        llp_tx_push_stuffed(llp_tx_line_buf[i]);
    }
    llp_tx_push_stuffed((uint8_t)(crc & 0xFFu));
    llp_tx_push_stuffed((uint8_t)(crc >> 8));

    UCSR0B |= (1 << UDRIE0);
}

void llp_transport_tx_reset(void)
{
    llp_tx_line_idx = 0;
}

void llp_transport_get_stats(llp_stats_t *stats)
{
    uint8_t sreg = SREG;
    cli();
    stats->rx_frames = llp_rx_frame_count;
    stats->rx_errors = llp_rx_error_count;
    stats->rx_timeouts = llp_rx_timeout_count;
    stats->rx_dropped = llp_rx_dropped_count;
    stats->tx_dropped = llp_tx_dropped_count;
    stats->rx_raw_bytes = llp_rx_raw_bytes;
    SREG = sreg;
}

void llp_transport_reset_stats(void)
{
    uint8_t sreg = SREG;
    cli();
    llp_rx_frame_count = 0;
    llp_rx_error_count = 0;
    llp_rx_timeout_count = 0;
    llp_rx_dropped_count = 0;
    llp_tx_dropped_count = 0;
    llp_rx_raw_bytes = 0;
    SREG = sreg;
}

unsigned long llp_transport_get_ms(void)
{
    unsigned long ms;
    uint8_t sreg = SREG;
    cli();
    ms = llp_ms_counter;
    SREG = sreg;
    return ms;
}

uint8_t llp_transport_get_tx_line_idx(void)
{
    uint8_t idx;
    uint8_t sreg = SREG;
    cli();
    idx = llp_tx_line_idx;
    SREG = sreg;
    return idx;
}