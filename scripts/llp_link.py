#!/usr/bin/env python3
"""LLP v4 link with Profile R1 (reliable command channel) over a serial port.

Wraps a pyserial-compatible port with a v4 parser and the R1 state machines:

    send(text)     G-code / system commands on channel 1, waiting for the ACK
    send_realtime  '?', '!', '~', 0x18 ... on channel 0 (never waits)
    poll(seconds)  channel-0 payloads as text (ok, error:, <status>, [DBG] ...)
    hello()        session start on channel 3 (sent twice)

Auto-detection: one unacknowledged command is not proof of old firmware — right
after the port opens the MCU may still be booting (F-013/F-014). Only when the
first ``FALLBACK_CONFIRM_COMMANDS`` commands go unacknowledged **and** no ACK was
ever received in the session does the link declare "firmware without Profile R1",
resend those pending commands on plain channel 0 and say so. A link that already
ACKed a command is never degraded by later losses.

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

# One command without an ACK does not prove the firmware is old: just after the
# port is opened the MCU is still booting (F-013/F-014). This many consecutive
# unacknowledged commands, with no ACK at all in the session, are needed before
# declaring "firmware without R1".
FALLBACK_CONFIRM_COMMANDS = 2


def is_realtime_byte(byte: int) -> bool:
    return byte in (0x18, ord('?'), ord('!'), ord('~')) or byte >= REALTIME_MIN


def drain_port(ser, seconds: float) -> int:
    """Discard everything the port delivers for `seconds`; returns bytes dropped.

    Opening the CH340 resets the MCU: the boot banner can take ~3.9 s to flush
    and on some opens the adapter replays ~1.1 MB of keep-alive frames (F-018)
    on top of the usual startup garbage (F-015). Feeding that through the LLP
    parser is useless work, so this helper reads and throws it away. A value
    `seconds <= 0` performs a single non-blocking read. Fakes without
    `in_waiting` degrade to zero, like `_write()` tolerates fakes without
    `flush()`.
    """
    discarded = 0
    deadline = time.monotonic() + seconds
    while seconds <= 0 or time.monotonic() < deadline:
        waiting = getattr(ser, "in_waiting", 0)
        if waiting:
            discarded += len(ser.read(waiting))
        if seconds <= 0:
            break
        time.sleep(0.002)
    return discarded


@dataclass
class LinkStats:
    commands: int = 0
    acked: int = 0
    retries: int = 0
    unacked: int = 0
    fallback_ch0: int = 0
    responses: int = 0


class Link:
    """Reliable command link over an already-open serial port.

    `fallback_confirm` (>= 1) is the number of consecutive unacknowledged
    commands, with no ACK at all in the session, that confirm the firmware has
    no Profile R1 before falling back to channel 0. One lost command on a
    freshly opened port only means the MCU is still booting (F-013/F-014).
    `acks_seen` counts well-formed CH_ACK frames even when they arrive too late
    to close a window, because an orphan ACK still proves the firmware speaks R1.
    """

    def __init__(self, ser, log=None, *,
                 timeout_ms: int = rel.DEFAULT_ACK_TIMEOUT_MS,
                 max_retries: int = rel.DEFAULT_MAX_RETRIES,
                 backoff_ms: int = rel.DEFAULT_BACKOFF_MS,
                 reliable: bool = True,
                 fallback_confirm: int = FALLBACK_CONFIRM_COMMANDS) -> None:
        if fallback_confirm < 1:
            raise ValueError("fallback_confirm must be >= 1")
        self.ser = ser
        self.log = log
        self.parser = llp.Parser(capacity=llp.MAX_PAYLOAD)
        self.sender = rel.Sender(timeout_ms, max_retries, backoff_ms)
        self.receiver = rel.Receiver()
        self.reliable = reliable
        self.fallback_confirm = fallback_confirm
        self.seq = 0
        self.acks_seen = 0
        self.stats = LinkStats()
        self._pending: List[str] = []
        self._unacked_commands: List[bytes] = []

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

    def drain(self, seconds: float) -> int:
        """Discard bytes for `seconds` without parsing them; returns the count."""
        return drain_port(self.ser, seconds)

    def _handle_event(self, event) -> None:
        if event[0] != "frame":
            return
        _, channel, payload = event
        if channel == rel.CH_ACK and len(payload) == 1:
            self.acks_seen += 1
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
        failed_seq = self.seq
        if self._send_reliable(data):
            self._unacked_commands.clear()
            return "acked"

        self.stats.unacked += 1
        self._unacked_commands.append(data)

        if len(self._unacked_commands) >= self.fallback_confirm:
            # An ACK can still be sitting in the OS buffer after the sender
            # expired; read it now so `acks_seen` sees it before deciding.
            self.pump(0)

        if self.acks_seen > 0:
            # The link already proved it speaks R1 (even a late, orphan ACK
            # counts): a lost command is a lost command, never a reason to
            # degrade the whole session (F-014). Fallback is impossible from
            # now on, so keep nothing queued.
            self._unacked_commands.clear()
            self._log("ERROR", f"command SEQ {failed_seq} not acknowledged")
            return "unacked"

        if len(self._unacked_commands) < self.fallback_confirm:
            self._log(
                "WARN",
                f"command SEQ {failed_seq} not acknowledged; waiting for "
                f"confirmation ({len(self._unacked_commands)}/"
                f"{self.fallback_confirm})",
            )
            return "unacked"

        # No ACK was ever received in the session, not even an orphan one: the
        # firmware ignores CH1 (no Profile R1), so none of these pending commands
        # ran. An R1 firmware whose whole ACK path is dead looks identical from
        # here; resending on CH0 may execute a command twice. That residual risk
        # is what F-014 accepts, and why two commands (not one) are required to
        # confirm the fallback.
        self.reliable = False
        self._log("WARN", "firmware without Profile R1: falling back to channel 0")
        for pending in self._unacked_commands:
            self._write(STREAM_CHANNEL, pending)
            self.stats.fallback_ch0 += 1
        self._unacked_commands.clear()
        return "sent"

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
                # Release the window so a later command can start, and burn this
                # SEQ: if any ACK was ever seen, the MCU may have consumed it and
                # only lost the ACK, so reusing it would be deduped silently.
                # The CH0 replay only runs when no ACK was ever seen.
                self.sender.abort()
                self.seq = (self.seq + 1) & 0xFF
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
                 drop_commands: int = 0, drop_acks: int = 0,
                 silent: bool = False) -> None:
        self.rx_from_pc = b""
        self.tx_to_pc = b""
        self.parser = llp.Parser(capacity=llp.MAX_PAYLOAD)
        self.receiver = rel.Receiver()
        self.support_r1 = support_r1
        self.drop_commands = drop_commands     # drop the N-th command frame
        self.drop_acks = drop_acks             # drop the N-th ACK
        self.silent = silent                   # ignore CH1: no delivery, no ACK
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
            if self.silent:
                return                     # link stalled: no delivery, no ACK
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


class _Log:
    """Minimal event collector for the self-test."""

    def __init__(self) -> None:
        self.events: List[str] = []

    def log_event(self, message: str) -> None:
        self.events.append(message)


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

    print("== firmware sin R1 (confirmación antes del fallback) ==")
    port = FakePort(support_r1=False)
    log = _Log()
    link = Link(port, log=log)
    link.hello()
    result = link.send("G1 X3")
    check(f"un comando sin ACK no degrada ({result})", result == "unacked")
    check("el enlace sigue en R1", link.reliable is True)
    check("todavía sin fallback", link.stats.fallback_ch0 == 0)
    check("nada entregado aún", port.delivered_lines == [])
    check("el WARN loguea el SEQ que falló",
          any("SEQ 0 not acknowledged" in event for event in log.events))
    result = link.send("G1 X4")
    check(f"el segundo comando confirma el fallback ({result})", result == "sent")
    check("enlace degradado a CH0", not link.reliable)
    check("dos comandos reenviados por CH0", link.stats.fallback_ch0 == 2)
    check("replay en orden", port.delivered_lines == ["G1 X3", "G1 X4"])
    check("dos ok recibidos", link.poll(0.05) == ["ok\n", "ok\n"])

    print("== boot lento (el enlace se recupera) ==")
    port = FakePort(support_r1=False)
    link = Link(port)
    link.hello()
    result = link.send("G1 X5")
    check(f"comando durante el boot sin ACK ({result})", result == "unacked")
    port.support_r1 = True
    result = link.send("G1 X6")
    check(f"comando posterior ACKeado ({result})", result == "acked")
    check("el enlace sigue en R1", link.reliable is True)
    check("sin fallback", link.stats.fallback_ch0 == 0)
    check("solo se entregó el comando bueno", port.delivered_lines == ["G1 X6"])
    check("el SEQ fallido se quemó", link.seq == 2)

    print("== enlace ya probado (nunca degrada) ==")
    port = FakePort()
    log = _Log()
    link = Link(port, log=log)
    link.hello()
    result = link.send("ok")
    check(f"comando inicial ACKeado ({result})", result == "acked")
    port.silent = True
    first = link.send("G1 X7")
    second = link.send("G1 X8")
    check(f"primera pérdida sin ACK ({first})", first == "unacked")
    check(f"segunda pérdida sin ACK ({second})", second == "unacked")
    check("el enlace no se degrada", link.reliable is True)
    check("sin fallback", link.stats.fallback_ch0 == 0)
    check("el ERROR loguea el SEQ que falló",
          any("SEQ 1 not acknowledged" in event for event in log.events))

    print("== ACK tardío (prueba de R1) ==")
    port = FakePort(support_r1=False)
    link = Link(port)
    link.hello()
    result = link.send("G1 X9")
    check(f"primero sin ACK ({result})", result == "unacked")
    port.tx_to_pc += llp.encode(rel.CH_ACK, rel.ack_payload(0))
    link.pump(0.05)
    check("el ACK huérfano se contó", link.acks_seen == 1)
    check("un ACK tardío no cierra ventana", link.stats.acked == 0)
    result = link.send("G1 X10")
    check(f"el ACK tardío evita el fallback ({result})", result == "unacked")
    check("el enlace sigue en R1", link.reliable is True)
    check("sin fallback", link.stats.fallback_ch0 == 0)

    print("== drenaje de la basura de apertura ==")
    port = FakePort()
    link = Link(port)
    port.tx_to_pc += b"\xaa\x55" * 100
    dropped = link.drain(0.02)
    check(f"drenó la basura ({dropped} bytes)", dropped == 200)
    check("el puerto quedó vacío", port.in_waiting == 0)
    check("el parser no vio nada", link.poll(0) == [])
    check("ninguna trama decodificada", link.parser.frames_ok == 0)
    check("ningún error de trama", link.parser.frames_error == 0)

    print("== bordes ==")
    try:
        Link(FakePort(), fallback_confirm=0)
        rejected = False
    except ValueError:
        rejected = True
    check("fallback_confirm < 1 se rechaza", rejected)

    port = FakePort(support_r1=False)
    link = Link(port, fallback_confirm=1)
    link.hello()
    result = link.send("G1 X20")
    check(f"fallback_confirm=1 cae en el primer comando ({result})", result == "sent")
    check("un comando reenviado por CH0", link.stats.fallback_ch0 == 1)
    check("entregado por CH0", port.delivered_lines == ["G1 X20"])

    port = FakePort()
    link = Link(port)
    port.tx_to_pc += b"abc"
    check("drain(0) descarta una vez", link.drain(0) == 3)
    check("objeto sin in_waiting no rompe", drain_port(object(), 0) == 0)

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
