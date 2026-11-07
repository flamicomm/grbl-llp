# AGENTS.md — `src/` (firmware)

Notas de bajo nivel. Se cargan al trabajar dentro de `src/`. Las reglas generales
(build, git, estilo, diagnóstico) están en el `AGENTS.md` de la raíz.

## Orden de arranque

`src/main.c:43` guarda `MCUSR` antes de limpiarlo (se reporta como `[MSG:RST:*]`).
Luego `src/main.c:48-52`:

1. `serial_init()` — baud + ISR UART + `llp_transport_init()`
2. `settings_init()` — carga settings desde EEPROM
3. `stepper_init()` — pines y timers de stepper
4. `system_init()` — pines de control y pin-change interrupt
5. `sei()` — habilita interrupciones

Después entra al `for(;;)` de inicialización (`src/main.c:69`), que limpia `sys`,
ejecuta startup lines y llama a `protocol_main_loop()` (`src/main.c:119`).

## Mapa de interrupciones (todas verificadas)

| ISR | Archivo:línea | Rol |
|---|---|---|
| `SERIAL_RX` | `src/serial.c:134` | recibe byte UART → `llp_transport_rx_byte()` |
| `SERIAL_UDRE` | `src/serial.c:99` | TX listo → `llp_transport_tx_byte()` |
| `TIMER1_COMPA_vect` | `src/stepper.c:319` | segmento del planificador (genera pulsos) |
| `TIMER0_OVF_vect` | `src/stepper.c:489` | temporización de pulso STEP |
| `TIMER0_COMPA_vect` | `src/stepper.c:504` | fin del `STEP_PULSE_DELAY` |
| `TIMER2_OVF_vect` | `src/llp_transport.c:55` | contador de ms de LLP |
| `LIMIT_INT_vect` | `src/limits.c:110` | pin-change de límites (hard limits) |
| `WDT_vect` | `src/limits.c:135` | watchdog (uso interno de Grbl) |
| `CONTROL_INT_vect` | `src/system.c:118` | pin-change de control (reset/cycle/feed hold) |

**Timer0, Timer1 y Timer2 están ocupados.** No agregues ISRs ni reconfigures prescalers
sin revisar el impacto en step timing o en el contador de ms de LLP.

## Estado de flags en `src/config.h`

| Flag | Estado | Línea |
|---|---|---|
| `MESSAGE_PROBE_COORDINATES` | habilitado (auto-report tras G38.x) | `164` |
| `CHECK_LIMITS_AT_INIT` | deshabilitado (sin switches) | `230` |
| `REPORT_FIELD_*` | deshabilitados, salvo `LINE_NUMBERS` | `269-274` |
| `ENABLE_BUILD_INFO_WRITE_COMMAND` | deshabilitado (`$I=` no existe) | `520` |
| `DEBUG_KEEPALIVE` | habilitado (diagnóstico de campo) | `707` |
| `CONTROL_RESET_DEBOUNCE_MS` | `100` ms | `713` |
| `DISABLE_CONTROL_RESET_PIN` | deshabilitado (A0 activo) | `730` |
| `LLP_RX_PAYLOAD_SIZE` | `64` (capacidad del parser v4) | `src/llp_transport.h` |
| `LLP_TX_LINE_BUF_SIZE` | `48` (una línea = una trama) | `src/llp_transport.h` |
| `LLP_RX_TIMEOUT_MS` | `2000` | `src/llp_transport.h` |

Los números de línea de esta tabla son volátiles: confírmalos con
`grep -n "<FLAG>" src/config.h` antes de citarlos en otro sitio.

## Integración LLP ↔ Grbl

| Punto | Archivo:línea | Qué hace |
|---|---|---|
| init | `src/serial.c:86` | `llp_transport_init()` tras configurar UART |
| TX | `src/serial.c:94` | `serial_write()` delega en `llp_transport_tx_byte()` |
| RX | `src/serial.c:137` | la ISR delega en `llp_transport_rx_byte()` |
| stats | `src/llp_transport.c:270` | `llp_transport_get_stats()` |
| comando `#` | `src/protocol.c:101-116` | imprime `buf/rx/e/t/rd/td` |

### Perfil R1 (invariantes)

- El ACK se emite **solo desde el main loop** (`llp_transport_poll()`, llamado en
  `protocol_execute_realtime()`). La ISR únicamente deja `llp_ack_pending`; el
  ring de TX tiene un solo escritor.
- Un frame `CH1` se acepta **atómicamente**: si el payload no entra completo en
  el ring de línea, no se consume el `SEQ` y no se ACKea. Nunca escribir medio
  comando.
- Un `SEQ` repetido se **re-ACKea y se descarta** (`llp4_rel_rx_accept()`): es lo
  que evita ejecutar dos veces un movimiento cuando se pierde el ACK.
- El canal 0 (stream grbl) y los realtime no cambian: la clasificación es por
  `channel` y el resto se ignora sin error.

## Cuidados en `src/`

- **`src/*.bak` no son fuente** (`config.h.bak`, `protocol.c.bak`, `planner.c.bak`,
  `gcode.c.bak`, `coolant_control.c.bak`): no se editan, no se compilan y no valen
  como evidencia de comportamiento.
- El bloque comentado de `src/config.h` (~678–700) documenta valores viejos; el valor
  activo puede vivir en otro header. Ver `doc/buffers.md`.
- ISRs cortas: nada de `printString`/`report_*` dentro de una ISR; encola y procesa
  en `protocol_execute_realtime()`.
- Cualquier campo nuevo en `sys` o `settings` consume RAM permanente: mide con
  `avr-size` antes de dar el cambio por bueno.
- **El stack es el recurso más ajustado.** La RAM libre después de `.noinit` es todo
  lo que hay, y `.noinit` está pegado debajo: un desborde lo corrompe en silencio
  (ver `doc/buffers.md` → "Stack"). Antes de agregar locales grandes o buffers,
  mirá `pk=` / `lim=` del keep-alive.
- **`.noinit` está reservado para diagnóstico de resets** (`src/system.c`:
  `last_reset_source`, `a0_transient_count`, `diag_*`). `system_diag_boot_init()`
  tiene que ser lo primero que corre en `main()`. No metas estado normal ahí.
- **Largo de las líneas de salida:** `llp_transport_tx_byte()` corta la línea cada
  `LLP_TX_CHUNK_SIZE` (hoy 48 bytes, igual a `LLP_TX_LINE_BUF_SIZE`). Una línea más
  larga se parte en varias tramas; la trama completa (peor caso) tiene que entrar en
  `TX_RING_BUFFER` porque el emisor reserva el espacio antes de escribir.
