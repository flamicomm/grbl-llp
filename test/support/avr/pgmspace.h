/*
  avr/pgmspace.h — host stub for the AVR program-memory helpers.

  Nothing in `llp_transport.c` reads program memory, but grbl.h and the vendored
  LLP headers include this file. On the host everything is addressable, so
  `PROGMEM` disappears and `pgm_read_*` are plain loads.
*/

#ifndef NATIVE_AVR_PGMSPACE_H
#define NATIVE_AVR_PGMSPACE_H

#include <stdint.h>

#define PROGMEM
#define PSTR(s) (s)

#define pgm_read_byte(address) (*(const uint8_t *)(address))
#define pgm_read_word(address) (*(const uint16_t *)(address))
#define pgm_read_dword(address) (*(const uint32_t *)(address))
#define pgm_read_float(address) (*(const float *)(address))

#endif /* NATIVE_AVR_PGMSPACE_H */
