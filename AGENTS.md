# AGENTS.md - grbl-llp

## Build & Upload

```bash
pio run -e uno                     # Compile
pio run -e uno -t upload           # Upload (auto-detect port)
pio run -e uno -t upload --upload-port /dev/ttyUSB0  # Upload to specific port
```

## Flash Constraints

- Flash: **90.3%** used (29112/32256 bytes)
- RAM: **83.3%** used (1705/2048 bytes; stack margin ~343 B)
- **Never use `-O0`** — flash overflows. Test changes with `-O0` only temporarily, then revert.
- Adding debug features may require removing other code to fit.

## Architecture

- **grbl-llp**: Grbl 1.1h fork with LLP (Lightweight Link Protocol) transport layer on Arduino Uno (ATmega328P)
- **esp8266-llp-bridge**: Separate ESP8266 firmware that bridges WiFi (TCP) ↔ UART ↔ grbl-llp
- LLP runs on UART segment (ESP↔Arduino) only; TCP already guarantees reliability PC↔ESP
- **Do not use `Serial.print()` on ESP bridge** — Serial is permanently connected to Arduino

## Key Modifications from Grbl 1.1h

| File | Change |
|---|---|---|
| `serial.c`, `serial.h` | `volatile` on `serial_rx_buffer_head` and `serial_tx_buffer_head` (shared ISR/main) |
| `llp_transport.c` | Timer2 used for ms counter; non-blocking TX flush (discards on overflow) |
| `protocol.c` | Keep-alive removed from main loop (2006-06-10); `[KA] ALARM` in critical alarm loop preserved; `#` command with LLP stats |
| `config.h` | `MESSAGE_PROBE_COORDINATES`, `CHECK_LIMITS_AT_INIT`, `REPORT_FIELD_*`, `ENABLE_BUILD_INFO_WRITE_COMMAND` disabled |
| `spindle_control.c` | Stubs only — no spindle I/O (manual 12V spindle) |
| `coolant_control.c` | Stubs only — no coolant I/O |
| `gcode.c` | TLO (G43.1/G49) removed; G18/G19 mapped to G17; spindle/coolant execution no-ops |
| `report.c` | Spindle/coolant removed from `$G` report; probe parameters stubbed; RPM settings removed |

## Buffer Sizes (RAM Optimization)

| Buffer | File | Value | Saved |
|---|---|---|---|
| `TX_BUFFER_SIZE` | `serial.h:31,33` | 128 | 64 B (was 192) |
| `LINE_BUFFER_SIZE` | `protocol.h:32` | 48 | 32 B (was 80) |
| `LLP_TX_LINE_BUF_SIZE` | `llp_transport.h:26` | 64 | 16 B (was 80) |
| `LLP_MAX_PAYLOAD` | `config.h:703` | 64 | 16 B (was 80) |
| `RX_BUFFER_SIZE` | `serial.h:27` | 80 | — (unchanged) |
| `BLOCK_BUFFER_SIZE` | `config.h` | 12 | — (unchanged) |

All sizes chosen based on actual traffic analysis: max G-code command = 21 chars, max TX response = 42 chars (worst-case status report with 3-axis positions ±1234.567).

## `#` Command (LLP Buffer Query)

Host sends `#` as LLP payload → Grbl responds:
```
buf:11 rx:150 e:0 t:2 rd:0 td:0
```
Fields: `buf:N` (planner blocks), `rx:N` (LLP frames), `e:N` (errors), `t:N` (timeouts), `rd:N` (RX dropped), `td:N` (TX dropped).

## LLP Protocol

- Frame: `[0xAA][0x55][LEN][PAYLOAD...][CRC]`
- CRC-8 per frame
- Inter-byte timeout: 2000ms → parser resets
- TX flush is non-blocking: if TX buffer full, partial frame is discarded and `tx_dropped++` increments
- LLP stats (`llp_transport_get_stats()`, `llp_transport_reset_stats()`) accessible via `#` command

## Known Issues

### Z-axis Hang (Grbl 1.1h bug, NOT in LLP code)

The original Grbl 1.1h (unmodified) also hangs on Z-axis movement (`G0 Z1`). This is a Grbl core bug, not an LLP issue. Symptoms:
- MCU completely stops (no keepalive, no serial response) after first Z movement
- X and Y work indefinitely; only Z_AXIS (index 2) triggers hang
- Swapping STEP_BIT assignments (Z→D2, X→D4) makes both work — confirms it's software index issue, not pin D4 hardware
- Workaround: remap Z_STEP to different pin in `cpu_map.h` and rewire CNC shield

### Keep-Alive in Alarm Loop

The critical limit alarm loop (`protocol_exec_rt_system`) has a minimal keep-alive (`[KA] ALARM\r\n`). This is intentional to keep flash usage low. Full stats were removed to save space.

## Debug Features

Keep-alive is compiled in by default. To disable:
1. Remove or comment the keep-alive block in `protocol.c` main loop (~180-197)
2. Remove `llp_stats_t` and `llp_transport_get_ms()` from `protocol.c` if no longer referenced

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/) format.

### Auto-generate entries for a new release

```bash
# From repo root:
bash scripts/generate-changelog.sh v1.0.1 v1.1.0
```

This appends formatted entries to `CHANGELOG.md` based on commit messages.
Always review and edit the result before committing.

### Automatic generation on tag creation

A `post-commit` hook in `.githooks/` detects when a new tag is created on the
`master` branch and auto-runs `generate-changelog.sh`. To enable:

```bash
git config core.hooksPath .githooks
```

The hook will amend the last commit with the updated `CHANGELOG.md`.

## Minimum Hardware Setup

- Arduino Uno (ATmega328P)
- CNC Shield v3.x (optional)
- No spindle control (`VARIABLE_SPINDLE` disabled in `config.h`)
- No coolant (minimal CNC config)
- Low speed: < 1000mm/min for PCB prototyping

## Related Documentation

- `README.md` — LLP protocol overview and differences from Grbl standard
- `src/config.h` — compile-time config, `#define DEFAULTS_GENERIC`, `CPU_MAP_ATMEGA328P`
- `src/cpu_map.h` — pin mapping (STEP_BIT, DIRECTION_BIT, LIMIT_BIT per axis)