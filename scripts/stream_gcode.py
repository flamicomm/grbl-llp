#!/usr/bin/env python3
"""
stream_gcode.py - Stream G-code to grbl-llp via LLP over serial.

Usage:
    python3 stream_gcode.py <gcode_file> [--port /dev/ttyUSB0] [--baud 115200]

The script sends each G-code line wrapped in LLP frames to the Arduino,
waits for 'ok' or 'error:' responses, and reports any failures.

Intentional stop commands (M0, M1, M2, M30) are skipped with a warning.
Unsupported commands that return 'error:' are logged but do not stop the stream.
"""

import argparse
import sys
import os
import time
import re
import serial

# Import local llp.py from the same scripts directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import llp
except ImportError:
    print("ERROR: Could not import llp.py. Ensure llp.py is in the same directory as this script:", _SCRIPT_DIR)
    sys.exit(1)


DEFAULT_PORT = '/dev/ttyUSB0'
DEFAULT_BAUD = 115200

# Commands that intentionally stop/pause the machine — skipped for automated streaming
INTENTIONAL_STOP_CMDS = {'M0', 'M1', 'M2', 'M30'}


def send_frame(ser, text):
    """Send text as an LLP frame over serial."""
    frame = llp.encode(text.encode('utf-8'))
    ser.write(frame)
    ser.flush()


def read_responses(ser, parser, timeout_sec):
    """Read and parse LLP frames, return list of decoded text strings."""
    start = time.time()
    payloads = []
    while time.time() - start < timeout_sec:
        if ser.in_waiting:
            raw = ser.read(ser.in_waiting)
            for byte in raw:
                result = parser.process_byte(byte, int(time.time() * 1000))
                if result == 1:
                    payload = parser.get_final_payload()
                    try:
                        text = payload.decode('utf-8', errors='replace')
                    except Exception:
                        text = str(payload)
                    payloads.append(text)
        time.sleep(0.02)
    return payloads


