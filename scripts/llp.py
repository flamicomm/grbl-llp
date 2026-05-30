#!/usr/bin/env python3
"""
llp.py - LLP (Layered Link Protocol) v3.0.0 Python implementation

Encodes and decodes LLP frames for the ESP8266 LLP Bridge integration tests.

Wire format (transport frame):
  [0xAA][0x55][LEN_L][LEN_H][PAYLOAD...][CRC_L][CRC_H]

Byte stuffing: every 0xAA byte in LEN, PAYLOAD or CRC is written as 0xAA 0x00.
An unexpected 0xAA 0x55 sequence inside a frame signals a resync event.

Payload format (layer chain):
  [LAYER_ID][META_LEN][METADATA...] ... [0x00][RAW APPLICATION DATA]

Layer ID 0x00 (FinalNode) signals end of layer chain, raw bytes follow.
"""

import struct
import time
import binascii
from typing import Optional

# Protocol constants
LLP_MAGIC_1 = 0xAA
LLP_MAGIC_2 = 0x55
LLP_LAYER_ID_FINAL = 0x00
LLP_MAX_PAYLOAD = 128
LLP_FRAME_TIMEOUT_MS = 2000


def crc16_ccitt(data: bytes) -> int:
    """
    Calculate CRC16-CCITT checksum over a byte string.
    Uses binascii.crc_hqx which implements CRC-16-CCITT (polynomial 0x1021, init 0xFFFF).
    """
    return binascii.crc_hqx(data, 0xFFFF)


def build_final_payload(raw_data: bytes) -> bytes:
    """
    Wrap raw application data in a FinalNode layer.
    Returns: [0x00][RAW_DATA...]
    """
    return bytes([LLP_LAYER_ID_FINAL]) + raw_data


def build_frame(llp_payload: bytes) -> bytes:
    """
    Encode an LLP payload into a wire-format frame with byte stuffing.
    Returns: [0xAA][0x55][LEN_L][LEN_H][STUFFED_PAYLOAD...][CRC_L][CRC_H]
    """
    if len(llp_payload) > LLP_MAX_PAYLOAD:
        raise ValueError(f"Payload too large: {len(llp_payload)} > {LLP_MAX_PAYLOAD}")

    # Calculate CRC over: magic + length + payload (all unstuffed)
    length_le = struct.pack('<H', len(llp_payload))
    crc_data = bytes([LLP_MAGIC_1, LLP_MAGIC_2]) + length_le + llp_payload
    crc = crc16_ccitt(crc_data)

    # Build output frame
    output = bytearray()
    output.append(LLP_MAGIC_1)
    output.append(LLP_MAGIC_2)

    for byte in length_le:
        output.append(byte)
        if byte == LLP_MAGIC_1:
            output.append(0x00)

    for byte in llp_payload:
        output.append(byte)
        if byte == LLP_MAGIC_1:
            output.append(0x00)

    crc_le = struct.pack('<H', crc)
    for byte in crc_le:
        output.append(byte)
        if byte == LLP_MAGIC_1:
            output.append(0x00)

    return bytes(output)


def encode(raw_data: bytes) -> bytes:
    """
    Convenience function: encode raw data as a complete LLP frame.
    Combines build_final_payload() and build_frame().
    """
    llp_payload = build_final_payload(raw_data)
    return build_frame(llp_payload)


