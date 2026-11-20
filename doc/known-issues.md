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

**Mitigación en firmware (CNC-029 G-I2):** al arrancar se validan los valores, no solo
la versión y el checksum. Si el registro es legible pero semánticamente inválido, el
firmware reporta `[MSG:EEPROM out of range; defaults in RAM, calibration kept]`,
opera con los defaults **en RAM** y **no escribe la EEPROM**: el cuelgue ya no ocurre
y la calibración del usuario no se descarta en silencio. El `$RST=$` explícito
sigue siendo la vía para descartarla a propósito.

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
- El keep-alive de depuración (`DEBUG_KEEPALIVE`) está **habilitado** por defecto en
  `src/config.h` para diagnóstico de campo (**el entorno `nano` lo apaga** con
  `-D DISABLE_DEBUG_KEEPALIVE` en `platformio.ini`, para conservar flash). Imprime
  cada 10 s, en **tres líneas**:
  ```
  [DBG:t=... s=... sp=... bf=...]
  [DBG:tx=... rx=... u=... re=... rt=...]
  [DBG:rd=... td=... ca=... a1=...]
  ```
  - `t` = uptime en ms de LLP; `s` = `sys.state`; `sp` = puntero de stack en el
    momento de imprimir; `bf` = bloques libres del planner.
  - `tx`/`rx` saturados → cuello de botella serial.
  - `u` = `UCSR0B` (bit `UDRIE0` indica TX pendiente).
  - `re`/`rt`/`rd`/`td` = errores, timeouts, drops RX y drops TX de LLP.
  - `ca` = pulsos en A0 (reset/abort) descartados por el debounce. Si crece
    durante un corte, hay EMI en ese pin aunque no llegue a reiniciar el MCU.
  - `a1` = pulsos en A1 (feed hold) descartados por el debounce de 20 ms. Si crece
    durante un corte, hay EMI en el pin de feed hold; el trabajo ya no se detiene
    por un glitch, pero conviene revisar el cableado/pull-up igual que en A0.
  - **Importante:** cada línea se corta en `LLP_TX_CHUNK_SIZE` (48 bytes, igual a
    `LLP_TX_LINE_BUF_SIZE`); una línea más larga se parte en varias tramas. La trama
    completa (peor caso `10 + 2n`) tiene que entrar en el ring de TX y hay un chequeo
    en compilación. Mantené el debug por debajo de ~45 caracteres para que cada
    mensaje salga en una sola trama.
  El costo en flash depende del build: medilo con `avr-size` antes de afirmar
  que entra. El entorno `nano` ya lo compila apagado (`-D DISABLE_DEBUG_KEEPALIVE`
  en `platformio.ini`) para no gastar ese margen; el `uno` lo conserva. Sin
  `DEBUG_KEEPALIVE` tampoco se marca el pasaje del main loop: no hay uptime, y
  `up=` en `[MSG:BOOT:...]` sale `?` (no medido, igual que `stk=?` en arranque
  frío). Tampoco hay `pk=` (pico de stack), `ca=` ni `a1=` **en las líneas
  `[DBG:...]`** (que directamente no existen); el `ca=` del banner de boot
  (`[MSG:BOOT:src=... ca=...]`) sigue apareciendo, porque es incondicional.

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
[MSG:BOOT:stk=331 lim=671]
[MSG:BOOT:src=NONE ca=0]
[MSG:BOOT:vcc=5.01]
```

| Campo | Significado |
|---|---|
| `n` | Reinicios desde el último encendido (1 en un arranque en frío) |
| `m` | `ok` = la RAM se conservó (fue un reset, no un power-on); `wipe` = arranque en frío |
| `up` | Uptime (ms) del **último pasaje del main loop** de la vida anterior; `?` si el build no lo mide (sin `DEBUG_KEEPALIVE`, como el `nano`) |
| `stk` / `lim` | Pico de stack de la vida anterior / RAM disponible para el stack |
| `src` | Último origen anotado (`NONE` si nadie llamó a `mc_reset()` antes del reset) |
| `ca` | Pulsos de A0 filtrados, preservados entre resets |
| `vcc` | Vcc estimado por el bandgap interno de 1.1 V; `?` si la lectura no es válida |

`vcc` se calcula como `Vcc = BANDGAP_MV × 1024 / ADC` (MUX del bandgap, referencia
AVcc, prescaler /128) y se mide **una vez, al arrancar**: un sag transitorio ya
recuperado puede leerse normal, y un brown-out severo puede saturar el ADC y dar
`?`. `BANDGAP_MV` (en `src/config.h`) es nominal: el bandgap real está entre 1.0 y
1.2 V, así que sin calibrarlo contra un multímetro el valor tiene ~±10 % de error.
Alcanza para orientar el diagnóstico de un reset, no para medir la fuente con
precisión.

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

El **feed hold (A1) tiene su propio debounce de 20 ms** (`CONTROL_FEED_HOLD_DEBOUNCE_MS`):
mismo esquema de armado en la ISR y confirmación en el main loop, pero en vez de
reiniciar setea `EXEC_FEED_HOLD` solo si el pin sigue LOW al vencer la ventana. Un
glitch de EMI de microsegundos ya no detiene el trabajo: se cuenta en `a1=` y la
máquina sigue cortando. Una pulsación real (> 20 ms) detiene igual que antes.

`a1=` cuenta **solo pulsos filtrados** (A1 LOW durante menos de 20 ms). Eso cambia
cómo se lee un false hold:

- **False hold con `a1=0`**: A1 estuvo LOW **≥ 20 ms** de forma continua. No fue un
  glitch corto: o alguien/botón sostuvo el pin, o hubo EMI sostenida. La máquina
  queda en `Hold:0` esperando `~` (o reset), igual que con un `!` del host.
- **`a1>0`**: hubo glitches cortos en A1 y el debounce los filtró (no detuvieron el
  trabajo). Aplicar el mismo pull-up 10 kΩ + 100 nF que en A0.

Diagnóstico diferencial: `HARD_LIMIT`, `SOFT_LIMIT` y `HOMING` **no pueden**
producir `SFR`; entran al bucle `[KA] ALARM` de `protocol_exec_rt_system()` y
esperan un reset explícito. Un `SFR` sin `[KA] ALARM` previo apunta a A0 o a LLP.

## 4c. Desborde de stack (encontrado en campo)

**Síntoma:** durante un corte, el bloque `.noinit` aparecía corrupto: `ca=` bajaba
(imposible para un contador que solo incrementa), `last_reset_source` tomaba valores
espurios (`SRC:LLP` sin que nadie mandara `0x18`), `diag_magic` se perdía y el
arranque reportaba `m=wipe` como si hubiera sido un corte de alimentación.

**Causa:** el stack desbordaba hacia `.noinit`. Con `BLOCK_BUFFER_SIZE = 12` quedaban
**323 B** de RAM libre para el stack; la cadena más profunda medía **331 B**. La
cadena es `gc_execute_line → mc_line → protocol_execute_realtime → 
protocol_exec_rt_system → report/print`: `mc_line()` espera espacio en el planner
llamando a `protocol_execute_realtime()` (`src/motion_control.c:61`), y por eso los
reportes de estado y el `[DBG]` se imprimen justo en el punto más profundo, con un
ISR de UART apilado encima.

**Consecuencia práctica:** cualquier reset cuyo origen fuera un soft reset de Grbl
se veía como `SRC:<basura>` y los resets de CPU como `m=wipe`, así que el diagnóstico
mentía justo en el caso que había que diagnosticar.

**Fix:** `BLOCK_BUFFER_SIZE` 12 → 8 (−184 B) y los arrays de `plan_buffer_line()`
pasados a `static` (−36 B de stack). Límite actual: **471 B**, pico medido con
streaming y planner lleno: **331 B**.

**Verificación:** el arranque imprime `[MSG:BOOT:stk=<pico> lim=<total>]` y el
keep-alive reporta `pk=` en vivo. Si `pk` se acerca a `lim`, hay que liberar RAM antes
de que vuelva a corromper `.noinit`. Detalle en [`buffers.md`](buffers.md).

**Gate estático (STK, CNC-029 Ola 3):** además del `pk=` de banco hay un gate en
host que falla antes de flashear. El entorno de medición `uno_stack`
(`platformio.ini`) compila con `-fstack-usage`, y el script suma el peor camino del
grafo de llamadas al `.data + .bss` del build de producción:

```bash
pio run -e uno_stack
python3 scripts/stack_gate.py
```

- **Fórmula del gate:** `pico_estático + .data + .bss > 2048 − 200` (**1848 B**)
  ⇒ falla. Los 200 B son el colchón fijado por el plan de CNC-029; `.noinit` queda
  *debajo* del stack y el script lo informa pero no lo suma (no es parte de la
  fórmula).
- **Cómo mide:** tamaños de frame de los `.su` de cada unidad de traducción + grafo
  de llamadas del ELF (`call`/`rcall`/`jmp`). Raíces: `main`/`__vectors` (contexto
  main) y cada `__vector_N` (contexto ISR). Peor caso = camino main más profundo +
  camino ISR más profundo, porque una interrupción cae sobre el frame más hondo del
  main y las ISR del AVR no se anidan (el flag `I` se limpia al entrar).
- **LTO apagado a propósito en `uno_stack`:** con LTO GCC deja los `.su` vacíos (los
  frames se cierran recién en la etapa LTRANS). Los números son una estimación del
  build sin LTO; se cotejan con el `pk=` de banco (si divergen más de 20 %, el gate
  hay que revisarlo). No cambia `uno` ni `nano`.
- **Ciclos:** el grafo tiene ciclos estáticos que no existen en runtime
  (`delay_sec → protocol_execute_realtime`, documentado en `src/nuts_bolts.c` como
  «to avoid nesting suspend loops»; `__bad_interrupt → __vectors`). El script los
  corta y los imprime: si aparece un ciclo nuevo, hay que mirarlo. `--strict-cycles`
  los convierte en error.
- **Llamadas indirectas:** `icall`/`eicall` no se resuelven estáticamente; cada sitio
  alcanzable suma 8 B de colchón (`INDIRECT_SLACK`) y se lista con `--verbose`.
- **Frames no estáticos:** un frame `dynamic` alcanzable hace fallar el gate (una VLA
  rompe la cuenta). Hoy el único `dynamic` (`print_uint8_base2_ndigit`) es código
  muerto y el script lo informa sin contarlo.


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

Los contadores son de 16 bits y **saturan en 65535**: no dan la vuelta. Un `rx:65535`
clavado es "saturado", no un link muerto; si necesitás números frescos, reiniciá la
placa. Antes de CNC-029 Ola 3 daban la vuelta a las ~2 h de streaming y el comando
parecía "bajar" solo.

## 7. `$1` (stepper idle delay) y reset desde ISR

**Qué pasa.** Un reset pedido **desde interrupción** con la máquina en movimiento
—`0x18` en un frame LLP (`src/llp_transport.c`, `CMD_RESET`) o un hard limit
(`src/limits.c`)— ejecuta `mc_reset()` en contexto de ISR
(`src/motion_control.c`). Con movimiento en curso, `mc_reset()` setea la alarma y
llama a `st_go_idle()`, y `st_go_idle()` (`src/stepper.c`) espera el dwell de `$1`
**antes** de deshabilitar los drivers:

```c
delay_ms(settings.stepper_idle_lock_time);
```

Ese `delay_ms()` corre con las interrupciones globales deshabilitadas (estamos
dentro de una ISR): durante `$1` ms no corre el main loop **ni la ISR de RX de la
UART**. Con el default `$1=25` son hasta **~25 ms** y, a 115200 baud (~11.5 B/ms),
unos **~288 B** que el host ya puso en el cable se pierden: el buffer de recepción
del AVR tiene 2 bytes y no hay flow control. En el canal 1 (R1) lo absorben el ACK
y la retransmisión; el stream del canal 0 (`ok`/`error`) no tiene recuperación, así
que el host ve un hueco aunque la máquina ya esté reseteada.

**`$1=255` no arregla este caso.** Con `$1=255` (`0xFF`) Grbl salta el dwell en el
fin de ciclo normal y deja los drivers siempre habilitados (es lo recomendable para
streaming con paradas frecuentes), **pero en el camino de reset el flag de alarma
ya está seteado** (`sys_rt_exec_alarm`) y la condición de `st_go_idle()` se cumple
igual: el `delay_ms(255)` bloquea **~255 ms** en la ISR (~2 900 B perdidos).

| `$1` | Fin de ciclo normal | Reset desde ISR en movimiento |
|---|---|---|
| `25` (default) | dwell de 25 ms; los drivers se deshabilitan | **~25 ms** de bloqueo |
| `255` | sin dwell; drivers siempre habilitados | **~255 ms** de bloqueo |

**Recomendación.** Para streaming con paradas frecuentes, `$1=255` evita el ciclo
habilitar/deshabilitar drivers y el dwell por parada. La ventana del reset la fija
`$1` en sentido contrario: si el banco mide pérdidas en un reset en movimiento, la
palanca es **bajar** `$1` (p. ej. `$1=1`, ~1 ms). Sólo el reset que corre en
**contexto de ISR** tiene esta ventana: el byte `0x18` en un frame LLP y los hard
limits (en `LIMIT_INT_vect`, o en `WDT_vect` con el debounce por watchdog). A0 (se
confirma en el main loop tras el debounce), los soft limits, `$C` off, `$RST=*` y el
homing fail llaman a `mc_reset()` desde el main loop con las IRQs habilitadas: el
`delay_ms()` frena el main loop, pero la ISR de RX sigue corriendo, así que los bytes
no se pierden en el hardware (a lo sumo saturan el ring y se cuentan en `rd:`).

**No diferido en esta ola (G-M3 paso 2).** Mover `st_go_idle()` al main loop
eliminaría la ventana, pero cambia el orden en que se deshabilitan los drivers tras
matar el movimiento; queda como opcional **solo si el banco lo pide**
(`docs/plans/CNC-029-grbl-llp.md` del workspace, Ola 2).

## 8. Timer2 cuenta 1.024 ms, no 1 ms (deriva +2,4 %)

`llp_transport_init()` (`src/llp_transport.c`) arranca Timer2 en modo **normal** con
prescaler 1/64: `16 MHz / 64 / 256 = 976.56 Hz`, o sea **un tick cada 1,024 ms**. El
contador `llp_ms_counter` se documenta como "ms" pero no lo es: cada unidad vale
1.024 ms. No es un bug nuevo y **no se migra a CTC** (el plan de CNC-029 lo deja
explícitamente fuera): la deriva es del 2,4 %, no afecta el movimiento (los pasos los
genera Timer1) y sólo estira plazos de diagnóstico y de protocolo.

Lo que sí hay que tener en la cabeza al leer un log o ajustar un umbral: **todo plazo
medido con `llp_transport_get_ms()` es un 2,4 % más largo que su valor nominal**.

| Plazo | Dónde | Nominal | Real |
|---|---|---|---|
| Timeout inter-byte del parser v4 (`LLP_RX_TIMEOUT_MS`) | `src/llp_transport.h` | 2000 ms | **2048 ms** |
| Espera por lugar en el ring de TX (`LLP_TX_WAIT_MS`) | `llp_tx_emit()` | 10 ms | 10,24 ms |
| Debounce de A0 (`CONTROL_RESET_DEBOUNCE_MS`) | `src/system.c` | 100 ms | 102,4 ms |
| Debounce de A1 (`CONTROL_FEED_HOLD_DEBOUNCE_MS`) | `src/system.c` | 20 ms | 20,48 ms |
| `[KA] ALARM` (loop crítico de alarma) | `src/protocol.c` | 1000 ms | **1024 ms** |
| `[DBG:...]` del keep-alive | `src/protocol.c` | 10 s | **10,24 s** |
| `t=` de `[DBG:...]` y `up=` de `[MSG:BOOT:...]` (uptime) | `src/protocol.c` | 1 min | 61,4 s |

Consecuencias prácticas:

- Un `t=` de 60000 en el log son **61,4 s** reales, no 60 s. Para comparar `up=` contra
  el último `t=` visto (reset vs cuelgue) la diferencia de escala es irrelevante, pero
  no lo uses como cronómetro.
- El timeout de `LLP_RX_TIMEOUT_MS` es de 2048 ms reales; un host que retransmite "a
  los 2 s" sigue entrando, y un gap de 2,02 s también cuenta como timeout. No hay
  margen fino que ajustar acá.
- Los debounces de A0/A1 filtran un poco más de lo que dicen sus nombres (2,4 % más):
  no cambia la decisión de hardware (pull-up + 100 nF), sólo no esperes 100,0 ms.
- `#`/`?` no dependen de este contador; los contadores de stats no se ven afectados.

**Si alguna vez hace falta exactitud** (no hoy): Timer2 en modo CTC con `OCR2A` a 249
da 1,0000 ms exactos y el vector pasa a ser `TIMER2_COMPA_vect`. Requiere tocar la
ISR y el `TCCR2A`, y no hay ninguna necesidad medida que lo justifique.
