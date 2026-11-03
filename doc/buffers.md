# Buffers y uso de RAM (grbl-llp)

Optimización de RAM para ATmega328P. Los valores cambian con el código: **este
documento no es la fuente de verdad**, es el mapa. Verifica siempre con `grep`.

## Valores activos

| Buffer | Archivo:línea | Valor | Notas |
|---|---|---|---|
| `TX_BUFFER_SIZE` | `src/serial.h:31` y `:33` | 128 | dos ramas (`USE_LINE_NUMBERS` on/off), mismo valor |
| `RX_BUFFER_SIZE` | `src/serial.h:27` | 80 | sin cambios respecto a Grbl |
| `LINE_BUFFER_SIZE` | `src/protocol.h:32` | 48 | línea G-code máxima aceptada |
| `LLP_TX_LINE_BUF_SIZE` | `src/llp_transport.h:26` | 48 | búfer de línea antes de framear (igual a `LINE_BUFFER_SIZE`) |
| `TX_RING_BUFFER` | `src/serial.h:40` | 129 | 128 usables; la trama se emite directo acá (sin `frame_buf`) |
| `LLP_RX_PAYLOAD_SIZE` | `src/llp_transport.h` | 64 | payload máximo aceptado por el parser v4 |
| `BLOCK_BUFFER_SIZE` | `src/planner.h:29` y `:31` | 8 | bloques del planificador (bajado de 12, ver "Stack") |
| `LLP_RX_TIMEOUT_MS` | `src/llp_transport.h` | 2000 | timeout inter-byte del parser |

## Trampa: `src/config.h` miente por diseño

`src/config.h` tiene un bloque comentado (aprox. líneas 678–700) que **documenta**
valores por defecto de otra época:

```
#define LLP_MAX_PAYLOAD 96      ← dentro de /* */
#define TX_BUFFER_SIZE 160      ← dentro de /* */
#define LLP_FRAME_TIMEOUT_MS 2000 ← dentro de /* */
```

(`LLP_MAX_PAYLOAD` y `LLP_FRAME_TIMEOUT_MS` eran de la librería v3: en v4 la
capacidad la fija `LLP_RX_PAYLOAD_SIZE` en `src/llp_transport.h`.)

Ninguna de esas líneas compila. La definición activa puede estar en:

- `src/llp_transport.h` → configuración del transporte v4 (payload, línea, timeout)
- `src/llp_v4.h` → defaults de la librería (`LLP4_DEFAULT_TIMEOUT_MS`, etc.)
- `src/planner.h`, `src/serial.h`, `src/protocol.h` → defaults con `#ifndef`

Regla: **nunca cites una línea de `config.h` sin comprobar que no está dentro del
bloque de comentario.**

## Cómo verificar

```bash
grep -rn "define .*BUFFER_SIZE\|define LLP_RX_PAYLOAD_SIZE\|define LLP_RX_TIMEOUT_MS" src/
```

Y para el consumo real:

```bash
~/.platformio/packages/toolchain-atmelavr/bin/avr-size --format=avr --mcu=atmega328p \
  .pio/build/uno/firmware.elf
```

## Análisis de tráfico que justifica los tamaños

- Comando G-code más largo observado: 21 caracteres.
- Respuesta TX más larga: 42 caracteres (status report de 3 ejes con posiciones
  tipo `±1234.567`).
- De ahí que `LINE_BUFFER_SIZE = 48` y `TX_BUFFER_SIZE = 128` dejen margen sin
  desperdiciar RAM.

## Dependencias al cambiar tamaños

1. `LINE_BUFFER_SIZE` debe caber en `LLP_RX_PAYLOAD_SIZE` (la línea viaja como payload del frame).
2. `LLP_TX_LINE_BUF_SIZE` debe poder contener la respuesta más larga antes de framear
   y, junto con el resto de la trama, entrar completa en `TX_RING_BUFFER`: el
   emisor reserva el peor caso antes de escribir (todo o nada), y hay un
   `_Static_assert`-style que lo verifica en compilación.
3. Subir cualquiera de los tres reduce el margen libre de RAM (2048 B menos el valor
   de `Data` que reporta `avr-size`). Recompila y vuelve a medir antes de dar por
   bueno el cambio.
4. `BLOCK_BUFFER_SIZE` afecta el comando `#` (`buf:N` = bloques libres) y la suavidad
   del streaming. Se bajó de 12 a 8 para liberar 184 B de RAM estática: con los feeds
   de esta máquina (< 500 mm/min) el lookahead sobrante no aporta nada y el stack lo
   necesitaba (ver "Stack"). No lo bajes más sin medir `pk=`.

## Stack: el recurso más ajustado

La RAM libre por encima de `.data`/`.bss`/`.noinit` es el stack. En el ATmega328P del
Uno eran **323 B** con el build anterior: la cadena más profunda (ver abajo) llegaba a
**331 B** y escribía dentro de `.noinit`, corrompiendo el diagnóstico de resets
(`ca`, `last_reset_source`, `diag_magic`) sin que nada fallara visiblemente.

Cadena más profunda (medida con el desensamblado, `main` incluye
`protocol_main_loop`/`protocol_execute_line` inlineados):

```
main (33) → gc_execute_line (94) → mc_line (7) → protocol_execute_realtime (21)
          → protocol_exec_rt_system (56, incluye report_realtime_status)
          → report/print (printFloat 27, etc.)     ← acá se imprimen ?/[DBG]
```

`mc_line()` espera espacio en el planificador llamando a `protocol_execute_realtime()`
(`src/motion_control.c:61`), así que **los reportes de estado y el `[DBG]` se imprimen
desde el punto más profundo** mientras el planner está lleno. Encima de eso se apila
cualquier ISR (la de UART RX son 21 B).

Medición: al arrancar se pinta la región libre con `0x5A` (`system_diag_stack_paint()`);
en el arranque siguiente se escanea y se reporta `[MSG:BOOT:stk=<pico> lim=<total>]`.
El keep-alive reporta el pico vivo en `pk=`.

Valores de referencia tras el fix de stack y del emisor en streaming: `lim=689`,
`pk=331` con streaming y planner lleno (≈358 B de margen).
Si `pk` se acerca a `lim`, el `.noinit` va a volver a corromperse: liberá RAM
(`BLOCK_BUFFER_SIZE`, locales `static`) antes de que eso pase.

## Stats del comando `#`

`#` imprime contadores de **transporte** (`llp_transport_get_stats()`), que están
siempre activos. Los contadores del **parser** están compilados fuera:
En v4 el parser no tiene contadores de parser: los del transporte
(`llp_transport.c`) están siempre activos y se consultan con `llp_transport_get_stats()`.
