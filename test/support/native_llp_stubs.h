/*
  native_llp_stubs.h — host definitions for the Grbl symbols the LLP transport
  uses.

  `src/llp_transport.c` talks to the rest of the firmware through a handful of
  globals and system helpers. On the target they live in serial.c, system.c and
  motion_control.c; here they are replaced by the minimum that makes the
  transport link and lets a test observe what it did (flags set, reset
  requested).

  The whole header is included by exactly one translation unit — the test file,
  which also `#include`s `src/llp_transport.c` so the module's `static` state
  (the ms counter, the R1 receiver) can be driven by the test. That is why the
  definitions below are not `static`: there is only one TU and the linker never
  sees a second copy.

  Nothing in `src/` includes this file: production builds are untouched.
*/

#ifndef NATIVE_LLP_STUBS_H
#define NATIVE_LLP_STUBS_H

#include "grbl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------- */
/* AVR registers (declared in test/support/avr/io.h)                         */
/* ------------------------------------------------------------------------- */

/* Interrupts start "enabled", like the main loop of the firmware. */
volatile uint8_t SREG = (uint8_t)(1u << SREG_I);
volatile uint8_t TCCR2A = 0;
volatile uint8_t TCCR2B = 0;
volatile uint8_t TIMSK2 = 0;
volatile uint8_t UCSR0B = 0;

volatile uint8_t PINB = 0, PINC = 0, PIND = 0;
volatile uint8_t DDRB = 0, DDRC = 0, DDRD = 0;
volatile uint8_t PORTB = 0, PORTC = 0, PORTD = 0;

/* ------------------------------------------------------------------------- */
/* Grbl serial rings (serial.c)                                              */
/* ------------------------------------------------------------------------- */

uint8_t serial_rx_buffer[RX_RING_BUFFER];
volatile uint8_t serial_rx_buffer_head = 0;
volatile uint8_t serial_rx_buffer_tail = 0;

uint8_t serial_tx_buffer[TX_RING_BUFFER];
volatile uint8_t serial_tx_buffer_head = 0;
volatile uint8_t serial_tx_buffer_tail = 0;

/* ------------------------------------------------------------------------- */
/* Grbl system module (system.c, motion_control.c)                           */
/* ------------------------------------------------------------------------- */

system_t sys;

volatile uint8_t sys_probe_state = 0;
volatile uint8_t sys_rt_exec_state = 0;
volatile uint8_t sys_rt_exec_alarm = 0;
volatile uint8_t sys_rt_exec_motion_override = 0;
volatile uint8_t sys_rt_exec_accessory_override = 0;

volatile uint8_t last_reset_source = RESET_SRC_NONE;

/* Observation points for the assertions. */
unsigned stub_mc_reset_calls = 0;

void system_set_reset_source(uint8_t src)
{
    last_reset_source = src;
}

void mc_reset(void)
{
    stub_mc_reset_calls++;
}

void system_set_exec_state_flag(uint8_t mask)
{
    sys_rt_exec_state |= mask;
}

void system_set_exec_motion_override_flag(uint8_t mask)
{
    sys_rt_exec_motion_override |= mask;
}

void system_set_exec_accessory_override_flag(uint8_t mask)
{
    sys_rt_exec_accessory_override |= mask;
}

#ifdef __cplusplus
}
#endif

#endif /* NATIVE_LLP_STUBS_H */
