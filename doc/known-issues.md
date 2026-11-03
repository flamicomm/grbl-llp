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
  `src/config.h` para diagnóstico de campo. Imprime cada 10 s, en **tres líneas**:
  ```
  [DBG:t=... s=... sp=... bf=...]
  [DBG:tx=... rx=... u=... re=... rt=...]
  [DBG:rd=... td=... ca=...]
  ```
  - `t` = uptime en ms de LLP; `s` = `sys.state`; `sp` = puntero de stack en el
    momento de imprimir; `bf` = bloques libres del planner.
  - `tx`/`rx` saturados → cuello de botella serial.
  - `u` = `UCSR0B` (bit `UDRIE0` indica TX pendiente).
  - `re`/`rt`/`rd`/`td` = errores, timeouts, drops RX y drops TX de LLP.
  - `ca` = pulsos en A0 (reset/abort) descartados por el debounce. Si crece
    durante un corte, hay EMI en ese pin aunque no llegue a reiniciar el MCU.
  - **Importante:** cada línea impresa tiene que entrar en una sola trama LLP.
    `llp_transport_tx_byte()` corta en `LLP_TX_CHUNK_SIZE` (`LLP_MAX_PAYLOAD - 1`,
    o sea 63 bytes con el default 64) porque `llp_build_final_payload()` antepone
    el byte de layer. Una línea más larga se parte en varias tramas y, si el chunk
    llegara a `LLP_MAX_PAYLOAD`, la cabecera se descarta en silencio. Mantené las
    líneas de debug por debajo de ~60 caracteres.
  El costo en flash depende del build: medilo con `avr-size` antes de afirmar
  que entra. En el Nano el margen es mínimo: si no entra, deshabilitarlo con
  `// #define DEBUG_KEEPALIVE`.

## 4b. Reset del MCU: `[MSG:RST:...]` y `[MSG:BOOT:...]`

**El bootloader de Arduino (optiboot) lee y limpia `MCUSR` antes de saltar al
sketch** (`optiboot.c`: `ch = MCUSR; MCUSR = 0;`). Consecuencia: en un Uno/Nano
con optiboot el sketch **siempre** lee `MCUSR == 0` y reporta `SFR`, incluso en un
brown-out o un reset externo. Los mensajes `BOR/EXT/WDR/POR` son inalcanzables en
esas placas; no sirven para diagnosticar.

Por eso el firmware mantiene diagnóstico en `.noinit` (no lo inicializa `crt0`,
así que sobrevive a un reset de CPU, no a un corte de alimentación). En cada
reinicio de CPU imprime, antes del banner de Grbl:

```
[MSG:BOOT:n=12 m=ok up=1234567]
[MSG:BOOT:src=NONE ca=0]
```

| Campo | Significado |
|---|---|
| `n` | Reinicios desde el último encendido (1 en un arranque en frío) |
| `m` | `ok` = la RAM se conservó (fue un reset, no un power-on); `wipe` = arranque en frío |
| `up` | Uptime (ms) del **último pasaje del main loop** de la vida anterior |
| `src` | Último origen anotado (`NONE` si nadie llamó a `mc_reset()` antes del reset) |
| `ca` | Pulsos de A0 filtrados, preservados entre resets |

Cómo leerlo:

- **`[MSG:BOOT]` presente** ⇒ hubo un reset de CPU (crt0 volvió a correr). Si `m=ok`,
  la RAM sobrevivió (brown-out, reset externo, watchdog o salto al vector).
- **`[MSG:BOOT]` ausente** y `[MSG:RST:SRC:X]` presente ⇒ fue un re-init en el lugar
  (soft reset de Grbl), `X` nombra al que llamó a `mc_reset()`.
- `up` muy anterior al último `[DBG:t=...]` visto por el host ⇒ el main loop dejó de
  correr antes del reset (cuelgue). `up` ≈ el último `t` ⇒ el reset fue inmediato.

Causa reportada por `MCUSR` (sólo útil si el bootloader **no** limpia `MCUSR`, p.
ej. con un bootloader propio):

| Mensaje | Significado |
|---|---|
| `[MSG:RST:BOR]` | Brown-out (caída de tensión) |
| `[MSG:RST:EXT]` | Reset externo (pin RESET / botón) |
| `[MSG:RST:WDR]` | Watchdog |
| `[MSG:RST:POR]` | Power-on |
| `[MSG:RST:SFR]` | **Soft reset** (software) o reset con `MCUSR` ya limpio |

Cuando la causa es `SFR`, se agrega el origen que anotó el que llamó a `mc_reset()`:

| `[MSG:RST:SRC:...]` | Origen |
|---|---|
| `LLP` | Byte `0x18` (CMD_RESET) dentro de un frame LLP |
| `A0` | Pin A0 (Reset/Abort del shield) confirmado tras `CONTROL_RESET_DEBOUNCE_MS` |
| `RST` | Comando `$RST` |
| `HARD_LIMIT` / `SOFT_LIMIT` | Alarma de límites |
| `HOMING` | Fallo de homing |
| `OTHER` | Otro `mc_reset()` sin anotar |

El debounce de A0 está en **100 ms** (`CONTROL_RESET_DEBOUNCE_MS`) y exige nivel
**bajo continuo**: la ISR arma el temporizador en el flanco de bajada y lo cancela
si el pin vuelve a HIGH, así que un pulso corto nunca reinicia el MCU (se cuenta
en `ca=`). Si el origen es `A0` y no hay botón de abort conectado:

1. Pull-up externo de 10 kΩ + 100 nF a GND en A0 (lo más robusto), o
2. desconectar A0 del shield, o
3. compilar con `#define DISABLE_CONTROL_RESET_PIN` (`src/config.h`), que saca A0
   del mask de control: sin PCINT y sin reset. Feed hold (A1) y cycle start (A2)
   siguen funcionando.

Diagnóstico diferencial: `HARD_LIMIT`, `SOFT_LIMIT` y `HOMING` **no pueden**
producir `SFR`; entran al bucle `[KA] ALARM` de `protocol_exec_rt_system()` y
esperan un reset explícito. Un `SFR` sin `[KA] ALARM` previo apunta a A0 o a LLP.

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
