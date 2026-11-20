/*
  util/delay.h — host stub for the AVR busy-wait helpers.

  grbl.h includes this header. The LLP transport has no delay calls (the TX
  ring wait is driven by the millisecond tick, not by _delay_ms), so the
  macros are no-ops; a real delay would only slow the test down.
*/

#ifndef NATIVE_UTIL_DELAY_H
#define NATIVE_UTIL_DELAY_H

#define _delay_ms(ms) ((void)(ms))
#define _delay_us(us) ((void)(us))

#endif /* NATIVE_UTIL_DELAY_H */
