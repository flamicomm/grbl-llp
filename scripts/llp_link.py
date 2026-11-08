#!/usr/bin/env python3
"""LLP v4 link with Profile R1 (reliable command channel) over a serial port.

Wraps a pyserial-compatible port with a v4 parser and the R1 state machines:

    send(text)     G-code / system commands on channel 1, waiting for the ACK
    send_realtime  '?', '!', '~', 0x18 ... on channel 0 (never waits)
    poll(seconds)  channel-0 payloads as text (ok, error:, <status>, [DBG] ...)
    hello()        session start on channel 3 (sent twice)

Auto-detection: if the first command is not acknowledged (older firmware that
only understands channel 0), the link falls back to plain channel 0 and says so,
so the same script works with both firmwares.

Run the built-in simulation (no hardware needed):

    python3 llp_link.py --self-test
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llp
import llp_rel as rel

STREAM_CHANNEL = 0

# Sent on channel 0, never "ok"-acknowledged and idempotent.
REALTIME_MIN = 0x80


def is_realtime_byte(byte: int) -> bool:
    return byte in (0x18, ord('?'), ord('!'), ord('~')) or byte >= REALTIME_MIN


@dataclass
class LinkStats:
    commands: int = 0
    acked: int = 0
    retries: int = 0
    unacked: int = 0
    fallback_ch0: int = 0
    responses: int = 0


class Link:
    """Reliable command link over an already-open serial port."""

    def __init__(self, ser, log=None, *,
                 timeout_ms: int = rel.DEFAULT_ACK_TIMEOUT_MS,
                 max_retries: int = rel.DEFAULT_MAX_RETRIES,
                 backoff_ms: int = rel.DEFAULT_BACKOFF_MS,
                 reliable: bool = True) -> None:
        self.ser = ser
        self.log = log
        self.parser = llp.Parser(capacity=llp.MAX_PAYLOAD)
        self.sender = rel.Sender(timeout_ms, max_retries, backoff_ms)
        self.receiver = rel.Receiver()
        self.reliable = reliable
        self.seq = 0
        self.stats = LinkStats()
        self._pending: List[str] = []

    # -- low level -------------------------------------------------------
    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000) & 0xFFFF

    def _write(self, channel: int, payload: bytes) -> None:
        self.ser.write(llp.encode(channel, payload))
        try:
            self.ser.flush()
        except AttributeError:  # fakes without flush()
            pass

    def _log(self, level: str, message: str) -> None:
        if self.log is not None:
            self.log.log_event(f"[{level}] {message}")

    def pump(self, seconds: float = 0.0) -> None:
        """Read frames for `seconds` (0 = whatever is already buffered)."""
        deadline = time.monotonic() + seconds
        while True:
            waiting = getattr(self.ser, "in_waiting", 0)
            if waiting:
                for byte in self.ser.read(waiting):
                    for event in self.parser.feed(byte, self._now_ms()):
                        self._handle_event(event)
            if time.monotonic() >= deadline:
                return
            time.sleep(0.002)

    def _handle_event(self, event) -> None:
        if event[0] != "frame":
            return
        _, channel, payload = event
        if channel == rel.CH_ACK and len(payload) == 1:
            if self.sender.on_ack(payload[0]):
                self.stats.acked += 1
                self.seq = (self.seq + 1) & 0xFF
            return
        if channel == STREAM_CHANNEL:
            text = payload.decode("utf-8", errors="replace")
            self._pending.append(text)
            self.stats.responses += 1
        # HELLO is outbound only; other channels are reserved.

    # -- profile helpers -------------------------------------------------
    def hello(self, session: int = rel.DEFAULT_SESSION) -> None:
        """Start a session so the MCU forgets its last SEQ (sent twice)."""
        payload = rel.hello_payload(session)
        self._write(rel.CH_HELLO, payload)
        self._write(rel.CH_HELLO, payload)

    def send_realtime(self, text: str) -> None:
        self._write(STREAM_CHANNEL, text.encode("utf-8"))

    def send(self, text: str, force_no_newline: bool = False) -> str:
        """Send one command. Returns "acked", "sent" or "unacked"."""
        if not force_no_newline and not text.endswith("\n"):
            text += "\n"
        data = text.encode("utf-8")

        if not self.reliable or (len(data) == 1 and is_realtime_byte(data[0])):
            self._write(STREAM_CHANNEL, data)
            return "sent"

        if len(data) > rel.MAX_DATA:
            # Cannot be a valid grbl line anyway; keep the old behaviour.
            self._write(STREAM_CHANNEL, data)
            self.stats.fallback_ch0 += 1
            return "sent"

        self.stats.commands += 1
        if self._send_reliable(data):
            return "acked"

        self.stats.unacked += 1
        if self.stats.commands == 1:
            # First command not acknowledged: older firmware, no R1 on CH1.
            self.reliable = False
            self._log("WARN", "firmware without Profile R1: falling back to channel 0")
            self._write(STREAM_CHANNEL, data)
            self.stats.fallback_ch0 += 1
            return "sent"

        # Never resend a later command on channel 0: it might have executed and
        # only the ACK was lost. The caller's ok/timeout logic decides.
        self._log("ERROR", f"command SEQ {self.seq} not acknowledged")
        return "unacked"

    def _send_reliable(self, data: bytes) -> bool:
        if not self.sender.start(self.seq, data, self._now_ms()):
            return False

        self._write(rel.CH_COMMAND, self.sender.payload)
        while self.sender.active:
            action = self.sender.service(self._now_ms())
            if action == rel.TX_SEND:
                self._write(rel.CH_COMMAND, self.sender.payload)
                self.stats.retries += 1
            elif action == rel.TX_EXHAUSTED:
                return False
            self.pump(0.002)
        return True

    def poll(self, seconds: float) -> List[str]:
        """Pump for `seconds` and return the channel-0 payloads as text."""
        self.pump(seconds)
        out, self._pending = self._pending, []
        return out


# ---------------------------------------------------------------------------
# Simulation (--self-test): a fake MCU that speaks the profile
# ---------------------------------------------------------------------------

class FakePort:
    """pyserial-compatible port backed by a simulated Arduino."""

    def __init__(self, support_r1: bool = True,
                 drop_commands: int = 0, drop_acks: int = 0) -> None:
        self.rx_from_pc = b""
        self.tx_to_pc = b""
        self.parser = llp.Parser(capacity=llp.MAX_PAYLOAD)
        self.receiver = rel.Receiver()
        self.support_r1 = support_r1
        self.drop_commands = drop_commands     # drop the N-th command frame
        self.drop_acks = drop_acks             # drop the N-th ACK
        self.commands_seen = 0
        self.acks_seen = 0
        self.delivered_lines: List[str] = []
        self.line_buffer = ""

    # pyserial surface
    @property
    def in_waiting(self) -> int:
        return len(self.tx_to_pc)

    def read(self, size: int) -> bytes:
        data, self.tx_to_pc = self.tx_to_pc[:size], self.tx_to_pc[size:]
        return data

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        for byte in data:
            for event in self.parser.feed(byte, int(time.monotonic() * 1000) & 0xFFFF):
                if event[0] == "frame":
                    self._handle(event[1], event[2])
        return len(data)

    # simulated MCU
    def _handle(self, channel: int, payload: bytes) -> None:
        if channel == rel.CH_HELLO:
            self.receiver.reset()
            return

        if channel == rel.CH_COMMAND and self.support_r1:
            self.commands_seen += 1
            if self.commands_seen == self.drop_commands:
                return                     # simulated loss: no delivery, no ACK
            seq, data = payload[0], payload[1:]
            if self.receiver.accept(seq):
                self._ingest(data)
            self._send_ack(seq)
            return

        if channel == STREAM_CHANNEL:
            self._ingest(payload)

    def _send_ack(self, seq: int) -> None:
        self.acks_seen += 1
        if self.acks_seen == self.drop_acks:
            return                         # simulated loss: the ACK never arrives
        self.tx_to_pc += llp.encode(rel.CH_ACK, rel.ack_payload(seq))

    def _ingest(self, data: bytes) -> None:
        self.line_buffer += data.decode("utf-8", errors="replace")
        while "\n" in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split("\n", 1)
            if line.strip():
                self.delivered_lines.append(line)
                self.tx_to_pc += llp.encode(STREAM_CHANNEL, b"ok\n")


def _self_test() -> int:
    failures = 0

    def check(label: str, condition: bool) -> None:
        nonlocal failures
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
        if not condition:
            failures += 1

    print("== entrega limpia ==")
    port = FakePort()
    link = Link(port)
    link.hello()
    result = link.send("G1 X0")
    check(f"comando ACKeado ({result})", result == "acked")
    check("una línea entregada", port.delivered_lines == ["G1 X0"])
    check("un ok recibido", link.poll(0.05) == ["ok\n"])
    check("sin retransmisiones", link.stats.retries == 0)

    print("== ACK perdido (dedup) ==")
    port = FakePort(drop_acks=1)
    link = Link(port)
    link.hello()
    result = link.send("G1 X1")
    check(f"comando ACKeado tras reintento ({result})", result == "acked")
    check("retransmitió", link.stats.retries == 1)
    check("entregado una sola vez", port.delivered_lines == ["G1 X1"])
    check("un solo ok", link.poll(0.05) == ["ok\n"])

    print("== comando perdido (retransmisión) ==")
    port = FakePort(drop_commands=1)
    link = Link(port)
    link.hello()
    result = link.send("G1 X2")
    check(f"comando ACKeado ({result})", result == "acked")
    check("retransmitió", link.stats.retries == 1)
    check("entregado una sola vez", port.delivered_lines == ["G1 X2"])

    print("== firmware viejo (auto-detección) ==")
    port = FakePort(support_r1=False)
    link = Link(port)
    link.hello()
    result = link.send("G1 X3")
    check(f"cae a CH0 ({result})", result == "sent")
    check("sin R1 en el enlace", not link.reliable)
    check("entregado por CH0", port.delivered_lines == ["G1 X3"])
    check("un ok recibido", link.poll(0.05) == ["ok\n"])

    print("== realtime no espera ACK ==")
    port = FakePort()
    link = Link(port)
    link.send("?", force_no_newline=True)
    check("sin comandos contados", link.stats.commands == 0)

    print()
    if failures:
        print(f"FAIL  {failures} check(s) failed")
        return 1
    print("ok    llp_link self-test: all checks pass")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="run the simulated MCU checks (no hardware)")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
