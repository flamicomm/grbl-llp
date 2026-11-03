/*
  llp_transport.c — LLP v4 (wire format 4.0.0) over UART for Grbl

  Framing is done by src/llp_v4.h; this file only connects it to the UART:
  the RX interrupt feeds the parser, the main loop encodes into the TX ring.

  RAM: the v4 parser keeps ~17 bytes of state and writes the payload into
  llp_rx_payload, so there is no per-parser payload buffer. TX streams straight
  into grbl's serial ring; there is no frame buffer.
*/

#include "grbl.h"

static llp4_parser_t llp_rx_parser;
static uint8_t llp_rx_payload[LLP_RX_PAYLOAD_SIZE];

static uint8_t llp_tx_line_buf[LLP_TX_LINE_BUF_SIZE];
static volatile uint8_t llp_tx_line_idx = 0;

static volatile unsigned long llp_ms_counter = 0;

static volatile uint16_t llp_rx_frame_count = 0;
static volatile uint16_t llp_rx_error_count = 0;
static volatile uint16_t llp_rx_timeout_count = 0;
static volatile uint16_t llp_rx_dropped_count = 0;
static volatile uint16_t llp_tx_dropped_count = 0;
static volatile uint16_t llp_rx_raw_bytes = 0;

/* Maximum time to wait for room in the TX ring before dropping a frame. */
#define LLP_TX_WAIT_MS 10u

/* Must match llp4_frame_size(): the whole worst-case frame has to fit in the
 * ring because the flush reserves it before writing. Checked at compile time. */
#define LLP_TX_RING_FREE_MAX ((uint16_t)(TX_RING_BUFFER - 1))
typedef char llp_tx_frame_fits_ring
    [(LLP4_FRAME_SIZE(LLP_TX_LINE_BUF_SIZE) <= LLP_TX_RING_FREE_MAX) ? 1 : -1];

/* Free bytes in the TX ring. `head` is only written from the main loop; the
 * UDRE ISR only advances `tail`, which can only increase the result. */
static uint16_t llp_tx_ring_free(void)
{
    uint8_t head = serial_tx_buffer_head;
    uint8_t tail = serial_tx_buffer_tail;
    if (tail > head) {
        return (uint16_t)(tail - head - 1u);
    }
    return (uint16_t)((uint16_t)TX_RING_BUFFER - head + tail - 1u);
}

/* Push one byte into the ring. Space was reserved beforehand, so no check. */
static void llp_tx_push_raw(uint8_t byte)
{
    uint8_t next = serial_tx_buffer_head + 1u;
    if (next == TX_RING_BUFFER) { next = 0; }
    serial_tx_buffer[serial_tx_buffer_head] = byte;
    serial_tx_buffer_head = next;
}

/* Encoder sink: the library does stuffing and CRC, we only store bytes. */
static void llp_tx_sink(void *ctx, uint8_t byte)
{
    (void)ctx;
    llp_tx_push_raw(byte);
}

void llp_transport_init(void)
{
    llp4_parser_init(&llp_rx_parser, llp_rx_payload,
                     (uint8_t)sizeof(llp_rx_payload), LLP_RX_TIMEOUT_MS);
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
    int rc;

    llp_rx_raw_bytes++;
    /* Inside the UART ISR: interrupts are disabled, so the 32-bit counter read
     * cannot tear. The parser only needs 16 bits (wrap-safe). */
    rc = llp4_parser_feed(&llp_rx_parser, byte, (uint16_t)llp_ms_counter);

    if (rc == LLP4_EV_FRAME) {
        uint8_t len = llp_rx_parser.length;
        if (len > 0) {
            llp_rx_frame_count++;
            llp_process_payload(llp_rx_payload, len);
        }
    } else if (rc < 0) {
        llp_rx_error_count++;
        if (rc == -(int)LLP4_ERR_TIMEOUT) {
            llp_rx_timeout_count++;
        }
    }
}

void llp_transport_tx_byte(uint8_t byte)
{
    if (llp_tx_line_idx < LLP_TX_LINE_BUF_SIZE) {
        llp_tx_line_buf[llp_tx_line_idx++] = byte;
    }

    // Flush on newline or when the line buffer is full.
    if (byte == '\n' || llp_tx_line_idx >= LLP_TX_CHUNK_SIZE) {
        llp_transport_tx_flush();
    }
}

void llp_transport_tx_flush(void)
{
    uint8_t idx = llp_tx_line_idx;
    llp4_tx_t tx;
    uint16_t needed;
    uint16_t wait_start;
    uint8_t i;

    if (idx == 0) {
        return;
    }
    llp_tx_line_idx = 0;

    needed = (uint16_t)llp4_frame_size(idx);
    if (needed > LLP_TX_RING_FREE_MAX) {
        llp_tx_dropped_count++;
        return;
    }

    /* Reserve the worst case first: the frame is queued completely or not at
     * all, so the wire never sees a partial frame. */
    wait_start = (uint16_t)llp_transport_get_ms();
    while (llp_tx_ring_free() < needed) {
        UCSR0B |= (1 << UDRIE0);
        if ((uint16_t)((uint16_t)llp_transport_get_ms() - wait_start) >= LLP_TX_WAIT_MS) {
            llp_tx_dropped_count++;
            return;
        }
    }

    llp4_tx_begin(&tx, LLP_TX_CHANNEL, idx, llp_tx_sink, NULL);
    for (i = 0; i < idx; i++) {
        llp4_tx_byte(&tx, llp_tx_line_buf[i]);
    }
    llp4_tx_end(&tx);

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
