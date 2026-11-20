/*
  avr/wdt.h — host stub for the watchdog helpers.

  grbl.h includes the watchdog header; the LLP transport never arms the WDT. On
  the host there is no watchdog, so the calls are no-ops.
*/

#ifndef NATIVE_AVR_WDT_H
#define NATIVE_AVR_WDT_H

#define WDTO_15MS 0
#define WDTO_30MS 1
#define WDTO_60MS 2
#define WDTO_120MS 3
#define WDTO_250MS 4
#define WDTO_500MS 5
#define WDTO_1S 6
#define WDTO_2S 7
#define WDTO_4S 8
#define WDTO_8S 9

#define wdt_reset() ((void)0)
#define wdt_disable() ((void)0)
#define wdt_enable(timeout) ((void)(timeout))

#endif /* NATIVE_AVR_WDT_H */
