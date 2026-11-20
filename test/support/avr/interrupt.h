/*
  avr/interrupt.h — host stub for `cli()`, `sei()` and the `ISR()` macro.

  On the host an interrupt vector is just a function, so a test can invoke the
  Timer2 overflow handler directly to advance the LLP millisecond counter. No
  real interrupt is ever delivered: the harness is single-threaded and feeds
  bytes explicitly, which is the point (the ISR body is exercised, not the
  hardware).
*/

#ifndef NATIVE_AVR_INTERRUPT_H
#define NATIVE_AVR_INTERRUPT_H

#include "avr/io.h"

#ifdef __cplusplus
extern "C" {
#endif

/* `ISR(TIMER2_OVF_vect)` becomes `void TIMER2_OVF_vect(void)`: a plain
 * function the test can call. */
#define ISR(vector) void vector(void)

static inline void cli(void)
{
    SREG = (uint8_t)(SREG & (uint8_t)~(1u << SREG_I));
}

static inline void sei(void)
{
    SREG = (uint8_t)(SREG | (uint8_t)(1u << SREG_I));
}

#ifdef __cplusplus
}
#endif

#endif /* NATIVE_AVR_INTERRUPT_H */
