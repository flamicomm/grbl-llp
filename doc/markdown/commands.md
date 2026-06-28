# Grbl-LLP Command Reference

This document describes how every command is processed, what responses to expect, and which commands need a line terminator.

---

## Line Terminator Requirements

Commands are divided into two processing paths with different terminator requirements.

### Real-Time Commands — No Terminator Needed

These are intercepted by `llp_process_payload()` (`src/llp_transport.c:73`) and execute **immediately** as soon as the LLP frame is received. They do **not** enter the serial RX ring buffer or the G-code parser.

| Byte(s) | Command | Action |
|---|---|---|
| `0x18` | Soft-Reset | `mc_reset()` — immediately halts and resets Grbl. If reset while in motion, position may be lost (alarm thrown). |
| `?` | Status Report Query | Emits `<Idle\|Run\|Hold\|...\|MPos:...>` immediately. Accepted at any time except during homing and critical alarm. |
| `~` | Cycle Start / Resume | Resumes from feed hold, safety door/parking state (when door closed), and M0 program pause. Ignored otherwise. |
| `!` | Feed Hold | Decelerates to stop and suspends. Accepted in IDLE, RUN, or JOG. If jogging, cancels jog and flushes remaining jog commands. |
| `0x84` | Safety Door | Suspends into DOOR state, disables spindle and coolant. If in motion, decelerates to stop. Available as input pin or command. If jogging, cancels jog and flushes queued motions. |
| `0x85` | Jog Cancel | Immediately cancels current jog by feed hold and flushes remaining jog commands from buffer. Ignored if not in JOG state. |
| `0x86` | Debug Report | Emits `{...}` debug data (only with `DEBUG` defined). |
| `0x90` | Feed OVR Reset | Feed override → 100%. Range 10%–200%. |
| `0x91` | Feed OVR Coarse + | Feed override +10%. |
| `0x92` | Feed OVR Coarse − | Feed override −10%. |
| `0x93` | Feed OVR Fine + | Feed override +1%. |
| `0x94` | Feed OVR Fine − | Feed override −1%. |
| `0x95` | Rapid OVR Reset | Rapid override → 100%. Only affects G0, G28, G30. |
| `0x96` | Rapid OVR Medium | Rapid override → 50%. |
| `0x97` | Rapid OVR Low | Rapid override → 25%. |
| `0x99` | Spindle OVR Reset | Spindle speed override → 100%. Range 10%–200%. |
| `0x9A` | Spindle OVR Coarse + | Spindle speed override +10%. |
| `0x9B` | Spindle OVR Coarse − | Spindle speed override −10%. |
| `0x9C` | Spindle OVR Fine + | Spindle speed override +1%. |
| `0x9D` | Spindle OVR Fine − | Spindle speed override −1%. |
| `0x9E` | Spindle OVR Stop | Toggles spindle enable/disable, only while in HOLD state. When motion restarts, last spindle state is restored after a 4.0 s delay. |
| `0xA0` | Coolant Flood Toggle | Toggles flood coolant output pin. Accepted in IDLE, RUN, or HOLD. Changes the coolant modal state in the G-code parser. |
| `0xA1` | Coolant Mist Toggle | Toggles mist coolant output pin (requires `ENABLE_M7` compile option). Same behavior as flood toggle. |

> **Note on Grbl-LLP specifics:** LLP real-time commands can be sent mixed with regular data in the same TCP stream. The LLP parser processes bytes as they arrive. Real-time bytes are consumed immediately by `llp_process_payload()`. Non-real-time bytes are written to the `serial_rx_buffer`. LLP also allows sending multiple real-time commands in a single frame (e.g. `[0xAA][0x55][0x03]['?'][0x18][CRC]`).

### Line-Buffered Commands — Require `\n` or `\r`

These accumulate in `serial_rx_buffer` and are assembled into a line buffer in `protocol_main_loop()` (`src/protocol.c:78`). Execution triggers only on `\n` (0x0A) or `\r` (0x0D).