class LLPParser:
    """
    Stateful LLP frame parser. Feed raw bytes via process_byte() and
    call get_final_payload() after process_byte() returns 1.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 0  # 0=WAIT_MAGIC1, 1=WAIT_MAGIC2, 2=LEN_L, 3=LEN_H, 4=PAYLOAD, 5=CRC_L, 6=CRC_H
        self.escape_pending = False
        self.payload_len = 0
        self.payload = bytearray()
        self.crc_received = 0
        self.crc_calculated = 0xFFFF
        self.last_byte_time = 0
        self.error_code = None
        self.frames_ok = 0
        self.frames_error = 0
        self.timeouts = 0

    def process_byte(self, byte: int, current_ms: Optional[int] = None) -> int:
        """
        Feed one raw byte into the parser.

        Returns:
          0 = incomplete frame (still building)
          1 = complete frame (call get_final_payload() to get it)
         -1 = error or resync
        """
        if current_ms is None:
            current_ms = int(time.time() * 1000)

        # Timeout check (not in magic states)
        if self.state not in (0, 1):
            if self.last_byte_time > 0 and (current_ms - self.last_byte_time) > LLP_FRAME_TIMEOUT_MS:
                self.error_code = 'TIMEOUT'
                self.timeouts += 1
                self.reset()
                self.last_byte_time = current_ms
                if byte == LLP_MAGIC_1:
                    self.state = 1
                return -1

        self.last_byte_time = current_ms

        # Handle escape sequences (not in magic states)
        if self.state not in (0, 1):
            if self.escape_pending:
                self.escape_pending = False
                if byte == LLP_MAGIC_2:
                    self.error_code = 'SYNC'
                    self.frames_error += 1
                    self.crc_calculated = 0xFFFF
                    self.crc_calculated = binascii.crc_hqx(
                        bytes([LLP_MAGIC_1, LLP_MAGIC_2]), self.crc_calculated
                    )
                    self.payload = bytearray()
                    self.state = 2
                    return -1
                elif byte == 0x00:
                    byte = LLP_MAGIC_1
                else:
                    self.error_code = 'SYNC'
                    self.frames_error += 1
                    self.reset()
                    return -1
            elif byte == LLP_MAGIC_1:
                self.escape_pending = True
                return 0

        # State machine
        if self.state == 0:  # WAIT_MAGIC1
            if byte == LLP_MAGIC_1:
                self.state = 1
            return 0

        if self.state == 1:  # WAIT_MAGIC2
            if byte == LLP_MAGIC_2:
                self.crc_calculated = 0xFFFF
                self.crc_calculated = binascii.crc_hqx(
                    bytes([LLP_MAGIC_1, LLP_MAGIC_2]), self.crc_calculated
                )
                self.state = 2
            elif byte == LLP_MAGIC_1:
                self.state = 1
            else:
                self.state = 0
            return 0

        if self.state == 2:  # READ_LEN_L
            self.payload_len = byte
            self.crc_calculated = binascii.crc_hqx(bytes([byte]), self.crc_calculated)
            self.state = 3
            return 0

        if self.state == 3:  # READ_LEN_H
            self.payload_len |= byte << 8
            self.crc_calculated = binascii.crc_hqx(bytes([byte]), self.crc_calculated)
            if self.payload_len > LLP_MAX_PAYLOAD:
                self.error_code = 'PAYLOAD_LEN'
                self.frames_error += 1
                self.reset()
                return -1
            self.payload = bytearray()
            self.state = 4 if self.payload_len > 0 else 5
            return 0

        if self.state == 4:  # READ_PAYLOAD
            self.payload.append(byte)
            self.crc_calculated = binascii.crc_hqx(bytes([byte]), self.crc_calculated)
            if len(self.payload) == self.payload_len:
                self.state = 5
            return 0

        if self.state == 5:  # READ_CRC_L
            self.crc_received = byte
            self.state = 6
            return 0

        if self.state == 6:  # READ_CRC_H
            self.crc_received |= byte << 8
            if self.crc_received != self.crc_calculated:
                self.error_code = 'CHECKSUM'
                self.frames_error += 1
                self.reset()
                return -1
            self.frames_ok += 1
            self.state = 0
            self.escape_pending = False
            return 1

        # Should not reach here
        self.reset()
        return -1

    def get_final_payload(self) -> bytes:
        """
        Extract raw application data from the last parsed frame.
        Returns bytes after the FinalNode (0x00) marker.
        """
        raw_payload = bytes(self.payload)
        pos = 0
        while pos < len(raw_payload):
            layer_id = raw_payload[pos]
            if layer_id == LLP_LAYER_ID_FINAL:
                return raw_payload[pos + 1:]
            pos += 1
        return b''


def decode(raw_bytes: bytes, parser: Optional[LLPParser] = None):
    """
    Decode a sequence of raw bytes into LLP payloads.

    Args:
        raw_bytes: raw bytes received from TCP/Serial
        parser: existing LLPParser instance to reuse (creates new if None)

    Returns:
        Tuple of (list_of_payloads, parser_instance)
    """
    if parser is None:
        parser = LLPParser()

    payloads = []
    for byte in raw_bytes:
        result = parser.process_byte(byte)
        if result == 1:
            payloads.append(parser.get_final_payload())
    return payloads, parser


# =============================================================================
# Command line tool for debugging
# =============================================================================

def main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 llp.py encode <text>  OR  python3 llp.py decode <hex>")
        print("       python3 llp.py parse <hex>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'encode':
        data = sys.argv[2].encode('utf-8')
        frame = encode(data)
        print("Encoded frame (hex):", frame.hex().upper())

    elif cmd == 'parse':
        hex_str = sys.argv[2].replace(' ', '').replace(':', '')
        raw_bytes = bytes.fromhex(hex_str)
        parser = LLPParser()
        for i, byte in enumerate(raw_bytes):
            result = parser.process_byte(byte)
            if result == 1:
                payload = parser.get_final_payload()
                print(f"Frame {parser.frames_ok}: payload={payload} ({len(payload)} bytes)")
                print(f"  Raw payload (hex): {bytes(parser.payload).hex().upper()}")
            elif result == -1:
                print(f"Byte {i}: error={parser.error_code}")

    elif cmd == 'decode':
        hex_str = sys.argv[2].replace(' ', '').replace(':', '')
        raw_bytes = bytes.fromhex(hex_str)
        payloads, _ = decode(raw_bytes)
        for p in payloads:
            print("Payload:", p.decode('utf-8', errors='replace'))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == '__main__':
    main()
