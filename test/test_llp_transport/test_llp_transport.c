/*
  test_llp_transport.c — host tests for the LLP integration logic (CNC-029 G-I4)

  What is under test: the parts of `src/llp_transport.c` that only the bench
  could exercise until now — the atomic acceptance of an R1 command against
  grbl's line ring, duplicate suppression and re-ACK, the HELLO reset, the
  SEQ wrap, the TX chunking of long lines, byte stuffing of 0xAA in a payload,
  and the inter-byte timeout.

  How it works: this file includes `src/llp_transport.c` directly, so the
  module's `static` state (the millisecond counter, the R1 receiver, the parser)
  is reachable from the test. Everything the module pulls from the rest of the
  firmware is stubbed:

    - `<avr/io.h>`, `<avr/interrupt.h>`, ... -> the headers under
      test/support/avr/ (registers are plain bytes, `ISR()` is a plain
      function, `cli()`/`sei()` toggle the modelled I flag);
    - serial rings, `sys`, the exec flags and `mc_reset()` ->
      test/support/native_llp_stubs.h;
    - frames on the wire are built with the same vendored encoder the firmware
      uses (`llp4_encode`), then fed byte by byte through
      `llp_transport_rx_byte()`, which is exactly what the UART ISR does.

  Production is not compiled here: the `native_test` env has
  `test_build_src = no`, so `uno`/`nano` are untouched by this file.

  Hardware note: time is simulated by calling the Timer2 overflow handler
  (`TIMER2_OVF_vect`), one call per LLP tick (1.024 ms on the target; the
  module works in ticks and the drift is documented in doc/known-issues.md §8).
*/

#include <string.h>

#include "unity.h"

#include "grbl.h"
#include "native_llp_stubs.h"

/* Single-translation-unit harness: the module's internals are part of the test
 * program on purpose (llp_ms_counter, llp_rel_rx, ...). */
#include "../../src/llp_transport.c"

/* The stub ISR() macro turns this into an ordinary function. */
void TIMER2_OVF_vect(void);

/* ------------------------------------------------------------------------- */
/* Helpers                                                                   */
/* ------------------------------------------------------------------------- */

#define MAX_DECODED_FRAMES 8

typedef struct {
    uint8_t channel;
    uint8_t len;
    uint8_t payload[LLP_RX_PAYLOAD_SIZE];
} decoded_frame_t;

static decoded_frame_t frames[MAX_DECODED_FRAMES];
static int frame_count;

static uint8_t last_wire[2 * LLP_RX_PAYLOAD_SIZE + 8];
static size_t last_wire_len;

/* Clean slate: empty rings, fresh parser and dedup state, interrupts "on". */
static void reset_harness(void)
{
    serial_rx_buffer_head = 0;
    serial_rx_buffer_tail = 0;
    serial_tx_buffer_head = 0;
    serial_tx_buffer_tail = 0;

    memset(&sys, 0, sizeof(sys));
    sys_rt_exec_state = 0;
    sys_rt_exec_motion_override = 0;
    sys_rt_exec_accessory_override = 0;
    last_reset_source = RESET_SRC_NONE;
    stub_mc_reset_calls = 0;

    llp_ms_counter = 0;
    llp_transport_init();
    llp_transport_reset_stats();

    frame_count = 0;
    last_wire_len = 0;
    SREG = (uint8_t)(1u << SREG_I);
}

/* Model the UART ISR draining the TX ring: pop every queued byte, rebuild the
 * frames with the vendored parser and keep them for the assertions. */
static void drain_tx_ring(void)
{
    llp4_parser_t parser;
    uint8_t payload[LLP_RX_PAYLOAD_SIZE];

    llp4_parser_init(&parser, payload, (uint8_t)sizeof(payload), 0);

    while (serial_tx_buffer_tail != serial_tx_buffer_head) {
        uint8_t tail = serial_tx_buffer_tail;
        uint8_t byte = serial_tx_buffer[tail];

        tail = (uint8_t)((tail + 1u) % TX_RING_BUFFER);
        serial_tx_buffer_tail = tail;

        if (llp4_parser_feed(&parser, byte, (uint16_t)llp_ms_counter) ==
            LLP4_EV_FRAME) {
            if (frame_count < MAX_DECODED_FRAMES) {
                frames[frame_count].channel = parser.channel;
                frames[frame_count].len = parser.length;
                memcpy(frames[frame_count].payload, payload, parser.length);
                frame_count++;
            }
        }
    }
}

