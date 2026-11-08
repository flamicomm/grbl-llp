# AGENTS.md — grbl-llp

Firmware Grbl 1.1h para ATmega328P con transporte LLP. **Este archivo es normativo**:
solo invariantes y comandos. La referencia (protocolo, diferencias con Grbl, pinout,
issues detallados) vive en `README.md` y `doc/`.

## Regla de oro

**Después de flashear, siempre `$RST=$` y verificar con `$$`.** La EEPROM puede
traer floats inválidos de otro build y colgar el planificador al primer movimiento.
Procedimiento y síntomas exactos: `README.md` → "Reseteo de EEPROM al flashear".

## Comandos

```bash
pio run -e uno                                        # compilar UNO
pio run -e nano                                       # compilar Nano (old bootloader)
pio run -e uno -t upload --upload-port /dev/ttyUSB0    # subir (ESP en modo transparente)
```

Medir tamaño real (obligatorio antes de afirmar que algo "cabe"):

```bash
~/.platformio/packages/toolchain-atmelavr/bin/avr-size --format=avr --mcu=atmega328p \
  .pio/build/uno/firmware.elf
```

## Invariantes

- **Solo `-Os`.** `-O0` desborda la flash: nunca lo dejes en un commit.
- **Headroom: ~5.0 KB en UNO / ~3.5 KB en Nano.** Es el resultado de apagar las features
  opcionales de `src/config.h` (`ENABLE_ARC_SUPPORT`, `ENABLE_NONMODAL_EXTRA`,
  `N_STARTUP_LINE 0`). Antes de agregar código, medí flash y RAM con `avr-size`;
  no cites porcentajes de memoria: cambian con cada commit, da el comando.
- **Timer2 está tomado** por el contador de ms de LLP (`src/llp_transport.c`). No lo reuses.
- **Buffers:** no cambies tamaños sin leer `doc/buffers.md` (hay dependencias entre
  `LINE_BUFFER_SIZE`, `LLP_RX_PAYLOAD_SIZE` y `TX_BUFFER_SIZE`).
- **Sin spindle ni coolant:** `spindle_control.c` y `coolant_control.c` son stubs.
- **Firmware del ESP8266 (fuera de este repo):** nunca uses `Serial.print()`; ese UART
  está permanentemente conectado al Arduino.

## Arquitectura (3 capas)

1. ESP8266 en modo transparente sobre `/dev/ttyUSB0` ↔ UART 115200 con frames LLP.
2. `src/llp_transport.c` (bytes, Timer2, flush no bloqueante) + `src/llp_v4.h`
   (framing v4.0.0, CRC16, canal, byte stuffing).
3. `src/serial.c` adapta las ISR UART a LLP; `src/protocol.c` consume líneas desframadas.

Perfil opcional **R1** (`src/llp_v4_rel.h`): canal 1 = comando confiable
`[SEQ][DATA]`, canal 2 = ACK `[SEQ]`, ventana 1 con dedup por `SEQ`. Normativo en
`llp-spec/docs/RELIABILITY.md`; invariantes de integración en `src/AGENTS.md`.
El canal 0 (stream grbl) queda igual.

Formato de trama, comando `#`, keep-alive y diferencias con Grbl estándar: `README.md`.

## Git

- `master` y `dev` son **ramas protegidas**: solo PR. No hagas commits ni amends en ellas.
- Prefijos de commit que el changelog clasifica: `feat|add|feature|implement|create`,
  `change|update|refactor|modify|optimize|improve`, `fix|bug|correct|resolve`,
  `remove|delete|drop`. Cualquier otro prefijo cae en "Unclassified".
- Hooks de changelog (`post-commit`/`post-merge`): `doc/changelog-workflow.md`.

## Reglas de código (C / AVR)

- C99, sin heap (`malloc` prohibido), sin C++ fuera de los `.cpp` ya existentes.
- `static` para todo lo privado del módulo; prototipo en el `.h` solo si se exporta.
- Variables compartidas con una ISR: **`volatile` obligatorio** (ver `serial_rx_buffer_head`).
- Strings de salida en `PROGMEM` / `printPgmString()`, nunca literales en RAM.
- Los `src/*.bak` son respaldos: no se editan, no se compilan, no se citan como evidencia.
- `src/config.h` contiene un bloque comentado con **valores viejos**; la definición activa
  puede estar en otro header (p. ej. `BLOCK_BUFFER_SIZE` → `src/planner.h`). Verifica con grep.

## Diagnóstico rápido

| Síntoma | Causa más probable | Acción |
|---|---|---|
| MCU se cuelga al primer `G0`/`G1` | EEPROM corrupta | `$RST=$` y luego `$$` |
| `$$` con `-2147483.648` o líneas faltantes | EEPROM corrupta | `$RST=$` |
| `ALARM:98` | No existe en Grbl (solo 1–10); sospecha corrupción de RAM/stack | revisar margen de stack, no "arreglar" el 98 |
| No responde nada tras flashear | ESP sin modo transparente o baud incorrecto | `doc/known-issues.md` |
| Frames LLP perdidos (`td:`/`rd:` crecen) | Buffer chico o flush descartando | `#` + `doc/buffers.md` |
| `[MSG:RST:SFR]` + `[MSG:RST:SRC:LLP]` | Byte `0x18` (reset) en un frame LLP | revisar ruido UART / comando de emergencia |
| `[MSG:RST:SFR]` + `[MSG:RST:SRC:A0]` | Pin A0 disparado por EMI (sin botón) | pull-up 10k + 100nF en A0, o desconectar A0 |
| `[MSG:BOOT:n=...]` tras reiniciarse | Reset de CPU; optiboot limpia `MCUSR`, por eso igual dice `SFR` | comparar `up=` con el último `[DBG:t=...]` (cuelgue vs reset inmediato) |
| `[MSG:RST:SRC:HARD_LIMIT/SOFT_LIMIT]` | Alarma de límites | revisar recorrido / `$20`/`$21` |
| `[DBG:... sp=...]` con `sp` muy bajo | Stack profundo | revisar buffers/stack (`doc/buffers.md`) |
| `[DBG:... ca=...]` creciendo durante un corte | EMI en A0 (pulsos filtrados por el debounce) | pull-up 10 k + 100 nF, desconectar A0 o `DISABLE_CONTROL_RESET_PIN` |
| `[DBG:ack=... dup=...]` con `dup` creciendo | El host retransmite: se perdió un ACK o un comando | normal si es esporádico (el dedup lo absorbe); si crece siempre, revisar UART/EMI |
| `[DBG:... busy=...]` creciendo | Comandos diferidos por falta de espacio en el ring de línea | revisar `RX_BUFFER_SIZE` / ritmo del host (`doc/buffers.md`) |

## Documentación

| Archivo | Contenido |
|---|---|
| `README.md` | protocolo LLP, diferencias con Grbl, pinout, compilación, scripts, EEPROM |
| `doc/buffers.md` | tamaños de buffer, análisis de tráfico, cómo verificarlos |
| `doc/known-issues.md` | cuelgue por EEPROM, rediagnóstico del "bug Z", ALARM:98, keep-alive |
| `doc/changelog-workflow.md` | hooks, flujo tras merge de PR |
| `src/AGENTS.md` | firmware: orden de init, mapa de ISRs, flags de `config.h` |

No escribas en `AGENTS.md`/`README.md` datos volátiles (líneas, porcentajes, versiones):
da el comando que los obtiene.
