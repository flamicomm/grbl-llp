# grbl-llp

**LLP (Lightweight Link Protocol) - Capa de transporte confiable para Grbl firmware**

## El Problema

La comunicación serial entre Arduino Uno y una PC via USB/UART es **inestable**. Se producen pérdidas de datos que causan:

- Fresado que termina abruptamente
- Comandos G-code que no se ejecutan completamente
- Pérdida de posición del equipo
- Respuestas "ok" que nunca llegan o se corrompen

### ¿Por qué ocurre?

Las conexiones seriales por USB (CDC/ACM) no garantizan entrega confiable de datos:
- Pérdida de bytes individuales
- Corrupción de datos (bytes cambiados)
- Reordenamiento de tramas
- Latencia variable e impredecible

Cuando se envía un comando G-code como `G0 X10 Y10`, si un solo byte se pierde o se corrompe, el comando nunca se ejecuta correctamente y el flujo de trabajo se rompe.

## La Solución: Protocolo LLP

LLP (Lightweight Link Protocol) es un protocolo de comunicaciones diseñado para ser **resistente a errores**:

### Características principales

- **Tramas binarias con CRC**: Cada trama incluye un checksum para validar integridad
- **Bytes de sincronización**: Secuencia `0xAA 0x55` para identificar inicio de trama
- **Longitud explícita**: Permite saber exactamente cuántos bytes conforman la trama
- **Doble validación**: CRC-8 detecta errores y corruption de datos

### Formato de trama LLP

```
+--------+--------+--------+-----------------+----------+
| 0xAA   | 0x55   | LENGTH | PAYLOAD (LENGTH) | CRC-8    |
+--------+--------+--------+-----------------+----------+
  Sync      Sync    Size        Data        Checksum
```

### ¿Cómo funciona?

```
Host → Envía trama LLP: [AA][55][05][G90][CRC]
     → Espera confirmación

Arduino → Recibe trama completa
       → Valida CRC
       → Extrae payload "G90"
       → Procesa comando
       → Responde con "ok\n"

Host → Recibe respuesta
     → Confirma que comando fue procesado
```

## Diferencias con Grbl estándar

| Aspecto | Grbl estándar | grbl-llp |
|---------|---------------|----------|
| Terminación de línea | Requiere `\n` explícito | Automático via LLP |
| Validación de datos | Ninguna | CRC-8 por trama |
| Detección de errores | Por perdida de respuesta | CRC validar cada trama |
| Respuesta a comandos | `ok` o `error` | `ok` o `error:1` (datos corruptos) |
| Buffer RX | 128 bytes | 80 bytes (espacio para overhead LLP) |
| Buffer TX | 100 bytes | 192 bytes (respuestas con framing) |
| Control de spindle | Habilitado | **Deshabilitado** (libera pines D11, D12) |
| Consulta de buffer | No disponible | **`#`** → responde `buf:N` |

## Comando de consulta de buffer

grbl-llp agrega el comando especial `#` para consultar el estado del planner buffer:

```
Host → LLP: "#"
Grbl  → LLP: "buf:11\r\n" + "ok\r\n"
```

**Uso:**
- Enviar `#` como payload de una trama LLP
- Grbl responde con `buf:N` donde N = bloques disponibles (0-12)
- Seguido de la respuesta estándar `ok`

**Ejemplo de flujo con control de buffer:**

```
Host → "#"          → Grbl responde "buf:11" (11 libres de 12)
Host → "G0 X10"     → Grbl responde "ok"
Host → "#"          → Grbl responde "buf:10" (10 libres, 1 ocupado)
Host → "G1 X20 Y30" → Grbl responde "ok"
Host → "#"          → Grbl responde "buf:9"  (9 libres, 2 ocupados)
```

## Archivos agregados

- `src/llp_transport.c` / `src/llp_transport.h` - Recepción y parsing de tramas LLP
- `src/llp_protocol.c` / `src/llp_protocol.h` - Máquina de estados del protocolo
- `src/serial.c` / `src/serial.h` - Integración de LLP con ISR serial

## Archivos modificados

- `src/protocol.c` - Fix para agregar `\n` automáticamente al procesar payload LLP
- `src/config.h` - Comentarios de configuración LLP
- `src/planner.h` - `BLOCK_BUFFER_SIZE` reducido de 16 a 12
- `src/stepper.h` - `SEGMENT_BUFFER_SIZE` permanece en 6

## Hardware

- Arduino Uno (ATmega328P)
- Serial a 115200 baud
- CNC Shield v3.x compatible (opcional)

## Compilación

```bash
pio run -e uno                                          # Compilar
pio run -e uno -t upload --upload-port /dev/ttyUSB0     # Subir a Arduino
```

## Librería Java

Implementación cliente LLP disponible en:
https://github.com/flamicomm/llp-protocol-java

## License

MIT — See [LICENSE](LICENSE)

LLP Specification v3.1.0 — Copyright © 2026 Flamingo Communications

This specification is maintained as the authoritative reference for the LLP protocol. All implementations should reference this document as the canonical behaviour definition.

## Créditos

- Grbl por Sungeun "Sonny" Jeon (@chamnit)
- Grbl v0.6 original por Simen Svale Skogsrud