#!/usr/bin/env python3
"""
stream_gcode.py - Stream G-code to grbl-llp via LLP over serial.

Usage:
    python3 stream_gcode.py <gcode_file> [--port /dev/ttyUSB0] [--baud 115200]

The script sends each G-code line wrapped in LLP frames to the Arduino,
waits for 'ok' or 'error:' responses, and reports any failures.

Note: Grbl-LLP uses LLP frames as command delimiters instead of '\\n'.
      This script ensures each command is terminated with '\\n' so the
      G-code is processed correctly (LLP frame = command, '\\n' = terminator).

Asynchronous status monitoring: every 1 second a background thread queues
a '?' status request. The main thread sends it through the serial port and
verifies a status response is received, confirming the device is responsive
even during motion execution.

Intentional stop commands (M0, M1, M2, M30) are skipped with a warning.
Unsupported commands that return 'error:' are logged but do not stop the stream.
"""

import argparse
import sys
import os
import time
import re
import serial
import threading
import queue
import datetime

# Import local llp.py from the same scripts directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, _SCRIPT_DIR)

import llp


DEFAULT_PORT = '/dev/ttyUSB0'
DEFAULT_BAUD = 115200
LOG_DIR = os.path.join(_SCRIPT_DIR, 'log')

# Commands that intentionally stop/pause the machine — skipped for automated streaming
INTENTIONAL_STOP_CMDS = {'M0', 'M1', 'M2', 'M30'}


class StreamLogger:
    """Logs all TX/RX to a file with timestamps for debugging."""

    def __init__(self, log_path):
        self.f = open(log_path, 'w', buffering=1)
        self._start = time.time()

    def log_tx(self, msg):
        elapsed = time.time() - self._start
        self.f.write(f"[{elapsed:.3f}] TX: {msg}\n")

    def log_rx(self, msg):
        elapsed = time.time() - self._start
        self.f.write(f"[{elapsed:.3f}] RX: {msg}\n")

    def log_event(self, msg):
        elapsed = time.time() - self._start
        self.f.write(f"[{elapsed:.3f}] *** {msg}\n")

    def close(self):
        self.f.close()


class StatusMonitor:
    """Background thread that queues '?' every 1 second for async status checks.

    Thread-safe design: the background thread only writes to a queue. The main
    thread owns the serial port, drains the queue, sends '?' frames, and counts
    responses. No locking required.

    Key invariant: we send at most ONE pending '?' at a time. We wait for a
    response before sending the next. This prevents responses from getting
    mixed between status requests and G-code commands.
    """
    def __init__(self, log=None):
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = None
        self._log = log

        # Public counters (updated by main thread)
        self.sent = 0
        self.received = 0

        # Tracks whether we've sent a '?' and are waiting for response.
        # We only send the next '?' after receiving a response for the previous.
        self._pending_response = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop_event.is_set():
            self._queue.put("?")
            self._stop_event.wait(5.0)

    def send_pending(self, ser):
        """Send at most ONE pending status request.

        If a previous '?' is still awaiting response, do nothing.
        This prevents response mixing between consecutive status requests.
        Must be called from the main thread only.

        NOTE: We do NOT append '\\n' to the status request '?', because
        '?' is a real-time command. Appending '\\n' causes Grbl to process
        the empty line as a G-code command and return an extra 'ok',
        which pollutes the response stream and causes timeouts.
        """
        if self._pending_response:
            return
        try:
            cmd = self._queue.get_nowait()
        except queue.Empty:
            return

        self.sent += 1
        self._pending_response = True
        cmd = cmd.strip()
        frame = llp.encode(cmd.encode('utf-8'))
        ser.write(frame)
        ser.flush()
        if self._log:
            self._log.log_tx(repr(cmd))

    def process_response(self, response):
        """Count status responses (<...>). Call from main thread.

        Also clears the pending flag since we received a response.
        """
        if response.startswith('<'):
            self.received += 1
            self._pending_response = False

    @property
    def missed(self):
        m = self.sent - self.received
        return m if m >= 0 else 0

    def report(self):
        return (f"Status checks: {self.sent} sent, {self.received} received, "
                f"{self.missed} missed")