/* Encode a frame with the real library and hand its bytes to the transport,
 * one by one, like the UART ISR would. */
static void feed_frame(uint8_t channel, const uint8_t *payload, uint8_t len)
{
    uint8_t wire[2 * LLP_RX_PAYLOAD_SIZE + 8];
    size_t n;
    size_t i;

    n = llp4_encode(channel, payload, len, wire, sizeof(wire));
    TEST_ASSERT_TRUE(n > 0);

    last_wire_len = (n < sizeof(last_wire)) ? n : sizeof(last_wire);
    memcpy(last_wire, wire, last_wire_len);

    for (i = 0; i < n; i++) {
        llp_transport_rx_byte(wire[i]);
    }
}

/* R1 command frame: payload = [SEQ][DATA...]. */
static void feed_command(uint8_t seq, const uint8_t *data, uint8_t data_len)
{
    uint8_t payload[LLP_RX_PAYLOAD_SIZE];

    TEST_ASSERT_TRUE((size_t)data_len + 1u <= sizeof(payload));
    payload[0] = seq;
    memcpy(&payload[1], data, data_len);
    feed_frame((uint8_t)LLP4_REL_CH_COMMAND, payload, (uint8_t)(data_len + 1u));
}

/* Pull what the main loop would consume from grbl's line ring. */
static size_t read_line_ring(uint8_t *out, size_t cap)
{
    size_t n = 0;

    while (serial_rx_buffer_head != serial_rx_buffer_tail && n < cap) {
        out[n++] = serial_rx_buffer[serial_rx_buffer_tail];
        serial_rx_buffer_tail =
            (uint8_t)((serial_rx_buffer_tail + 1u) % RX_RING_BUFFER);
    }
    return n;
}

/* Advance the LLP clock by calling the Timer2 overflow handler. */
static void advance_ms(unsigned int ticks)
{
    while (ticks > 0u) {
        TIMER2_OVF_vect();
        ticks--;
    }
}

/* ------------------------------------------------------------------------- */
/* (a) A command that does not fit is deferred: no SEQ consumed, no ACK.     */
/* ------------------------------------------------------------------------- */

static void test_busy_ring_defers_command_without_ack(void)
{
    static const uint8_t line[] = "G1X1\n";
    llp_stats_t stats;

    /* Leave two free bytes in the line ring (head=3, tail=6): fewer than the
     * five data bytes of the command. */
    serial_rx_buffer_head = 3;
    serial_rx_buffer_tail = 6;

    feed_command(7, line, (uint8_t)(sizeof(line) - 1u));

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_busy);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_duplicates);

    /* The ACK is emitted from the main loop: with the command deferred there
     * must be nothing to send. */
    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(0, frame_count);

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(0, stats.tx_acks);

    /* The main loop drains the ring; the sender retransmits the same SEQ and
     * now it must be accepted (the sequence number was never consumed). */
    serial_rx_buffer_tail = serial_rx_buffer_head;
    feed_command(7, line, (uint8_t)(sizeof(line) - 1u));

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_duplicates);

    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(1, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP4_REL_CH_ACK, frames[0].channel);
    TEST_ASSERT_EQUAL_UINT8(1, frames[0].len);
    TEST_ASSERT_EQUAL_UINT8(7, frames[0].payload[0]);
}

/* ------------------------------------------------------------------------- */
/* (b) A repeated SEQ is re-ACKed and its data discarded.                    */
/* ------------------------------------------------------------------------- */

