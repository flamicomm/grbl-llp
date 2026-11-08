# grbl-llp

**Grbl 1.1h fork with LLP (Lightweight Link Protocol) transport layer for reliable CNC communication over UART.**

Fork enfocado exclusivamente al **fresado de PCBs** con Arduino Uno/Nano (ATmega328P). El firmware elimina todo lo no esencial (spindle, coolant, cambio de herramienta) para liberar flash y dejar espacio para el protocolo LLP.

## El Problema

La comunicación serial entre Arduino y PC via USB/UART es **inestable**. Se producen pérdidas de datos que causan:

- Fresado que termina abruptamente
- Comandos G-code que no se ejecutan completamente
- Pérdida de posición del equipo
- Respuestas "ok" que nunca llegan o se corrompen

## La Solución: Protocolo LLP v4

LLP (Lightweight Link Protocol) v4.0.0 es un protocolo binario de transporte que garantiza integridad de datos sobre UART:

- **Tramas binarias con CRC16-CCITT**: Checksum por frame (no por byte)
- **Bytes de sincronización**: Secuencia `0xAA 0x55` para resync
- **Header fijo de 6 bytes**: longitud (1 B) y canal (1 B)
- **Byte stuffing**: `0xAA` en el resto de la trama se escapa como `0xAA 0x00`
- **Timeout inter-byte**: 2000 ms sin datos → parser resetea
- **Parser con buffer externo**: el payload se escribe directo en RAM del consumidor

### Formato de trama LLP v4.0.0

```
[0xAA][0x55][LEN][CHANNEL][PAYLOAD...][CRC_L][CRC_H]
```

`CRC16-CCITT` cubre `MAGIC + LEN + CHANNEL + PAYLOAD` y va little-endian.
La especificación completa vive en el repo `llp-protocol`
(`docs/SPEC-v4.md`); el header vendorizado es `src/llp_v4.h`.

### Keep-alive

El keep-alive automático (`[KA]`) fue **eliminado del main loop** para reducir flash. Solo se conserva `[KA] ALARM\r\n` en el loop crítico de alarmas (`protocol.c:284`). El host debe sondear estado mediante `?` o `#`. Si no hay respuesta, el MCU está colgado.

### Comando `#` (LLP Buffer Query)

Host envía `#` como payload LLP → Grbl responde con estadísticas:

```
buf:11 rx:150 e:0 t:2 rd:0 td:0
```

Fields: `buf:N` (planner blocks libres), `rx:N` (frames recibidos), `e:N` (errores), `t:N` (timeouts), `rd:N` (RX dropped), `td:N` (TX dropped).

## Diferencias con Grbl estándar

| Aspecto | Grbl estándar | grbl-llp |
|---|---|---|
| Protocolo de transporte | Texto plano (`\n` terminador) | LLP v4 binario con CRC16 |
| Keep-alive | No disponible | Eliminado del main loop; solo `[KA] ALARM` |
| Buffer query | No disponible | `#` → `buf:N rx:N ...` |
| Control de spindle | PWM/hardware | **Deshabilitado** (manual 12V) |
| Control de coolant | Flood/mist | **Deshabilitado** |
| Tool length offset (G43.1/G49) | Soportado | **Eliminado** |
| Planos G18/G19 | Soportados | **Mapeados a G17** (XY) |
| Paradas M0/M1/M2/M30 | Pausan/resetean programa | **No-op** (stream no se cuelga) |
| Cambio de herramienta (M6) | Soportado | **Error:20** (no soportado) |
| Probe auto-report (G38.x) | No disponible | `[PRB:x,y,z:N]` automático tras cada probe |
| Flash usado | ~93% (Grbl original) | **84.2%** UNO / **88.4%** Nano (optimizado para PCB) |
| RAM usada | ~80% | **67.2%** |

## Archivos agregados/modificados clave

| Archivo | Cambio |
|---|---|
| `src/llp_transport.c` / `src/llp_transport.h` | RX por ISR al parser v4, TX en streaming al ring (sin frame buffer), stats, Timer2 ms counter |
| `src/llp_v4.h` | Framing LLP v4.0.0 (single-header, parser + encoder en streaming) |
| `src/serial.c` / `src/serial.h` | `volatile` en buffer heads, integración LLP con ISR serial |
| `src/protocol.c` | Keep-alive eliminado del main loop; `[KA] ALARM` preservado en alarm loop; comando `#` con LLP stats |
| `src/spindle_control.c` | Stubs vacíos (no spindle I/O) |
| `src/coolant_control.c` | Stubs vacíos (no coolant I/O) |
| `src/gcode.c` | TLO eliminado, G18/G19→G17, spindle/coolant no-op, M0/M1/M2/M30 no-op |
| `src/report.c` | Spindle/coolant removido de `$G`; `report_probe_parameters()` implementado con auto-report tras G38.x |
| `src/config.h` | `MESSAGE_PROBE_COORDINATES` **habilitado** (auto-report probe); `CHECK_LIMITS_AT_INIT`, `REPORT_FIELD_*`, `ENABLE_BUILD_INFO_WRITE_COMMAND` deshabilitados |

