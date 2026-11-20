/*
  avr/io.h — host stub for the ATmega328P register file.

  The `native_test` environment compiles `src/llp_transport.c` on the host. The
  firmware headers reach this file through `<avr/io.h>` (grbl.h): the include
  path puts `test/support` before the system headers, so nothing here touches
  the production build.

  Only the registers the LLP transport reads or writes are modelled, as plain
  bytes so a test can preset or inspect them. The production AVR build keeps
  using the real header: this file is never on its include path.
*/

#ifndef NATIVE_AVR_IO_H
#define NATIVE_AVR_IO_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Status register. Bit 7 mirrors the AVR I flag; the harness starts with
 * interrupts "enabled" (SREG = 0x80) so the llp_tx_emit wait loop behaves like
 * the main loop does on the target. */
extern volatile uint8_t SREG;

/* Timer2 (LLP millisecond tick, llp_transport.c) and UART TX interrupt enable. */
extern volatile uint8_t TCCR2A;
extern volatile uint8_t TCCR2B;
extern volatile uint8_t TIMSK2;
extern volatile uint8_t UCSR0B;

/* Port registers referenced by cpu_map.h macros (never expanded by the LLP
 * translator unit, declared so the header stays compilable if a test reads a
 * pin map macro by accident). */
extern volatile uint8_t PINB;
extern volatile uint8_t PINC;
extern volatile uint8_t PIND;
extern volatile uint8_t DDRB;
extern volatile uint8_t DDRC;
extern volatile uint8_t DDRD;
extern volatile uint8_t PORTB;
extern volatile uint8_t PORTC;
extern volatile uint8_t PORTD;

/* Bit positions used by llp_transport.c and the UART helper in the harness. */
#define SREG_I 7
#define CS22 2
#define TOIE2 0
#define UDRIE0 5

#ifdef __cplusplus
}
#endif

#endif /* NATIVE_AVR_IO_H */