| Prefix | Command Category | Example |
|---|---|---|
| — | G-code | `G0 X10 Y20 F300` |
| `$` | System commands | `$$`, `$H`, `$X`, `$100=250` |
| `#` | LLP query | `#` |

The LLP frame itself does **not** need a `\n` — the line terminator must be part of the payload. For example:

```
LLP frame: [AA][55][09]G0 X10\n[CRC]
          └─ LLP header ─┘└─ G-code ─┘
```

---

## Response Behavior

### Commands That Reply `ok`

| Command | Condition | Code Path |
|---|---|---|
| **Empty/comment line** | Line contains only `(...)` or `;...` or is blank | `protocol.c:94-95` |
| **`#`** | Always — prints LLP stats first, then `ok` | `protocol.c:118` |
| **`$`** subcommands | On success — includes `$$`, `$G`, `$C`, `$X`, `$H`, `$#`, `$S`, `$I`, `$N`, `$<n>=<v>`, `$SLP` | `system.c:275` |
| **`$J=`** | If jog command passes G-code parsing | `system.c:134` → `gc_execute_line()` |
| **G-code** | If `gc_execute_line()` returns `STATUS_OK` | `protocol.c:124` |

All `ok` responses are the literal string `ok\r\n`.

### Commands That Reply `error:N`

| Command | Condition | Typical Error Codes |
|---|---|---|
| **Overflow line** | Line > 48 characters (`LINE_BUFFER_SIZE`) | `error:STATUS_OVERFLOW` |
| **`$`** subcommands | Invalid syntax, wrong state, or setting failure | `STATUS_INVALID_STATEMENT`, `STATUS_IDLE_ERROR`, `STATUS_BAD_NUMBER_FORMAT`, `STATUS_SETTING_DISABLED`, `STATUS_SETTING_READ_FAIL`, `STATUS_CHECK_DOOR` |
| **G-code in ALARM/JOG** | Machine is in alarm or jog state | `STATUS_SYSTEM_GC_LOCK` |
| **G-code parse error** | Invalid G-code block | `STATUS_GCODE_UNSUPPORTED_COMMAND` (error:20), `STATUS_GCODE_MODAL_GROUP_VIOLATION`, `STATUS_GCODE_AXIS_COMMAND_CONFLICT`, etc. |
| **G-code execution error** | Runtime failure | `STATUS_GCODE_INVALID_TARGET`, `STATUS_GCODE_ARC_RADIUS_ERROR`, etc. |

All error responses follow the format `error:N\r\n` where `N` is the numeric status code (see `status_codes_t` in `src/report.h`).

### Commands That Reply Nothing (No ACK)

| Command | Response |
|---|---|
| **`0x18` (Reset)** | None — system resets immediately |
| **`?` (Status Report)** | None — instead emits `<Idle\|Run\|Hold\|Door\|... MPos:... WPos:...>\r\n` |
| **`~` (Cycle Start)** | None |
| **`!` (Feed Hold)** | None |
| **`0x80`–`0x9F` (Overrides)** | None |
| **`0x84` (Safety Door)** | None |
| **`0x85` (Jog Cancel)** | None |

### Other Responses (Not `ok`/`error`)