def send_frame(ser, text, log=None, force_no_newline=False):
    """Send text as an LLP frame over serial.
    
    The text is expected to end with '\\n' (G-code line terminator).
    If it doesn't, we add it to ensure proper processing.
    Set force_no_newline=True for real-time commands ('?', '!', '~', '\\x18')
    that should not have '\\n' appended.
    """
    if not force_no_newline and not text.endswith('\n'):
        text = text + '\n'
    
    frame = llp.encode(text.encode('utf-8'))
    ser.write(frame)
    ser.flush()
    if log:
        log.log_tx(repr(text))


def read_responses(ser, parser, timeout_sec, log=None):
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
                    if log:
                        log.log_rx(repr(text))
        time.sleep(0.02)
    return payloads


def clean_line(line):
    """Strip comments and whitespace from a G-code line.
    
    Returns the cleaned line without trailing newline comments or whitespace,
    but preserves the original line end for G-code processing.
    """
    stripped = line.rstrip('\r\n')
    stripped = stripped.strip()
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


def check_state(ser, parser, log=None):
    """Send '?' and return parsed status string, or None if no response."""
    send_frame(ser, "?", log=log, force_no_newline=True)
    resps = read_responses(ser, parser, timeout_sec=1.5, log=log)
    for r in resps:
        s = r.strip()
        if s.startswith('<'):
            return s
    return None




def check_buffer(ser, parser, log=None):
    """Send '#' and return buffer info string, or None if no response."""
    send_frame(ser, "#", log=log)
    resps = read_responses(ser, parser, timeout_sec=1.5, log=log)
    for r in resps:
        s = r.strip()
        if s.startswith('buf:'):
            return s
    return None


def reset_arduino(ser, parser, log=None):
    """Send ctrl-x reset and wait for startup."""
    print("[RESET] Sending ctrl-x (0x18) to reset Arduino...")
    if log:
        log.log_event("Sending ctrl-x (0x18) reset")
    send_frame(ser, "\x18", log=log)
    time.sleep(0.5)
    resps = read_responses(ser, parser, timeout_sec=2.0, log=log)
    for r in resps:
        print(f"  {r.strip()}")
    time.sleep(1.0)


def drain_responses(ser, parser, status_mon, drain_ms=100, log=None):
    """Drain all remaining responses from the serial buffer.

    This ensures no leftover responses from previous commands contaminate
    the next command's response handling.
    """
    count = 0
    start = time.time()
    while time.time() - start < drain_ms / 1000.0:
        remaining = read_responses(ser, parser, timeout_sec=0.05, log=log)
        if not remaining:
            break
        for r in remaining:
            status_mon.process_response(r)
            count += 1
    if count > 0 and log:
        log.log_event(f"Drained {count} leftover response(s)")


def process_responses(ser, parser, resps, status_mon, line_num, cmd,
                      got_ok, got_error, error_count, errors_detail,
                      last_keepalive):
    """Process a batch of responses. Returns updated tracking variables."""
    for r in resps:
        status_mon.process_response(r)

        r_stripped = r.strip()
        if r_stripped.lower() == 'ok':
            got_ok = True
        elif r_stripped.lower().startswith('error:'):
            got_error = True
            error_count += 1
            errors_detail.append((line_num, cmd, r_stripped))
        elif r_stripped.startswith('[KA]'):
            last_keepalive = time.time()

    return got_ok, got_error, error_count, last_keepalive


