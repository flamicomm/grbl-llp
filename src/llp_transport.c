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

static void llp_process_payload(uint8_t *payload, uint16_t len)
{
    for (uint16_t i = 0; i < len; i++) {
        uint8_t c = payload[i];
        switch (c) {
            case CMD_RESET:
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
        uint8_t payload_buf[LLP_MAX_PAYLOAD];
        int payload_len = llp_get_final_payload(&llp_rx_parser.frame,
                                                 payload_buf, sizeof(payload_buf));
        if (payload_len > 0) {
            llp_rx_frame_count++;
            llp_process_payload(payload_buf, (uint16_t)payload_len);
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

    if (byte == '\n' || llp_tx_line_idx >= LLP_TX_LINE_BUF_SIZE) {
        llp_transport_tx_flush();
    }
}

void llp_transport_tx_flush(void)
{
    uint8_t idx = llp_tx_line_idx;
    if (idx == 0) return;
    llp_tx_line_idx = 0;

    uint8_t payload_buf[LLP_MAX_PAYLOAD];
    size_t payload_len = llp_build_final_payload(payload_buf, sizeof(payload_buf),
                                                   llp_tx_line_buf, (uint16_t)idx);
    if (payload_len == 0) return;

    uint8_t frame_buf[LLP_MAX_FRAME_SIZE(LLP_MAX_PAYLOAD)];
    size_t frame_len = llp_build_frame(frame_buf, sizeof(frame_buf),
                                         payload_buf, (uint16_t)payload_len);
    if (frame_len == 0) return;

    for (size_t i = 0; i < frame_len; i++) {
        uint8_t next_head = serial_tx_buffer_head + 1;
        if (next_head == TX_RING_BUFFER) { next_head = 0; }
        if (next_head == serial_tx_buffer_tail) {
            // TX buffer full - discard remaining bytes rather than blocking.
            // This prevents the main loop from stalling, which would cause
            // incoming serial data to be lost. Partial frames will be
            // discarded by the receiving LLP parser via CRC check.
            llp_tx_dropped_count++;
            UCSR0B |= (1 << UDRIE0);
            return;
        }
        serial_tx_buffer[serial_tx_buffer_head] = frame_buf[i];
        serial_tx_buffer_head = next_head;
    }

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