def clean_line(line):
    """Strip comments and whitespace from a G-code line."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith('(') and stripped.endswith(')'):
        return None
    if stripped.startswith(';'):
        return None
    if '(' in stripped:
        stripped = stripped.split('(')[0].strip()
    if not stripped:
        return None
    return stripped


def get_cmd_token(cmd):
    """Extract the first G or M token from a command, e.g. 'G0', 'M3', 'M30'."""
    m = re.match(r'^\s*([GM]\d+)', cmd.upper())
    return m.group(1) if m else None


def is_motion_command(cmd):
    """Check if command is a motion command that takes time to execute."""
    upper = cmd.upper().lstrip()
    return any(upper.startswith(p) for p in ('G0 ', 'G1 ', 'G2 ', 'G3 ', 'G00 ', 'G01 ', 'G02 ', 'G03 '))


def check_state(ser, parser):
    """Send '?' and return parsed status string, or None if no response."""
    send_frame(ser, "?")
    resps = read_responses(ser, parser, timeout_sec=1.5)
    for r in resps:
        s = r.strip()
        if s.startswith('<'):
            return s
    return None


def check_buffer(ser, parser):
    """Send '#' and return buffer info string, or None if no response."""
    send_frame(ser, "#")
    resps = read_responses(ser, parser, timeout_sec=1.5)
    for r in resps:
        s = r.strip()
        if s.startswith('buf:'):
            return s
    return None


def reset_arduino(ser, parser):
    """Send ctrl-x reset and wait for startup."""
    print("[RESET] Sending ctrl-x (0x18) to reset Arduino...")
    send_frame(ser, "\x18")
    time.sleep(0.5)
    resps = read_responses(ser, parser, timeout_sec=2.0)
    for r in resps:
        print(f"  {r.strip()}")
    time.sleep(1.0)


def stream_gcode(filepath, port, baud):
    """Main streaming logic."""
    with open(filepath, 'r') as f:
        lines = f.readlines()

    commands = []
    for i, line in enumerate(lines, start=1):
        cleaned = clean_line(line)
        if cleaned is not None:
            commands.append((i, cleaned))

    total_commands = len(commands)
    print(f"File: {filepath}")
    print(f"Total lines: {len(lines)}, Commands to send: {total_commands}")
    print(f"Port: {port} @ {baud} baud")
    print(f"Connecting...")

    with serial.Serial(port, baud, timeout=1) as ser:
        time.sleep(2.5)
        parser = llp.LLPParser()
        read_responses(ser, parser, timeout_sec=1.5)
        reset_arduino(ser, parser)

        print("Connected. Streaming started...")
        print("=" * 60)

        sent = 0
        ok_count = 0
        error_count = 0
        timeout_count = 0
        skipped_count = 0
        errors_detail = []
        last_keepalive = time.time()
        last_progress = time.time()

        print("[INIT] Sending $X (unlock if alarm)...")
        send_frame(ser, "$X")
        resps = read_responses(ser, parser, timeout_sec=2.0)
        for r in resps:
            r_stripped = r.strip()
            if r_stripped.startswith('[KA]'):
                last_keepalive = time.time()
            else:
                print(f"  {r_stripped}")

        for line_num, cmd in commands:
            sent += 1
            token = get_cmd_token(cmd)

            # Skip intentional stop commands
            if token in INTENTIONAL_STOP_CMDS:
                skipped_count += 1
                print(f"[SKIP] Line {line_num}: '{cmd}' (intentional stop command)")
                continue

            is_motion = is_motion_command(cmd)

            # Send command
            send_frame(ser, cmd)

            # Wait for 'ok' or 'error:'
            got_ok = False
            got_error = False
            base_timeout = 12.0 if is_motion else 5.0
            start_wait = time.time()
            elapsed = 0.0

            while elapsed < base_timeout:
                resps = read_responses(ser, parser, timeout_sec=0.3)
                for r in resps:
                    r_stripped = r.strip()
                    if r_stripped.lower() == 'ok':
                        got_ok = True
                    elif r_stripped.lower().startswith('error:'):
                        got_error = True
                        error_count += 1
                        errors_detail.append((line_num, cmd, r_stripped))
                    elif r_stripped.startswith('[KA]'):
                        last_keepalive = time.time()

                if got_ok or got_error:
                    break
                elapsed = time.time() - start_wait

            # If no ok/error yet, check state with '?'
            if not got_ok and not got_error:
                status = check_state(ser, parser)
                if status:
                    if any(s in status for s in ('Run', 'Hold', 'Cycle', 'Jog')):
                        # Still executing, give more time
                        extended_wait = 25.0 if is_motion else 10.0
                        extra_start = time.time()
                        while time.time() - extra_start < extended_wait:
                            resps = read_responses(ser, parser, timeout_sec=0.5)
                            for r in resps:
                                r_stripped = r.strip()
                                if r_stripped.lower() == 'ok':
                                    got_ok = True
                                elif r_stripped.lower().startswith('error:'):
                                    got_error = True
                                    error_count += 1
                                    errors_detail.append((line_num, cmd, r_stripped))
                                elif r_stripped.startswith('[KA]'):
                                    last_keepalive = time.time()
                            if got_ok or got_error:
                                break

                            # Poll state every 2s during extended wait
                            elapsed_extra = time.time() - extra_start
                            if elapsed_extra > 2.0 and elapsed_extra % 2.0 < 0.6:
                                st = check_state(ser, parser)
                                if st and 'Idle' in st:
                                    got_ok = True
                                    break
                    else:
                        pass

            # Final check - if still no ok/error, ping with '#'
            if not got_ok and not got_error:
                buf_info = check_buffer(ser, parser)
                if buf_info:
                    got_ok = True
                else:
                    timeout_count += 1
                    print(f"\n[TIMEOUT] Line {line_num}: '{cmd}'")
                    print(f"  No 'ok' after {base_timeout}s and no response to '#'")
                    print(f"  Last keepalive: {time.time() - last_keepalive:.1f}s ago")
                    print("=" * 60)
                    print("\nSTREAMING ABORTED - MCU HUNG")
                    print(f"  Last processed line: {line_num}")
                    print(f"  Command: {cmd}")
                    print(f"  Sent: {sent}/{total_commands}")
                    print(f"  OK: {ok_count}, Errors: {error_count}, Timeouts: {timeout_count}, Skipped: {skipped_count}")
                    return False

            if got_error:
                print(f"[ERROR] Line {line_num}: '{cmd}' -> {errors_detail[-1][2]}")
                # Continue streaming — unsupported commands are not fatal

            if got_ok:
                ok_count += 1

            # Progress update
            if time.time() - last_progress > 3.0 or sent % 100 == 0 or sent == total_commands:
                pct = (sent / total_commands) * 100
                print(f"  Progress: {sent}/{total_commands} ({pct:.1f}%) OK={ok_count} ERR={error_count} SKIP={skipped_count}")
                last_progress = time.time()

        # Drain trailing responses
        print("\nDraining trailing responses...")
        time.sleep(0.5)
        resps = read_responses(ser, parser, timeout_sec=2.0)
        for r in resps:
            r_stripped = r.strip()
            if r_stripped.lower() == 'ok':
                ok_count += 1
            elif r_stripped.lower().startswith('error:'):
                error_count += 1

        # Final health check
        print("\nFinal health check...")
        buf_info = check_buffer(ser, parser)
        status = check_state(ser, parser)
        alive = buf_info is not None or status is not None

        if buf_info:
            print(f"  Buffer: {buf_info}")
        if status:
            print(f"  Status: {status}")

        print("=" * 60)
        print("STREAMING COMPLETE")
        print(f"  Total commands sent: {sent}/{total_commands}")
        print(f"  OK responses: {ok_count}")
        print(f"  Error responses: {error_count}")
        print(f"  Skipped (intentional stops): {skipped_count}")
        print(f"  Timeouts: {timeout_count}")
        print(f"  MCU alive after streaming: {'YES' if alive else 'NO'}")

        if errors_detail:
            print("\n  Errors detail:")
            for line_num, cmd, err in errors_detail:
                print(f"    Line {line_num}: '{cmd}' -> {err}")

        return alive


def main():
    parser = argparse.ArgumentParser(description="Stream G-code to grbl-llp via LLP")
    parser.add_argument('gcode_file', help='Path to G-code file to stream')
    parser.add_argument('--port', default=DEFAULT_PORT, help='Serial port (default: /dev/ttyUSB0)')
    parser.add_argument('--baud', type=int, default=DEFAULT_BAUD, help='Baud rate (default: 115200)')
    args = parser.parse_args()

    if not os.path.isfile(args.gcode_file):
        print(f"ERROR: File not found: {args.gcode_file}")
        sys.exit(1)

    alive = stream_gcode(args.gcode_file, args.port, args.baud)
    sys.exit(0 if alive else 1)


if __name__ == '__main__':
    main()
