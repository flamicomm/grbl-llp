#!/usr/bin/env python3
"""Profile R1 bench test — reliable command channel against a real MCU.

Speaks LLP v4 directly over a serial port (no ESP bridge in the path) and
exercises the receiver side of Profile R1 (llp-spec docs/RELIABILITY.md):

    COMMAND (channel 1): [SEQ][DATA...]
    ACK     (channel 2): [SEQ]
    HELLO   (channel 3): [session]

What it checks:

  clean      every command is ACKed and answered with exactly one "ok"
  duplicate  a retransmission is ACKed again and produces no second "ok"
  same-seq   a different payload with an already-seen SEQ is ACKed, not run
  wrap       SEQ 255 -> 0 is delivered twice

The sender side is the reference implementation in ``llp_rel.py`` (same state
machine as the C firmware), so this script also exercises it against hardware.

Nothing here moves an axis: every command is "G4 P0" (dwell zero) preceded by
"$X". Run it only with the machine idle.

Usage:
    python3 scripts/test_r1.py                    # /dev/ttyUSB0
    python3 scripts/test_r1.py --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # pyserial
import llp
import llp_link
import llp_rel as rel


class Bench:
    """Serial port + v4 parser + the reference R1 sender."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.02
        # On this CH340, opening the port toggles DTR and resets the MCU even
        # with dtr=False (F-013). That is why main() drains the boot banner with
        # `--wait-boot` before the handshake and then sends `$X`.
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.ser.dtr = False
        self.ser.rts = False

        self.parser = llp.Parser(capacity=llp.MAX_PAYLOAD)
        self.sender = rel.Sender()
        self.seq = 0
        self.acks: list[int] = []
        self.oks = 0
        self.text: list[str] = []
        self.retransmits = 0
        self.line_buf = ""

    def close(self) -> None:
        self.ser.close()

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000) & 0xFFFF

    # -- I/O -------------------------------------------------------------
    def write_frame(self, channel: int, payload: bytes) -> None:
        self.ser.write(llp.encode(channel, payload))
        self.ser.flush()

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            waiting = self.ser.in_waiting
            if waiting:
                for byte in self.ser.read(waiting):
                    for event in self.parser.feed(byte, self._now_ms()):
                        self._handle(event)
            if time.monotonic() >= deadline:
                return
            time.sleep(0.002)

    def drain(self, seconds: float) -> int:
        """Discard everything for `seconds` (boot banner, F-018 replay)."""
        return llp_link.drain_port(self.ser, seconds)

    def _handle(self, event) -> None:
        if event[0] != "frame":
            return
        _, channel, payload = event
        if channel == rel.CH_ACK and len(payload) == 1:
            seq = payload[0]
            self.acks.append(seq)
            if self.sender.on_ack(seq):
                self.seq = (self.seq + 1) & 0xFF
            return
        if channel != 0:
            return
        # A line can be split across frames: the MCU drains its TX ring in chunks, so a
        # ring wrap can cut a line in two. Reassemble by newline before counting.
        self.line_buf += payload.decode("latin1", "replace")
        while "\n" in self.line_buf:
            raw, self.line_buf = self.line_buf.split("\n", 1)
            line = raw.strip()
            if not line:
                continue
            if line == "ok":
                self.oks += 1
            if line == "ok" or line.startswith(("[MSG", "[DBG", "error")):
                self.text.append(line)

    # -- profile ---------------------------------------------------------
    def hello(self, session: int = rel.DEFAULT_SESSION) -> None:
        payload = rel.hello_payload(session)
        self.write_frame(rel.CH_HELLO, payload)
        self.write_frame(rel.CH_HELLO, payload)

    def command(self, data: bytes) -> tuple[bool, int]:
        """Send one command with R1 retransmission. Returns (acked, attempts)."""
        payload = rel.command_payload(self.seq, data)
        attempts = 0

        if not self.sender.start(self.seq, data, self._now_ms()):
            return False, 0

        self.write_frame(rel.CH_COMMAND, payload)
        attempts += 1
        while self.sender.active:
            action = self.sender.service(self._now_ms())
            if action == rel.TX_SEND:
                self.write_frame(rel.CH_COMMAND, payload)
                attempts += 1
                self.retransmits += 1
            elif action == rel.TX_EXHAUSTED:
                # Release the window so a later command can start, and burn this
                # SEQ: if any ACK was ever seen, the MCU may have consumed it and
                # only lost the ACK, so reusing it would be deduped silently.
                # The CH0 replay only runs when no ACK was ever seen.
                self.sender.abort()
                self.seq = (self.seq + 1) & 0xFF
                return False, attempts
            self.pump(0.005)
        return True, attempts


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, label: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            self.failed += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--wait-boot", type=float, default=4.5,
                    help="seconds to drain the port after opening it: the CH340 "
                         "resets the MCU on open and the boot banner takes ~3.9 s "
                         "(F-013/F-014)")
    args = ap.parse_args()

    link = Bench(args.port, args.baud)
    report = Report()

    try:
        discarded = link.drain(args.wait_boot)
        print(f"  boot: {discarded} bytes descartados en {args.wait_boot:g}s")

        print("== preparación ==")
        link.hello()
        acked, attempts = link.command(b"$X\n")
        report.check(acked, f"$X aceptado (seq 0, {attempts} intento/s)")
        # Drain the "ok" of $X before zeroing the counters. The ACK of a command is sent
        # before the line is executed, so its "ok" can still be in flight and would be
        # counted as an extra one in T1.
        link.pump(0.3)
        link.acks.clear()
        link.oks = 0

        print("== T1: entrega limpia ==")
        sent = 0
        for _ in range(10):
            acked, _ = link.command(b"G4 P0\n")
            if not acked:
                break
            sent += 1
        link.pump(0.3)
        report.check(sent == 10 and link.oks == 10,
                     f"10 comandos -> 10 ACK y 10 ok (ok={link.oks})")

        print("== T2: ACK perdido (duplicado del mismo SEQ) ==")
        last_seq = (link.seq - 1) & 0xFF
        link.acks.clear()
        link.oks = 0
        link.write_frame(rel.CH_COMMAND, rel.command_payload(last_seq, b"G4 P0\n"))
        link.pump(0.3)
        report.check(link.acks == [last_seq], f"duplicado re-ACKeado (acks={link.acks})")
        report.check(link.oks == 0, f"duplicado NO ejecutado (ok={link.oks})")

        print("== T3: mismo SEQ con otro payload ==")
        link.acks.clear()
        link.oks = 0
        link.write_frame(rel.CH_COMMAND, rel.command_payload(last_seq, b"G4 P0.1\n"))
        link.pump(0.3)
        report.check(link.acks == [last_seq], f"ACK igual (acks={link.acks})")
        report.check(link.oks == 0, f"payload distinto descartado (ok={link.oks})")

        print("== T4: wrap de SEQ 255 -> 0 ==")
        link.acks.clear()
        link.oks = 0
        link.seq = 255
        ok255, _ = link.command(b"G4 P0\n")
        ok0, _ = link.command(b"G4 P0\n")
        link.pump(0.3)
        report.check(ok255 and ok0 and link.oks == 2,
                     f"255 y 0 entregados (ok={link.oks})")

        print("== estado del enlace ==")
        link.pump(10.5)  # wait for the keep-alive block
        for line in link.text:
            if line.startswith("[DBG:"):
                print(f"  {line}")
        print(f"  retransmisiones de este arnés: {link.retransmits}")

    finally:
        link.close()

    print()
    if report.failed:
        print(f"FALLARON {report.failed} verificaciones")
        return 1
    print("Todas las verificaciones pasaron")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