static void test_duplicate_seq_is_reacked_and_discarded(void)
{
    static const uint8_t line[] = "G0X0\n";
    uint8_t ring[32];
    size_t ring_len;
    llp_stats_t stats;

    feed_command(3, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(1, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP4_REL_CH_ACK, frames[0].channel);
    TEST_ASSERT_EQUAL_UINT8(3, frames[0].payload[0]);

    /* The ACK was "lost" on the wire: the sender repeats the frame. */
    frame_count = 0;
    feed_command(3, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_poll();
    drain_tx_ring();

    TEST_ASSERT_EQUAL_INT(1, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP4_REL_CH_ACK, frames[0].channel);
    TEST_ASSERT_EQUAL_UINT8(3, frames[0].payload[0]);

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_duplicates);

    /* The data was delivered exactly once. */
    ring_len = read_line_ring(ring, sizeof(ring));
    TEST_ASSERT_EQUAL_INT((int)(sizeof(line) - 1u), (int)ring_len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(line, ring, sizeof(line) - 1u);
}

/* ------------------------------------------------------------------------- */
/* (c) HELLO starts a new session: the dedup state is reset.                 */
/* ------------------------------------------------------------------------- */

static void test_hello_resets_dedup_state(void)
{
    static const uint8_t line[] = "G2X2\n";
    const uint8_t session = 0x52; /* what the ESP emitter sends today */
    llp_stats_t stats;

    feed_command(5, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_poll();
    drain_tx_ring();

    /* Same SEQ before HELLO: duplicate, suppressed (and re-ACKed). */
    frame_count = 0;
    feed_command(5, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_duplicates);

    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(1, frame_count);

    /* New session: HELLO is outbound only, it must not be ACKed. */
    frame_count = 0;
    feed_frame((uint8_t)LLP4_REL_CH_HELLO, &session, 1);
    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(0, frame_count);

    /* The sender restarted its SEQ at the same value: it must be delivered. */
    feed_command(5, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(2, stats.rx_commands);
    /* HELLO restarts the whole receiver state, duplicate counter included:
     * `dup=` in the `#` report starts again from zero for the new session. */
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_duplicates);
}

/* ------------------------------------------------------------------------- */
/* (d) SEQ wraps 255 -> 0: both are new, and dedup still works afterwards.   */
/* ------------------------------------------------------------------------- */

static void test_seq_wraps_255_to_0(void)
{
    static const uint8_t first[] = "a\n";
    static const uint8_t second[] = "b\n";
    llp_stats_t stats;

    feed_command(255, first, (uint8_t)(sizeof(first) - 1u));
    feed_command(0, second, (uint8_t)(sizeof(second) - 1u));

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(2, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_duplicates);

    /* 0 is a new SEQ after 255 (not a duplicate), and repeating 0 is. */
    feed_command(0, second, (uint8_t)(sizeof(second) - 1u));
    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(2, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_duplicates);

    llp_transport_poll();
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(1, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP4_REL_CH_ACK, frames[0].channel);
    TEST_ASSERT_EQUAL_UINT8(0, frames[0].payload[0]);
}

/* ------------------------------------------------------------------------- */
/* (e) A line longer than the 48-byte chunk leaves as two frames.            */
/* ------------------------------------------------------------------------- */

static void test_long_line_is_split_in_two_frames(void)
{
    static const char line[] =
        "<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\n";
    const size_t line_len = sizeof(line) - 1u;
    size_t sent = 0;
    size_t rebuilt_len = 0;
    uint8_t rebuilt[2 * LLP_TX_LINE_BUF_SIZE];
    llp_stats_t stats;

    TEST_ASSERT_TRUE(line_len > LLP_TX_CHUNK_SIZE);

    /* First chunk: exactly LLP_TX_CHUNK_SIZE bytes, flushed by the module. */
    while (sent < (size_t)LLP_TX_CHUNK_SIZE) {
        llp_transport_tx_byte((uint8_t)line[sent++]);
    }

    /* Model the UART ISR draining the ring before the rest of the line. */
    drain_tx_ring();
    TEST_ASSERT_EQUAL_INT(1, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP_TX_CHANNEL, frames[0].channel);
    TEST_ASSERT_EQUAL_UINT8(LLP_TX_CHUNK_SIZE, frames[0].len);

    while (sent < line_len) {
        llp_transport_tx_byte((uint8_t)line[sent++]);
    }
    drain_tx_ring();

    TEST_ASSERT_EQUAL_INT(2, frame_count);
    TEST_ASSERT_EQUAL_UINT8(LLP_TX_CHANNEL, frames[1].channel);
    TEST_ASSERT_EQUAL_UINT8((uint8_t)(line_len - LLP_TX_CHUNK_SIZE),
                            frames[1].len);

    /* The two payloads reconstruct the line byte for byte. */
    memcpy(&rebuilt[rebuilt_len], frames[0].payload, frames[0].len);
    rebuilt_len += frames[0].len;
    memcpy(&rebuilt[rebuilt_len], frames[1].payload, frames[1].len);
    rebuilt_len += frames[1].len;
    TEST_ASSERT_EQUAL_INT((int)line_len, (int)rebuilt_len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(line, rebuilt, line_len);

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(0, stats.tx_dropped);
    TEST_ASSERT_EQUAL_UINT8(0, llp_transport_get_tx_line_idx());
}

/* ------------------------------------------------------------------------- */
/* (f) 0xAA inside the payload survives the stuffing.                        */
/* ------------------------------------------------------------------------- */

static void test_magic_byte_in_payload_keeps_stuffing(void)
{
    static const uint8_t data[] = {0xAA, 'X', 0xAA, '\n'};
    const uint8_t data_len = (uint8_t)(sizeof(data) - 1u);
    uint8_t ring[16];
    size_t ring_len;
    size_t i;
    size_t magic_count = 0;
    llp_stats_t stats;

    feed_command(9, data, data_len);

    /* The encoder must escape every payload 0xAA as [0xAA][0x00]: after the
     * two magic bytes no bare 0xAA may appear. */
    TEST_ASSERT_TRUE(last_wire_len > 2u);
    for (i = 2; i < last_wire_len; i++) {
        if (last_wire[i] == LLP4_MAGIC_1) {
            magic_count++;
            TEST_ASSERT_TRUE(i + 1u < last_wire_len);
            TEST_ASSERT_EQUAL_UINT8(0x00, last_wire[i + 1u]);
            i++; /* skip the escape byte */
        }
    }
    TEST_ASSERT_TRUE(magic_count >= 2u);

    /* The receiver un-stuffs and delivers the payload untouched. */
    ring_len = read_line_ring(ring, sizeof(ring));
    TEST_ASSERT_EQUAL_INT((int)data_len, (int)ring_len);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(data, ring, data_len);

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_commands);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_errors);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_timeouts);
}

/* ------------------------------------------------------------------------- */
/* (g) A gap longer than LLP_RX_TIMEOUT_MS reports LLP4_ERR_TIMEOUT.         */
/* ------------------------------------------------------------------------- */

static void test_gap_over_timeout_reports_timeout(void)
{
    static const uint8_t line[] = "ok\n";
    uint8_t wire[2 * LLP_RX_PAYLOAD_SIZE + 8];
    size_t n;
    size_t i;
    llp_stats_t stats;
    uint8_t payload[LLP_RX_PAYLOAD_SIZE];

    payload[0] = 1; /* SEQ */
    memcpy(&payload[1], line, sizeof(line) - 1u);
    n = llp4_encode((uint8_t)LLP4_REL_CH_COMMAND, payload,
                    (uint8_t)sizeof(line), wire, sizeof(wire));
    TEST_ASSERT_TRUE(n > 0);

    /* Feed the frame's header and one payload byte... */
    for (i = 0; i < 5u; i++) {
        llp_transport_rx_byte(wire[i]);
    }

    /* ...then stop for longer than the inter-byte timeout. */
    advance_ms((unsigned int)LLP_RX_TIMEOUT_MS + 1u);
    llp_transport_rx_byte(wire[5]);

    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_errors);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_timeouts);
    TEST_ASSERT_EQUAL_UINT16(0, stats.rx_frames);

    /* The parser resynchronises: the next full frame is delivered. */
    feed_command(2, line, (uint8_t)(sizeof(line) - 1u));
    llp_transport_get_stats(&stats);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_frames);
    TEST_ASSERT_EQUAL_UINT16(1, stats.rx_commands);
}

/* ------------------------------------------------------------------------- */
/* Runner                                                                    */
/* ------------------------------------------------------------------------- */

void setUp(void)
{
    reset_harness();
}

void tearDown(void)
{
}

int main(int argc, char **argv)
{
    (void)argc;
    (void)argv;

    UNITY_BEGIN();
    RUN_TEST(test_busy_ring_defers_command_without_ack);
    RUN_TEST(test_duplicate_seq_is_reacked_and_discarded);
    RUN_TEST(test_hello_resets_dedup_state);
    RUN_TEST(test_seq_wraps_255_to_0);
    RUN_TEST(test_long_line_is_split_in_two_frames);
    RUN_TEST(test_magic_byte_in_payload_keeps_stuffing);
    RUN_TEST(test_gap_over_timeout_reports_timeout);
    return UNITY_END();
}