| Trigger | Response | Source |
|---|---|---|
| **Startup / reset** | `Grbl 1.1h ['$' for help]\r\n` | `report.c:172` |
| **Hard limits at init** | `[MSG:Check Limits]\r\n` | `protocol.c:45` |
| **Alarm / sleep state on startup** | `[MSG:'$H'\|'$X' to unlock]\r\n` | `protocol.c:53` |
| **Alarm event** | `ALARM:N\r\n` (N = 1..10) | `report.c:125-131` |
| **Critical alarm loop** | `[KA] ALARM\r\n` (keepalive every ~1s) | `protocol.c:255` |
| **`$C` toggle on** | `[MSG:Enabled]\r\n` | `system.c:157` |
| **`$C` toggle off** | `[MSG:Disabled]\r\n` (followed by reset) | `system.c:153` |
| **`$X` unlock** | `[MSG:Caution: Unlocked]\r\n` | `system.c:164` |
| **`$RST=*` wipe** | `[MSG:Restoring defaults]\r\n` (followed by reset) | `system.c:234` |
| **Safety door ajar while running** | `[MSG:Check Door]\r\n` | `protocol.c:317` |
| **Probe success (G38.x)** | `[PRB:x.xxx,y.yyy,z.zzz:1]\r\n` — auto-reported after each successful G38.2/3/4/5 probe | `motion_control.c:311-314` |
| **Probe failure (G38.x)** | `[PRB:x.xxx,y.yyy,z.zzz:0]\r\n` — auto-reported when probe did not trigger | `motion_control.c:311-314` |
| **Reset cause** | `[MSG:RST:BOR\|EXT\|WDR\|POR\|SFR]\r\n` — auto-reported after every MCU reset. `BOR`=brown-out, `EXT`=external/RESET pin, `WDR`=watchdog, `POR`=power-on, `SFR`=soft firmware reset | `main.c` |

---

## Command Flow Diagram

```
                    ┌──────────────┐
  LLP frame ──────►│ llp_transport │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ llp_parse    │
                    │ _byte()      │
                    └──────┬───────┘
                           │
              ┌────────────┴────────────┐
              │                         │
     ┌────────▼────────┐      ┌────────▼────────┐
     │ llp_process_    │      │ serial_rx_      │
     │ payload()       │      │ buffer_write()  │
     └────────┬────────┘      └────────┬────────┘
              │                        │
     ┌────────▼────────┐      ┌────────▼────────┐
     │ Real-time cmd?  │      │ protocol_main_  │
     │ Execute NOW     │      │ loop() waits    │
     │ No response     │      │ for \n or \r    │
     └─────────────────┘      └────────┬────────┘
                                        │
                              ┌─────────▼──────────┐
                              │ Line complete?      │
                              │ Dispatch:           │
                              │  empty  → ok        │
                              │  $*     → ok|error  │
                              │  #      → stats+ok  │
                              │  G-code → ok|error  │
                              └─────────────────────┘
```

---

## `$` System Command Details

Commands listed in the `$` help message (`[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]`).

#### `$$` — View Grbl settings
Prints all Grbl configuration settings (`$0`–`$32`). See [Grbl v1.1 Configuration](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Configuration) for details.

#### `$x=val` — Write Grbl settings
Store a setting value, e.g. `$100=250.000`. Requires IDLE or ALARM state; returns `error:STATUS_IDLE_ERROR` otherwise.

#### `$#` — View G-code parameters
Prints G54–G59 work offsets, G28/G30 predefined positions, G92 offset, TLO, and last probe result.

#### `$G` — View G-code parser state
Prints all active modal groups, e.g. `[GC:G0 G54 G17 G21 G90 G94 M0 M5 M9 T0 S0.0 F500.0]`.

#### `$I` — View/build info
Prints Grbl version and build date. Optionally `$I=<string>` stores a custom identification string (requires `ENABLE_BUILD_INFO_WRITE_COMMAND`).

#### `$N` — View startup lines
Prints the two stored startup G-code blocks (`$N0=`, `$N1=`).

#### `$Nx=line` — Save startup block
Store a G-code block to run on startup/reset, e.g. `$N0=G20 G54 G17`. If the G-code is invalid, returns error and does not save. Startup blocks do not run if Grbl initializes in ALARM state or after `$X` unlock.

#### `$J=line` — Jogging motion
Executes a jog command. Unlike normal G-code, jog commands:
- Can be canceled via jog-cancel or feed-hold.
- Do **not** alter the G-code parser modal state.
- Respect soft-limits by returning an error (no alarm) if exceeded.
- Require `$J=` prefix, axis words with target, and `F` feed rate.
- Accept optional `G20`/`G21`, `G90`/`G91`, `G53` for single-command override.
- Returns `ok` after parsing and queuing, or `error:` if invalid.