def wait_for_gcode_response(ser, parser, status_mon, line_num, cmd,
                            base_timeout, is_motion, error_count,
                            errors_detail, last_keepalive, log=None):
    """Wait for 'ok' or 'error:' from a G-code command.

    During the wait, also drains pending status requests from the monitor
    and sends them through the serial port. After each read_responses call,
    we also drain any leftover responses to prevent contamination.

    Returns: (got_ok, got_error, error_count, last_keepalive)
    """
    got_ok = False
    got_error = False
    start_wait = time.time()
    elapsed = 0.0

    while elapsed < base_timeout:
        status_mon.send_pending(ser)

        resps = read_responses(ser, parser, timeout_sec=0.3, log=log)

        got_ok, got_error, error_count, last_keepalive = process_responses(
            ser, parser, resps, status_mon, line_num, cmd,
            got_ok, got_error, error_count, errors_detail,
            last_keepalive)

        # Drain any leftover responses after processing current batch
        drain_start = time.time()
        while time.time() - drain_start < 0.05:
            remaining = read_responses(ser, parser, timeout_sec=0.01, log=log)
            if not remaining:
                break
            for r in remaining:
                status_mon.process_response(r)

        if got_ok or got_error:
            break
        elapsed = time.time() - start_wait

    if got_ok and log:
        log.log_event(f"Line {line_num}: got OK ({elapsed:.2f}s)")
    elif got_error and log:
        log.log_event(f"Line {line_num}: got ERROR ({elapsed:.2f}s)")
    elif log:
        log.log_event(f"Line {line_num}: TIMEOUT after {base_timeout}s")

    return got_ok, got_error, error_count, last_keepalive


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

    # Create log file
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    basename = os.path.splitext(os.path.basename(filepath))[0]
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{basename}_{ts}.log")
    log = StreamLogger(log_path)
    print(f"[LOG] Writing log to: {log_path}")
    log.log_event(f"File: {filepath}, Lines: {len(lines)}, Commands: {total_commands}")
    log.log_event(f"Port: {port} @ {baud}")

    with serial.Serial(port, baud, timeout=1) as ser:
        time.sleep(2.5)
        parser = llp.LLPParser()
        read_responses(ser, parser, timeout_sec=1.5, log=log)
        reset_arduino(ser, parser, log=log)

        print("Connected. Streaming started...")
        print("=" * 60)
        log.log_event("Streaming started")

        sent = 0
        ok_count = 0
        error_count = 0
        timeout_count = 0
        skipped_count = 0
        errors_detail = []
        last_keepalive = time.time()
        last_progress = time.time()

        # Start background status monitor
        status_mon = StatusMonitor(log=log)
        status_mon.start()
        print("[MONITOR] Status monitoring started (every 5s)")
        log.log_event("Status monitor started (every 5s)")

        print("[INIT] Sending $X (unlock if alarm)...")
        log.log_event("Sending $X (unlock)")
        send_frame(ser, "$X", log=log)
        resps = read_responses(ser, parser, timeout_sec=2.0, log=log)
        got_ok, got_error, error_count, last_keepalive = process_responses(
            ser, parser, resps, status_mon, None, "$X",
            False, False, error_count, errors_detail,
            last_keepalive)

        for r in resps:
            r_stripped = r.strip()
            if not r_stripped.startswith('[KA]'):
                if r_stripped.lower() != 'ok' and not r_stripped.lower().startswith('error:'):
                    print(f"  {r_stripped}")

        for line_num, cmd in commands:
            sent += 1
            token = get_cmd_token(cmd)

            if token in INTENTIONAL_STOP_CMDS:
                skipped_count += 1
                print(f"[SKIP] Line {line_num}: '{cmd}' (intentional stop command)")
                log.log_event(f"Line {line_num}: SKIPPED (intentional stop)")
                continue

            is_motion = is_motion_command(cmd)

            drain_responses(ser, parser, status_mon, drain_ms=100, log=log)

            status_mon.send_pending(ser)

            print(f"[SEND] Line {line_num}: '{cmd}'")
            send_frame(ser, cmd, log=log)

            base_timeout = 12.0 if is_motion else 5.0
            if token in ('G2', 'G3'):
                base_timeout = 25.0

            got_ok, got_error, error_count, last_keepalive = (
                wait_for_gcode_response(
                    ser, parser, status_mon, line_num, cmd,
                    base_timeout, is_motion, error_count,
                    errors_detail, last_keepalive, log=log))

            if not got_ok and not got_error:
                log.log_event(f"Line {line_num}: entering timeout handler")

                # Send '?' manually (no check_state) so we don't lose 'ok' responses
                send_frame(ser, "?", log=log, force_no_newline=True)
                resps = read_responses(ser, parser, timeout_sec=1.5, log=log)

                # Process ALL responses including 'ok' and 'error:'
                got_ok, got_error, error_count, last_keepalive = (
                    process_responses(
                        ser, parser, resps, status_mon,
                        line_num, cmd, got_ok, got_error,
                        error_count, errors_detail,
                        last_keepalive))

                # Check machine state from status responses
                status = None
                for r in resps:
                    s = r.strip()
                    if s.startswith('<'):
                        status = s
                        break

                if not got_ok and not got_error and status:
                    if any(s in status for s in ('Run', 'Hold', 'Cycle', 'Jog')):
                        extended_wait = 25.0 if is_motion else 10.0
                        log.log_event(f"Line {line_num}: machine busy ({status}), extending wait {extended_wait}s")
                        extra_start = time.time()
                        while time.time() - extra_start < extended_wait:
                            status_mon.send_pending(ser)

                            resps = read_responses(ser, parser, timeout_sec=0.5, log=log)

                            got_ok, got_error, error_count, last_keepalive = (
                                process_responses(
                                    ser, parser, resps, status_mon,
                                    line_num, cmd, got_ok, got_error,
                                    error_count, errors_detail,
                                    last_keepalive))

                            if got_ok or got_error:
                                break

                            # Check if any status response shows Idle
                            for r in resps:
                                s = r.strip()
                                if s.startswith('<') and 'Idle' in s:
                                    log.log_event(f"Line {line_num}: machine now idle")
                                    got_ok = True
                                    break

                            if got_ok:
                                break

            if not got_ok and not got_error:
                buf_info = check_buffer(ser, parser, log=log)
                if buf_info:
                    log.log_event(f"Line {line_num}: buffer responded, assuming ok")
                    got_ok = True
                else:
                    timeout_count += 1
                    log.log_event(f"Line {line_num}: TIMEOUT - no ok, no buffer response, last keepalive {time.time() - last_keepalive:.1f}s ago")
                    print(f"\n[TIMEOUT] Line {line_num}: '{cmd}'")
                    print(f"  No 'ok' after {base_timeout}s and no response to '#'")
                    print(f"  Last keepalive: {time.time() - last_keepalive:.1f}s ago")
                    print("=" * 60)
                    print("\nSTREAMING ABORTED - MCU HUNG")
                    print(f"  Last processed line: {line_num}")
                    print(f"  Command: {cmd}")
                    print(f"  Sent: {sent}/{total_commands}")
                    print(f"  OK: {ok_count}, Errors: {error_count}, Timeouts: {timeout_count}, Skipped: {skipped_count}")
                    status_mon.stop()
                    log.close()
                    return False

            if got_error:
                log.log_event(f"Line {line_num}: ERROR - {errors_detail[-1][2]}")
                print(f"[ERROR] Line {line_num}: '{cmd}' -> {errors_detail[-1][2]}")

            if got_ok:
                ok_count += 1
                print(f"[OK] Line {line_num} - Success")

            if time.time() - last_progress > 3.0 or sent % 100 == 0 or sent == total_commands:
                pct = (sent / total_commands) * 100
                print(f"  Progress: {sent}/{total_commands} ({pct:.1f}%) OK={ok_count} ERR={error_count} SKIP={skipped_count}")
                last_progress = time.time()

        print("\nStopping status monitor...")
        status_mon.stop()
        log.log_event("Status monitor stopped")

        print("\nDraining trailing responses...")
        time.sleep(0.5)
        resps = read_responses(ser, parser, timeout_sec=2.0, log=log)
        for r in resps:
            status_mon.process_response(r)
            r_stripped = r.strip()
            if r_stripped.lower() == 'ok':
                ok_count += 1
            elif r_stripped.lower().startswith('error:'):
                error_count += 1

        print("\nFinal health check...")
        buf_info = check_buffer(ser, parser, log=log)
        alive = buf_info is not None

        if buf_info:
            print(f"  Buffer: {buf_info}")

        print("=" * 60)
        print("STREAMING COMPLETE")
        print(f"  Total commands sent: {sent}/{total_commands}")
        print(f"  OK responses: {ok_count}")
        print(f"  Error responses: {error_count}")
        print(f"  Skipped (intentional stops): {skipped_count}")
        print(f"  Timeouts: {timeout_count}")
        print(f"  {status_mon.report()}")
        print(f"  MCU alive after streaming: {'YES' if alive else 'NO'}")

        log.log_event(f"Streaming complete: {sent}/{total_commands} sent, OK={ok_count}, "
                      f"ERR={error_count}, TIMEOUT={timeout_count}, SKIP={skipped_count}")

        if errors_detail:
            print("\n  Errors detail:")
            for line_num, cmd, err in errors_detail:
                print(f"    Line {line_num}: '{cmd}' -> {err}")

        log.close()
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