## Hardware

- **Arduino Uno** (ATmega328P) — soporte principal
- **Arduino Nano** (ATmega328P, old bootloader) — también soportado
- CNC Shield v3.x (opcional)
- Serial a 115200 baud
- No spindle control (manual 12V)
- No coolant
- Low speed: < 1000mm/min para PCB prototyping

### Pin mapping (rama `dev`, pines originales)

| Función | Pin |
|---|---|
| X_STEP | D2 |
| Y_STEP | D3 |
| Z_STEP | D4 |
| X_DIR | D5 |
| Y_DIR | D6 |
| Z_DIR | D7 |
| STEPPERS_DISABLE | D8 |
| X_LIMIT | D9 |
| Y_LIMIT | D10 |
| Z_LIMIT | D11 |

### Pin mapping (rama `custom`, Z remapeado)

En la rama `custom`, `Z_STEP` se movió de D4 (defectuoso en algunos Arduinos) a **A4** (PORTC), libre tras eliminar coolant.

| Función | Pin |
|---|---|
| X_STEP | D2 |
| Y_STEP | D3 |
| **Z_STEP** | **A4** |
| Z_DIR | D7 (sin cambio) |

## Compilación y subida

```bash
# Arduino Uno
pio run -e uno                                          # Compilar
pio run -e uno -t upload --upload-port /dev/ttyUSB0     # Subir

# Arduino Nano (old bootloader)
pio run -e nano                                         # Compilar
pio run -e nano -t upload --upload-port /dev/ttyUSB0  # Subir
```

## Streaming de G-code

El script `scripts/stream_gcode.py` envía archivos G-code completos al Arduino vía LLP:

```bash
python3 scripts/stream_gcode.py scripts/gcode_examples/test_large.gcode --port /dev/ttyUSB0
```

**Características:**
- Envía cada línea como frame LLP, espera `ok` o `error:`
- Reset automático (`ctrl-x`) al inicio
- Salta M0/M1/M2/M30 (paradas intencionales)
- Continúa tras `error:` (ej: M6 no soportado → `error:20`)
- Detecta cuelgues MCU y aborta con info de línea/comando

## Canal de comandos confiable (perfil R1)

El wire format v4 garantiza que no llegue una trama corrupta, pero **no** que una
trama llegue. Si se pierde un comando, el host queda esperando el `ok` para
siempre, y reintentar a ciegas puede ejecutar el movimiento dos veces.

