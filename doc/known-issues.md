# Issues conocidos y diagnóstico (grbl-llp)

Narrativa detallada. El resumen operativo está en `AGENTS.md`; el protocolo y las
diferencias con Grbl, en `README.md`.

## 1. Cuelgue por EEPROM corrupta (crítico)

**Síntoma:** el MCU se congela por completo (sin respuesta serie, sin keep-alive)
tras cualquier `G0`/`G1`, en cualquier eje. `$$` muestra valores imposibles como
`$11=-2147483.648` (0x80000000 leído como float), `$110=-2147483.648`,
`$121=0.000`, o faltan líneas (`$101`, `$120`).

**Causa:** EEPROM con datos de otro build/otro fork (`nan`, `inf`, floats sin
inicializar) en `max_rate`, `acceleration`, `junction_deviation`. El planificador
calcula parámetros de paso inválidos y la ISR de steppers o el main loop se traban.

**Solución:** `$RST=$` (restaura defaults de compilación; el MCU se resetea solo)
y verificar con `$$`. Después reconfigurar `$100`–`$132`. Snippet completo en
`README.md` → "Reseteo de EEPROM al flashear".

**Prevención:** verificar `$$` después de flashear cualquier board, nuevo o usado.

## 2. El "bug del eje Z" era el mismo problema

Se creyó durante un tiempo que era contención de pines del core de Grbl:

- el MCU se detenía tras el primer movimiento (de Z, o de cualquier eje — Z fue
  simplemente el primero que se probó);
- `$$` mostraba la misma basura que en el caso de EEPROM.

**Resolución:** tras `$RST=$`, `G0 Z2 F200` funciona incluso en un UNO pelado sin
CNC Shield. No existe bug de Z. Ante este síntoma: `$RST=$` **antes** de cualquier
otra hipótesis.

## 3. `ALARM:98`

No es un código de alarma de Grbl (el core define 1–10). Aparece con el planificador
en un estado inconsistente y la hipótesis vigente es corrupción de RAM por desborde
de stack en la capa LLP, no un bug del core.

Consecuencia práctica: **no existe "fix del ALARM:98"**. Si aparece, revisar el
margen de stack y el uso de buffers (`doc/buffers.md`), no parchear el código de
alarmas.

## 4. Keep-alive

- El keep-alive periódico del main loop fue eliminado para ahorrar flash.
- En el loop crítico de alarmas (`src/protocol.c:284`) queda un `[KA] ALARM\r\n`
  cada 1000 ms, implementado con el contador de ms de LLP (`llp_transport_get_ms()`).
- El host debe sondear con `?` o `#`. Si no hay respuesta, el MCU está colgado.
- El keep-alive de depuración (`DEBUG_KEEPALIVE`) está **habilitado** en
  `src/config.h` para diagnóstico de campo. Imprime cada 10 s:
  `[DBG:t=... st=... tx=... rx=... bf=... li=... sp=... u=... re=... rt=... rd=... td=...]`.
  - `sp` muy bajo → stack profundo / riesgo de desborde.
  - `tx`/`rx` saturados → cuello de botella serial.
  - `bf=0` → planner lleno (normal en streaming).
  - `u` = `UCSR0B` (bit `UDRIE0` indica TX pendiente).
  - `re`/`rt`/`rd`/`td` = errores, timeouts, drops RX y drops TX de LLP.
  Ocupa ~1 KB de flash extra en el UNO (93.4% usado). En el Nano queda al 98%:
  si no entra, deshabilitarlo con `// #define DEBUG_KEEPALIVE`.

## 4b. Reset del MCU: `[MSG:RST:...]`

Al reinicializar, `main.c` reporta la causa leyendo `MCUSR`:

| Mensaje | Significado |
|---|---|
| `[MSG:RST:BOR]` | Brown-out (caída de tensión) |
| `[MSG:RST:EXT]` | Reset externo (pin RESET / botón) |
| `[MSG:RST:WDR]` | Watchdog |
| `[MSG:RST:POR]` | Power-on |
| `[MSG:RST:SFR]` | **Soft reset** (software) |

Cuando es `SFR`, se agrega el origen:

| `[MSG:RST:SRC:...]` | Origen |
|---|---|
| `LLP` | Byte `0x18` (CMD_RESET) dentro de un frame LLP |
| `A0` | Pin A0 (Reset/Abort del shield) confirmado tras `CONTROL_RESET_DEBOUNCE_MS` |
| `RST` | Comando `$RST` |
| `HARD_LIMIT` / `SOFT_LIMIT` | Alarma de límites |
| `HOMING` | Fallo de homing |
| `OTHER` | Otro `mc_reset()` sin anotar |

El debounce de A0 está en **100 ms** (`CONTROL_RESET_DEBOUNCE_MS`) para tolerar
EMI de husillos con escobillas. Si el origen es `A0` y no hay botón de abort
conectado, agregar pull-up externo de 10 kΩ + 100 nF a GND en A0, o desconectar
ese pin del shield.

## 5. Subida al Arduino a través del ESP8266

El ESP es un dispositivo permanente en `/dev/ttyUSB0`. Para flashear hay que ponerlo
en modo transparente; con el env `nano` funciona a 57600 baud. El UNO usa 115200.

Si tras flashear no hay respuesta: verificar modo transparente y baud antes de
sospechar del firmware.

## 6. Frames LLP descartados

El flush de TX es no bloqueante: si el búfer está lleno, el frame parcial se descarta
y `tx_dropped` incrementa. Diagnóstico con el comando `#`:

```
buf:11 rx:150 e:0 t:2 rd:0 td:0
```

- `td:` creciente → `TX_BUFFER_SIZE` / `LLP_TX_LINE_BUF_SIZE` chicos, o el host no
  consume.
- `rd:` creciente → saturación de RX.
- `t:` creciente → timeouts inter-byte (`LLP_FRAME_TIMEOUT_MS`).
- `e:` creciente → CRC o framing inválido (revisar byte stuffing del lado host).
