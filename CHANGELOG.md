# Changelog

All notable changes to grbl-llp are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-06-10

### Added
- `scripts/generate-changelog.sh` — auto-generate changelog entries from git log
- `.githooks/post-commit` — git hook that auto-runs changelog generation on master when a new tag is detected
- `scripts/gcode_examples/test_short_noz.gcode` — minimal smoke test for quick validation
- Zero-copy RX in LLP transport via `llp_get_final_payload_ptr()` (v3.1.0 API)

### Changed
- **LLP protocol upgraded to v3.1.0** — zero-copy RX, reduced parser struct (~17 B RAM saved)
- **Keep-alive removed from main loop** — `[KA]` messages no longer sent automatically; host must poll via `?` or `#`. Critical-alarm `[KA] ALARM` preserved.
- **Stack buffers → static BSS** — TX payload, TX line, and TX frame buffers moved from ISR/stack to static, eliminating stack overflow that caused `ALARM:98`
- **Buffer sizes optimized** (based on traffic analysis: max G-code = 21 chars, max response = 42 chars):
  - `TX_BUFFER_SIZE`: 192 → 128 (saves 64 B RAM)
  - `LINE_BUFFER_SIZE`: 80 → 48 (saves 32 B RAM)
  - `LLP_TX_LINE_BUF_SIZE`: 80 → 64 (saves 16 B RAM)
  - `LLP_MAX_PAYLOAD`: 80 → 64 (saves 16 B RAM + 32 B in frame buffer)
  - `RX_BUFFER_SIZE`: unchanged at 80
  - `BLOCK_BUFFER_SIZE`: unchanged at 12
- **`config.h`**: `LLP_MAX_PAYLOAD=64`, `LLP_ENABLE_STATS=0` explicitly defined
- **`stream_gcode.py`**: added timeout handling with extended wait for busy machine, status monitoring, improved LLP framing
- **`AGENTS.md`**: updated RAM/flash usage, buffer sizes, upload notes

### Fixed
- **Stack overflow eliminated** — peak stack usage reduced from ~250 B to ≤85 B; stack margin increased from 167 B to 343 B
- **ALARM:98 root cause** — RAM corruption from LLP transport stack overflow no longer possible
- **TX flush non-blocking** — partial frames discarded on overflow instead of blocking ISR

### RAM Usage
- Before: 1881/2048 B (91.8%) — stack margin ~167 B
- After:  1705/2048 B (83.3%) — stack margin ~343 B
- Total saved: **176 B**

## [1.0.1] - 2026-03-?? (approximate)

### Added
- Script `stream_gcode.py` for testing G-code files via LLP
- `platformio.ini` entry for Arduino Nano (ESP upload compatibility)

### Changed
- Parameter query commands (`$$`, `$#`, etc.) now return complete parameter set
- Removed trailing newlines added by firmware in UART responses
- Disabled M commands that cause intentional stops (`M0`, `M1`, `M30` handled by streamer)

### Fixed
- Bug in response to parameter query `$#` (101807)
- Bug preventing parameter retrieval via `$I` (61172)

## [1.0.0] - 2026-02-?? (initial LLP release)

### Added
- **LLP (Lightweight Link Protocol)** transport layer over UART — framing with `0xAA 0x55`, CRC-8, inter-byte timeout
- PlatformIO project structure
- `#` command for LLP buffer query (planner blocks, frame count, errors, drops)
- Support for both Arduino Uno (115200 baud) and Nano (57600 baud upload via ESP bridge)

### Changed
- Forked from Grbl 1.1h — trimmed for minimal PCB milling:
  - Removed spindle control (manual 12 V spindle)
  - Removed coolant control
  - Removed TLO (G43.1/G49), G18/G19 remapped to G17
  - Disabled probes, limits at init, build info write
  - Removed spindle/coolant from `$G` report
- Reduced RAM usage by disabling `REPORT_FIELD_BUFFER_STATE`, `REPORT_FIELD_PIN_STATE`, `REPORT_FIELD_CURRENT_FEED_SPEED`, `REPORT_FIELD_WORK_COORD_OFFSET`, `REPORT_FIELD_OVERRIDES`
- `volatile` added to `serial_rx_buffer_head` and `serial_tx_buffer_head` (shared ISR/main)
- Timer2 repurposed for LLP millisecond counter

### Fixed
- Pin A4 used for Z-axis movement (workaround for original D4 pin conflict causing MCU hang)

[1.1.1]: https://github.com/flamicomm/grbl-llp/releases/tag/v1.1.1
[1.1.0]: https://github.com/flamicomm/grbl-llp/releases/tag/v1.1.0
[1.0.1]: https://github.com/flamicomm/grbl-llp/releases/tag/v1.0.1
[1.0.0]: https://github.com/flamicomm/grbl-llp/releases/tag/v1.0.0


## [1.1.1] - 2026-06-28

### Added
- **Probe auto-report after G38.x**: Enabled `MESSAGE_PROBE_COORDINATES` in config.h; implemented `report_probe_parameters()` in report.c using original Grbl 1.1h code. Firmware now auto-sends `[PRB:x,y,z:s]\r\n` after each probe cycle — no `?` polling needed.
- **MCUSR reset cause logging**: Added `last_mcusr` global, read MCUSR at startup, and auto-report `[MSG:RST:BOR|EXT|WDR|POR|SFR]\r\n` after every welcome message to diagnose random resets during milling.
- Documented EEPROM corruption bug in AGENTS.md — corrupted `max_rate`, `acceleration`, or `junction_deviation` float values cause planner lockup on any motion command

### Changed
- Updated AGENTS.md: "Z-axis hang" rediagnosed as corrupted EEPROM settings bug; flash/RAM values updated; probe auto-report and reset-cause logging documented
- Updated README.md: documented mandatory `$RST=$` after flashing; updated flash/RAM/keep-alive/LLP version to match current code
- Updated doc/markdown/commands.md: added G38.x probing section and `[MSG:RST:...]` message

### Fixed
- **Critical: Motion hang on any axis** — previously misattributed to a Z‑axis pin bug. Root cause was EEPROM garbage (0x80000000 floats) from previous Grbl builds. Fixed by running `$RST=$` once after flashing.