El perfil **R1** (normativo en [`llp-spec`](https://github.com/flamicomm/llp-spec/blob/main/docs/RELIABILITY.md),
implementación en `src/llp_v4_rel.h`) agrega una capa confiable sobre dos canales:

```
COMMAND (canal 1):  [SEQ][DATA...]      host -> MCU
ACK     (canal 2):  [SEQ]               MCU  -> host
```

- **Ventana 1**: un comando en vuelo. El host no manda el siguiente hasta el ACK.
- **Dedup**: el MCU guarda `last_seq`; un `SEQ` repetido se re-ACKea y **se
  descarta** (`llp4_rel_rx_accept()`), así un ACK perdido nunca ejecuta dos veces.
- **Aceptación atómica**: si los bytes no entran completos en el ring de línea
  (`RX_BUFFER_SIZE = 80`), **no se consume el `SEQ` ni se ACKea**; la
  retransmisión lo reintenta después. La presión de buffer se convierte en
  retransmisión, no en pérdida.
- **ACK desde el main loop** (`llp_transport_poll()`, llamado en
  `protocol_execute_realtime()`): el ring de TX tiene un solo escritor, la ISR
  solo marca `llp_ack_pending`.
- El stream grbl normal (`ok`, status, `[DBG]`, `[MSG]`) sigue por el **canal 0**
  sin cambios, y los realtime (`?`, `!`, `~`, `0x18`) también.

El keep-alive reporta la salud del enlace en una cuarta línea:

```
[DBG:ack=1234 dup=0 busy=0]
```

`dup` > 0 significa que hubo retransmisiones (ACK perdido) y que el dedup las
absorbió; `busy` cuenta comandos diferidos por falta de espacio en el ring.

En Python, `scripts/llp_rel.py` es la copia vendorizada de la referencia y
`scripts/llp_link.py` el enlace de alto nivel que usan `stream_gcode.py` y
`test_r1.py`. El enlace solo declara "sin R1" cuando los primeros
`FALLBACK_CONFIRM_COMMANDS` comandos no se ACKean **y** nunca hubo un ACK en la
sesión; ahí reenvía esos comandos pendientes por el canal 0 y sigue por ahí, así
el mismo script sirve para ambos firmwares. Un enlace que ya ACKeó nunca se
degrada por comandos sueltos perdidos. `python3 scripts/llp_link.py --self-test`
ejercita entrega limpia, ACK perdido, comando perdido, auto-detección, boot lento
y drenaje, contra un MCU simulado.

Para el banco hay inyección de fallas (nunca en una máquina cortando):
`#define LLP_TEST_DROP_CH1 20` tira 1 comando de cada 20 y
`#define LLP_TEST_DROP_ACK 20` tira 1 ACK de cada 20. El arnés
`scripts/test_r1.py` habla CH1/CH2 directo por el puerto serie y verifica
entrega limpia, duplicados, `SEQ` repetido con otro payload y el wrap 255→0.
Abrir el puerto resetea el MCU y el banner de arranque puede tardar ~3,9 s
(F-013/F-014), así que antes del handshake el arnés **drena** lo que llega
durante `--wait-boot` (4,5 s por defecto) e informa cuántos bytes descartó; ese
conteo es también evidencia del replay del CH340 (F-018).

## Scripts de prueba

| Script | Descripción |
|---|---|
| `scripts/stream_gcode.py` | Streaming G-code real vía LLP (usa R1: reintenta en vez de abortar) |
| `scripts/llp.py` | Librería LLP v4 (encode/decode/parser) |
| `scripts/llp_rel.py` | Perfil R1 (`Sender`/`Receiver`) — copia vendorizada de la referencia |
| `scripts/llp_link.py` | Enlace serie con R1: ACK, hello y auto-detección de firmware sin R1 (`--self-test` sin hardware) |
| `scripts/test_r1.py` | Banco del perfil R1 (ACK, dedup, retransmisión) por puerto serie |
| `test_short.gcode` | G-code corto para pruebas rápidas |
| `test_large.gcode` | G-code real de PCB (FlatCAM, ~5000 líneas) |

## Flash y RAM

Medido con `avr-size` sobre el build actual (`pio run -e uno` / `-e nano`, `-Os`):

- **UNO**: 84.2% (27148/32256 bytes) · **Nano**: 88.4% (27148/30720 bytes)
- **RAM**: 67.2% (1377/2048 bytes)
- **Solo `-Os`**: `-O0` desborda flash

El espacio libre no es casual: `src/config.h` tiene switches para compilar fuera G-code
que esta máquina no usa. Los ahorros están medidos con `avr-size` (ver el bloque
"Flash budget" del propio `config.h`):

| Switch | Ahorro | Qué desactiva |
|---|---:|---|
| `ENABLE_ARC_SUPPORT` (apagado) | ~3220 B | G2/G3: parsing, `mc_arc()` y libm (`atan2`/`sqrt`) |
| `ENABLE_NONMODAL_EXTRA` (apagado) | ~500 B | G10, G28/G30, G28.1/G30.1 y G53 (G92 se conserva) |
| `N_STARTUP_LINE 0` | ~290 B | startup blocks y comando `$N` |
| `DEBUG_KEEPALIVE` (encendido) | ~750 B | líneas `[DBG:...]` cada 10 s |

`DEBUG_KEEPALIVE` está encendido a propósito para la validación del perfil R1; apagarlo
deja el UNO en ~82% de flash. `$RST=*`, `$RST=$` y `$RST=#` se conservan: el procedimiento
de reseteo de EEPROM de más abajo depende de `$RST=$`.

Con `ENABLE_ARC_SUPPORT` apagado, una línea G2/G3 responde `error:20` (no hay
interpolación de arcos). El host no las genera: pcb2gcode no emite G02/G03, el generador
de base de madera emite G0/G1 y el autolevel usa G0/G1/G38.3.

## Importante: Reseteo de EEPROM al flashear

**Siempre ejecutar `$RST=$` después de flashear este firmware en un Arduino nuevo o usado.** Los datos previos en EEPROM (de Grbl stock, otro fork, o un board diferente) pueden contener floats inválidos que lockean el planificador al primer movimiento.

**Síntomas de EEPROM corrupta:**
- MCU se congela al ejecutar cualquier `G0`/`G1` (sin importar el eje)
- `$$` muestra valores imposibles como `-2147483.648` o `0.000` en max_rate/acceleration
- Faltan líneas en `$$` (ej: `$101`, `$120` ausentes)

**Solución:**
```bash
echo '$RST=$' | python3 -c "
import sys; sys.path.insert(0,'scripts')
import serial, time, llp
s = serial.Serial('/dev/ttyUSB0', 115200, timeout=3)
s.setDTR(0); time.sleep(0.1); s.setDTR(1); time.sleep(2)
s.read_all()
frame = llp.encode(b'\$RST=\$\n')
s.write(frame); time.sleep(1); s.close()
"
```

Después verificar con `$$` y reconfigurar `$100`–`$132` para tu máquina.

## Issues conocidos

### Cuelgue en movimiento tras flashear

El cuelgue al primer movimiento (antes reportado como "Z-axis bug" o "G2/G3 bug") fue rediagnosticado como **corrupción de EEPROM**. Ver sección "Reseteo de EEPROM" arriba. Si el board se cuelga después de `$RST=$`, puede haber un problema de hardware real.

### Diagnóstico de resets

`DEBUG_KEEPALIVE` está habilitado por defecto y emite cada 10 s (en tres líneas,
cada una dentro de una sola trama LLP):

```
[DBG:t=... s=... sp=... bf=...]
[DBG:tx=... rx=... u=... re=... rt=...]
[DBG:rd=... td=... ca=... pk=...]
```

En cada **reset de CPU** imprime además, antes del banner:

```
[MSG:BOOT:n=... m=ok|wipe up=...]
[MSG:BOOT:stk=... lim=...]
[MSG:BOOT:src=NONE|A0|LLP|... ca=...]
[MSG:BOOT:vcc=<x.xx>|?]
```

`n` cuenta reinicios desde el último encendido, `m` indica si la RAM se conservó
(reset) o hubo power-on, y `up` es el uptime del último pasaje del main loop de la
vida anterior (sirve para distinguir un reset inmediato de un cuelgue previo).
`vcc` estima la alimentación con el bandgap interno de 1.1 V
(`Vcc = BANDGAP_MV × 1024 / ADC`, ver `BANDGAP_MV` en `src/config.h`) **en el
momento del arranque**: un sag transitorio ya recuperado puede leerse normal y un
brown-out severo puede saturar el ADC y dar `?`. Sin calibrar `BANDGAP_MV` contra
un multímetro el valor tiene un error de ~±10 %, suficiente para orientar el
diagnóstico; imprime `?` si la lectura no es válida.

Se agrega `stk=` (pico de stack de la vida anterior) y `lim=` (RAM disponible para el
stack). Si `stk` se acerca a `lim`, el stack puede desbordar hacia `.noinit` y
corromper todo este diagnóstico; el keep-alive reporta el pico en vivo como `pk=`.

**Ojo:** el bootloader optiboot de Arduino limpia `MCUSR` antes de saltar al
sketch, así que en un Uno/Nano estándar el sketch **siempre** reporta
`[MSG:RST:SFR]`, incluso ante brown-out o reset externo. Los mensajes
`BOR/EXT/WDR/POR` sólo aparecen con un bootloader que no limpie `MCUSR`.

Cuando hubo un **soft reset** de Grbl (re-init en el lugar, sin reset de CPU) se
imprime `[MSG:RST:SRC:<origen>]` y **no** aparece `[MSG:BOOT]`. Orígenes:
`LLP` (byte 0x18 en un frame), `A0` (pin de abort tras el debounce de 100 ms),
`RST` (`$RST`), `HARD_LIMIT`/`SOFT_LIMIT`, `HOMING`, `OTHER`.

El campo `ca=` cuenta los pulsos de A0 descartados por el debounce (EMI) y ahora
se conserva entre resets (vive en `.noinit`). Detalle y mitigaciones en
[`doc/known-issues.md`](doc/known-issues.md).

## Documentación técnica interna

- [`doc/buffers.md`](doc/buffers.md) — tamaños de buffer, dependencias y trampa del bloque comentado de `config.h`.
- [`doc/known-issues.md`](doc/known-issues.md) — rediagnóstico del "bug Z", `ALARM:98`, keep-alive, frames descartados.
- [`doc/changelog-workflow.md`](doc/changelog-workflow.md) — hooks `post-commit`/`post-merge` y flujo de PR.
- [`AGENTS.md`](AGENTS.md) — reglas normativas para agentes; [`src/AGENTS.md`](src/AGENTS.md) — init, ISRs y flags de firmware.

## Librería cliente

- **Java**: [llp-protocol-java](https://github.com/flamicomm/llp-protocol-java)
- **Python**: `scripts/llp.py` (incluido en este repo)

## License

MIT — See [LICENSE](LICENSE)

LLP Specification v4.0.0 — Copyright © 2026 Flamingo Communications

## Créditos

- Grbl por Sungeun "Sonny" Jeon (@chamnit)
- Grbl v0.6 original por Simen Svale Skogsrud
