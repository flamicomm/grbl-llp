# grbl-llp

**Grbl 1.1h fork with LLP (Lightweight Link Protocol) transport layer for reliable CNC communication over UART.**

Fork enfocado exclusivamente al **fresado de PCBs** con Arduino Uno/Nano (ATmega328P). El firmware elimina todo lo no esencial (spindle, coolant, cambio de herramienta) para liberar flash y dejar espacio para el protocolo LLP.

## El Problema

La comunicación serial entre Arduino y PC via USB/UART es **inestable**. Se producen pérdidas de datos que causan:

- Fresado que termina abruptamente
- Comandos G-code que no se ejecutan completamente
- Pérdida de posición del equipo
- Respuestas "ok" que nunca llegan o se corrompen

## La Solución: Protocolo LLP v3

LLP (Layered Link Protocol) v3.1.0 es un protocolo binario de transporte que garantiza integridad de datos sobre UART:

- **Tramas binarias con CRC16-CCITT**: Checksum por frame (no por byte)
- **Bytes de sincronización**: Secuencia `0xAA 0x55` para resync
- **Longitud explícita** (2 bytes, little-endian)
- **Byte stuffing**: `0xAA` en payload se escapa como `0xAA 0x00`
- **Timeout inter-byte**: 2000ms sin datos → parser resetea
- **Layer chain**: Payload `0x00` indica FinalNode (datos raw)
- **Zero-copy RX**: `llp_get_final_payload_ptr()` lee directo del buffer del parser

### Formato de trama LLP v3.1.0

```
[0xAA][0x55][LEN_L][LEN_H][PAYLOAD...][CRC_L][CRC_H]
```

### Keep-alive

El keep-alive automático (`[KA]`) fue **eliminado del main loop** para reducir flash. Solo se conserva `[KA] ALARM\r\n` en el loop crítico de alarmas (`protocol.c:255`). El host debe sondear estado mediante `?` o `#`. Si no hay respuesta, el MCU está colgado.

### Comando `#` (LLP Buffer Query)

Host envía `#` como payload LLP → Grbl responde con estadísticas:

```
buf:11 rx:150 e:0 t:2 rd:0 td:0
```

Fields: `buf:N` (planner blocks libres), `rx:N` (frames recibidos), `e:N` (errores), `t:N` (timeouts), `rd:N` (RX dropped), `td:N` (TX dropped).

## Diferencias con Grbl estándar

| Aspecto | Grbl estándar | grbl-llp |
|---|---|---|
| Protocolo de transporte | Texto plano (`\n` terminador) | LLP v3 binario con CRC16 |
| Keep-alive | No disponible | Eliminado del main loop; solo `[KA] ALARM` |
| Buffer query | No disponible | `#` → `buf:N rx:N ...` |
| Control de spindle | PWM/hardware | **Deshabilitado** (manual 12V) |
| Control de coolant | Flood/mist | **Deshabilitado** |
| Tool length offset (G43.1/G49) | Soportado | **Eliminado** |
| Planos G18/G19 | Soportados | **Mapeados a G17** (XY) |
| Paradas M0/M1/M2/M30 | Pausan/resetean programa | **No-op** (stream no se cuelga) |
| Cambio de herramienta (M6) | Soportado | **Error:20** (no soportado) |
| Probe auto-report (G38.x) | No disponible | `[PRB:x,y,z:N]` automático tras cada probe |
| Flash usado | ~93% (Grbl original) | **91.1%** UNO / **95.7%** Nano (optimizado para PCB) |
| RAM usada | ~80% | **83.3%** |

## Archivos agregados/modificados clave

| Archivo | Cambio |
|---|---|
| `src/llp_transport.c` / `src/llp_transport.h` | Recepción, TX flush non-blocking, stats, Timer2 ms counter |
| `src/llp_protocol.h` | Parser de frames LLP v3 (single-header, layer chain) |
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
python3 scripts/stream_gcode.py test_large.gcode --port /dev/ttyUSB0
```

**Características:**
- Envía cada línea como frame LLP, espera `ok` o `error:`
- Reset automático (`ctrl-x`) al inicio
- Salta M0/M1/M2/M30 (paradas intencionales)
- Continúa tras `error:` (ej: M6 no soportado → `error:20`)
- Detecta cuelgues MCU y aborta con info de línea/comando

## Scripts de prueba

| Script | Descripción |
|---|---|
| `scripts/stream_gcode.py` | Streaming G-code real vía LLP |
| `scripts/llp.py` | Librería LLP v3 (encode/decode/parser) |
| `test_short.gcode` | G-code corto para pruebas rápidas |
| `test_large.gcode` | G-code real de PCB (FlatCAM, ~5000 líneas) |

## Flash y RAM

- **Flash**: 91.1% (29394/32256 bytes UNO) / 95.7% (29394/30720 bytes Nano) con `-Os`
- **RAM**: 83.3% (1706/2048 bytes)
- **Solo `-Os`**: `-O0` desborda flash

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

## Librería cliente

- **Java**: [llp-protocol-java](https://github.com/flamicomm/llp-protocol-java)
- **Python**: `scripts/llp.py` (incluido en este repo)

## License

MIT — See [LICENSE](LICENSE)

LLP Specification v3.1.0 — Copyright © 2026 Flamingo Communications

## Créditos

- Grbl por Sungeun "Sonny" Jeon (@chamnit)
- Grbl v0.6 original por Simen Svale Skogsrud
