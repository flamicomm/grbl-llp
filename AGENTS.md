# AGENTS.md - grbl-llp

> **⚠️ Regla de oro: Siempre ejecutar `$RST=$` después de flashear.**  
> EEPROM de Arduinos usados puede contener floats inválidos de Grbl stock/otro fork que lockean el planificador. Ver "Motion Hang Due to Corrupted EEPROM Settings" abajo.

## Build & Upload

```bash
pio run -e uno                     # Compile
pio run -e uno -t upload           # Upload (auto-detect port)
pio run -e uno -t upload --upload-port /dev/ttyUSB0  # Upload to specific port
```

## Flash Constraints

- Flash: **91.1%** used (29394/32256 bytes for UNO; 95.7% Nano)
- RAM: **83.3%** used (1706/2048 bytes; stack margin ~342 B)
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
| `config.h` | `CHECK_LIMITS_AT_INIT`, `REPORT_FIELD_*`, `ENABLE_BUILD_INFO_WRITE_COMMAND` disabled; `MESSAGE_PROBE_COORDINATES` **enabled** (auto-report after G38.x) |
| `spindle_control.c` | Stubs only — no spindle I/O (manual 12V spindle) |
| `coolant_control.c` | Stubs only — no coolant I/O |
| `gcode.c` | TLO (G43.1/G49) removed; G18/G19 mapped to G17; spindle/coolant execution no-ops |
| `report.c` | Spindle/coolant removed from `$G` report; RPM settings removed; `report_probe_parameters()` re-enabled with auto-report after G38.x |

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
- CRC16-CCITT per frame
- Inter-byte timeout: 2000ms → parser resets
- TX flush is non-blocking: if TX buffer full, partial frame is discarded and `tx_dropped++` increments
- LLP stats (`llp_transport_get_stats()`, `llp_transport_reset_stats()`) accessible via `#` command

## Critical Context

- **Current RAM**: 1706/2048 B (83.3%); Flash: 29394/32256 B (91.1% UNO) / 29394/30720 B (95.7% Nano); stack margin ~342 B
- **ALARM:98 root cause**: Not a standard Grbl alarm code (only 1–10 defined). Most likely RAM corruption from stack overflow in LLP transport layer.
- **Motion hang root cause**: Corrupted EEPROM settings (see "Motion Hang Due to Corrupted EEPROM Settings" below). `$RST=$` fixes it immediately.
- **ESP8266 bridge**: Permanent intervening device on `/dev/ttyUSB0`. To upload to the Arduino, the ESP must be in transparent mode (works with `nano` env at 57600 baud). The UNO uses standard 115200 baud for upload.
- **Protected branches**: `master` and `dev` accept changes only via PR; local git hooks must not attempt commits/amends.

## Known Issues

### Motion Hang Due to Corrupted EEPROM Settings (Critical)

**Symptoms:** MCU freezes completely (no serial response, no keep‑alive) after any `G0`/`G1` motion command — regardless of axis. `$$` shows garbage values like `$11=-2147483.648` (0x80000000 as float), `$110=-2147483.648`, `$121=0.000`, or missing lines (`$101`, `$120`).

**Root cause:** Corrupted EEPROM settings (nan / inf / uninitialised float data) for `max_rate`, `acceleration`, `junction_deviation`, etc. When the planner reads these, it computes invalid step parameters, causing the stepper interrupt or main loop to lock up.

**Discovered:** 2026‑06‑11 — an UNO with seemingly random hangs was fixed entirely by running `$RST=$`. The same board then executed XY and Z moves without issues.

**Fix:**
```bash
# Restore all settings to compile‑time defaults:
echo '$RST=$' | python3 -c "
import sys; sys.path.insert(0,'scripts')
import serial, time, llp
s = serial.Serial('/dev/ttyUSB0', 115200, timeout=3)
s.setDTR(0); time.sleep(0.1); s.setDTR(1); time.sleep(2)
s.read_all()  # drain init
frame = llp.encode(b'\$RST=\$\n')
s.write(frame); time.sleep(1); s.close()
"
```
After `$RST=$`, the MCU resets automatically. Verify with `$$` — all values should be reasonable. Then reconfigure per‑axis settings (`$100`–`$132`) for your machine.

**Prevention:** Always verify settings with `$$` after flashing a new board. Old EEPROM data from a previous Grbl build or a different unit can contain invalid floats that silently corrupt the planner.

### ~~Z-axis Hang~~ (Rediagnosed: same EEPROM corruption bug)

Previously thought to be a Grbl core pin‑contention bug. Symptoms matched the corrupted EEPROM pattern exactly:
- MCU stops after first Z movement (or ANY axis — Z was just the first tested)
- `$$` showed garbage values identical to the corrupted settings bug

**2026‑06‑11 resolution:** After `$RST=$` cleared the EEPROM garbage, Z‑axis motion (`G0 Z2 F200`) worked perfectly — even on a bare UNO with no CNC shield, where this was previously reported as "unfixable." The supposed "Z‑axis bug" was the same corrupted EEPROM issue all along. Retest if a board exhibits this symptom — `$RST=$` before any other debugging.

### Keep-Alive in Alarm Loop

The critical limit alarm loop (`protocol_exec_rt_system`) has a minimal keep-alive (`[KA] ALARM\r\n`). This is intentional to keep flash usage low. Full stats were removed to save space.

## Debug Features

Keep-alive is compiled in by default. To disable:
1. Remove or comment the keep-alive block in `protocol.c` main loop (~180-197)
2. Remove `llp_stats_t` and `llp_transport_get_ms()` from `protocol.c` if no longer referenced

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/) format.

### Manual generation

```bash
# From repo root:
bash scripts/generate-changelog.sh v1.0.1 v1.1.1
```

This appends formatted entries to `CHANGELOG.md` based on commit messages.
Always review and edit the result before committing.

### Automatic generation (git hooks)

Two hooks in `.githooks/` auto-generate changelog entries:

| Hook | Trigger | Behaviour |
|---|---|---|
| `post-commit` | Local `git commit` | Updates CHANGELOG.md on feature branches; preview-only on master/dev |
| `post-merge` | `git pull` or `git merge` | Updates CHANGELOG.md locally (does not amend — branches may be protected) |

Since **`master` and `dev` are protected** (PR-only merges), the hooks:
- Run **locally** on your machine when you pull the merged PR (`git pull` triggers `post-merge`)
- Update `CHANGELOG.md` in the working tree without committing
- On protected branches, instruct you to create a new PR with the changelog change
- On feature branches, you can `git add && git commit` the changelog directly

**Enable hooks:**
```bash
git config core.hooksPath .githooks
chmod +x .githooks/post-commit .githooks/post-merge
```

**Workflow example after a PR merge:**
```bash
git checkout dev && git pull
# post-merge fires → CHANGELOG.md updated locally
git checkout -b update-changelog
git add CHANGELOG.md && git commit -m "Update CHANGELOG.md"
git push origin update-changelog
# create PR → dev
```

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