#### `$SLP` — Sleep mode
Places Grbl into a de-powered sleep state: spindle, coolant, and stepper enable pins are shut down. Exit by soft-reset or power-cycle. Grbl re-initializes in ALARM state.

#### `$C` — Check G-code mode
Toggles check mode: all G-code is parsed fully (including soft-limit checks) but no motion executes. When toggled off, Grbl performs an automatic soft-reset for a clean state.

#### `$X` — Kill alarm lock
Overrides the alarm lock, allowing G-code functions while in alarm state. **Use with extreme caution** — position has likely been lost. Use G91 incremental mode for short moves. Does not run startup lines; always reset after clearing the alarm.

#### `$H` — Homing cycle
Runs the homing cycle. Single-axis homing also available: `$HX`, `$HY`, `$HZ` (requires `HOMING_SINGLE_AXIS_COMMANDS`).

#### `$RST=$`, `$RST=#`, `$RST=*` — Restore defaults
- `$RST=$` — Restore `$$` settings to defaults.
- `$RST=#` — Clear all G-code parameters (G54–G59, G28, G30, etc.).
- `$RST=*` — Wipe all EEPROM data used by Grbl (settings, parameters, startup lines, build info).
All three trigger an automatic reset after execution.

---

## G38.x Probing Commands (Auto-Leveling)

Grbl-LLP supports G38.x probing via the probe pin on **A5** (Analog Pin 5). These commands move an axis until the probe pin is triggered, then stop and record the contact position.

| Command | Behavior on trigger | Behavior on no trigger |
|---|---|---|
| **G38.2** | Stops, records position, returns success | ALARM, stops |
| **G38.3** | Stops, records position, returns success | Returns error, stops |
| **G38.4** (away) | Stops, records position, returns success | ALARM, stops |
| **G38.5** (away) | Stops, records position, returns success | Returns error, stops |

### Auto-Report After Probe

When `MESSAGE_PROBE_COORDINATES` is enabled (default in grbl-llp), the firmware **automatically** sends the probe contact position after every G38.x cycle:

```
G38.2 Z-1 F30       ← sent by host
ok\r\n              ← G-code accepted and executed
[PRB:45.678,12.345,-0.085:1]\r\n  ← auto-reported by firmware
```

The `:1` flag means the probe was triggered (success). `:0` means the probe did not trigger (failure).

The host does NOT need to poll with `?` to obtain the probe position.

### Suggested Probing Workflow for PCB Leveling

```
G0 X10 Y10 Z2         ← position above probe point
G38.2 Z-1 F30         ← probe down until contact
[PRB:45.678,12.345,-0.085:1]  ← position received automatically
G0 Z2                 ← retract

G0 X20 Y10 Z2         ← next point
G38.2 Z-1 F30         ← probe again
[PRB:45.678,12.345,-0.080:1]  ← position received automatically
G0 Z2                 ← retract
...                   ← repeat for grid
```

### Probe Pin Wiring

```
Arduino A5   ←── alligator clip → tool bit (V-bit)
Arduino GND  ←── alligator clip → PCB copper (GND)
```

Pull-up internal active by default. $6=0 is correct. When the bit touches copper, A5 goes LOW → probe triggers.

### See Also

- `config.h`: `MESSAGE_PROBE_COORDINATES` (line 164), `ALLOW_FEED_OVERRIDE_DURING_PROBE_CYCLES` (line 544)
- `src/probe.c`: `probe_state_monitor()` runs in stepper ISR for microsecond-accurate trigger detection
- `$#` command: includes `[PRB:...]` line with last probe result

---

## Summary

| Characteristic | Real-Time Commands | Line-Buffered Commands |
|---|---|---|
| Terminator needed | No | `\n` or `\r` required |
| Response to success | None (except `?` → status report) | `ok\r\n` |
| Response to failure | None (silently ignored if invalid) | `error:N\r\n` |
| Processing path | `llp_process_payload()` switch | `protocol_main_loop()` line dispatch |
| G-code, `$`, `#` | Not processed here | Processed here |
| Reset, overrides, `?`, `~`, `!` | Processed here | Not processed here |
