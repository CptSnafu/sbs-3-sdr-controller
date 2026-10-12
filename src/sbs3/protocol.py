"""Observed DLE-framed SBS-3 messages.

The checksum field is deliberately treated as opaque until its algorithm is
verified from independent captures.
"""

from dataclasses import dataclass
from typing import Optional
from .integrity import crc16

DLE = 0x10
STX = 0x02
ETX = 0x03


@dataclass(frozen=True)
class Frame:
    payload: bytes
    integrity: bytes

    @property
    def sequence(self) -> Optional[int]:
        return self.payload[1] if len(self.payload) >= 2 and self.payload[0] == 0x54 else None

    @property
    def frequency_hz(self) -> Optional[int]:
        """Frequency location observed in the two 0x54 channel-setting layouts."""
        channel_layouts = (b"\x03\x00\x0a\x00", b"\x04\x00\x08\x00")
        if (
            len(self.payload) < 20
            or self.payload[:1] != b"\x54"
            or self.payload[8:12] not in channel_layouts
        ):
            return None
        return int.from_bytes(self.payload[12:16], "little")


def encode_frame(payload: bytes, integrity: bytes) -> bytes:
    """Return a DLE-escaped wire frame using supplied, captured integrity bytes.

    Captures place the two integrity bytes *after* DLE/ETX.
    """
    if len(integrity) != 2:
        raise ValueError("integrity must contain exactly two bytes")
    escaped = payload.replace(bytes([DLE]), bytes([DLE, DLE]))
    return bytes([DLE, STX]) + escaped + bytes([DLE, ETX]) + integrity


def encode_network_frame(payload: bytes, integrity: bytes) -> bytes:
    """Encode a Kinetic TCP frame, including DLE stuffing of the CRC bytes.

    USB captures place the two CRC bytes after DLE/ETX without observable
    stuffing.  Kinetic's documented Ethernet protocol additionally stuffs DLE
    bytes in the CRC field, so it needs its own encoder.
    """
    if len(integrity) != 2:
        raise ValueError("integrity must contain exactly two bytes")
    escape = lambda value: value.replace(bytes([DLE]), bytes([DLE, DLE]))
    return bytes([DLE, STX]) + escape(payload) + bytes([DLE, ETX]) + escape(integrity)


def decode_frame(wire: bytes) -> Frame:
    """Decode exactly one DLE-framed wire message."""
    if len(wire) < 6 or not wire.startswith(bytes([DLE, STX])) or wire[-4:-2] != bytes([DLE, ETX]):
        raise ValueError("missing DLE/STX or DLE/ETX delimiters")
    body = wire[2:-4]
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] == DLE:
            if i + 1 >= len(body) or body[i + 1] != DLE:
                raise ValueError("unescaped DLE inside frame")
            out.append(DLE)
            i += 2
        else:
            out.append(body[i])
            i += 1
    if not out:
        raise ValueError("frame too short")
    return Frame(payload=bytes(out), integrity=wire[-2:])


class DleFrameStream:
    """Reassemble complete SBS-3 wire frames across arbitrary USB packets."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames = []
        while True:
            start = self._buffer.find(bytes([DLE, STX]))
            if start < 0:
                # Keep a possible first DLE so a following packet can supply STX.
                self._buffer[:] = self._buffer[-1:] if self._buffer[-1:] == bytes([DLE]) else b""
                break
            if start:
                del self._buffer[:start]
            end = self._buffer.find(bytes([DLE, ETX]), 2)
            if end < 0 or len(self._buffer) < end + 4:
                break
            frames.append(bytes(self._buffer[:end + 4]))
            del self._buffer[:end + 4]
        return frames


class NetworkFrameStream:
    """Reassemble documented Kinetic TCP frames, including stuffed CRC bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buffer.find(bytes([DLE, STX]))
            if start < 0:
                self._buffer[:] = self._buffer[-1:] if self._buffer[-1:] == bytes([DLE]) else b""
                break
            if start:
                del self._buffer[:start]
            end = self._buffer.find(bytes([DLE, ETX]), 2)
            if end < 0:
                break
            cursor = end + 2
            integrity = bytearray()
            incomplete = False
            while len(integrity) < 2:
                if cursor >= len(self._buffer):
                    incomplete = True
                    break
                value = self._buffer[cursor]
                if value == DLE:
                    if cursor + 1 >= len(self._buffer):
                        incomplete = True
                        break
                    if self._buffer[cursor + 1] != DLE:
                        # A TCP CRC must be DLE-stuffed. Discard this malformed
                        # start and continue looking for the next frame.
                        del self._buffer[:cursor + 1]
                        integrity.clear()
                        break
                    integrity.append(DLE)
                    cursor += 2
                else:
                    integrity.append(value)
                    cursor += 1
            if incomplete:
                break
            if len(integrity) != 2:
                continue
            wire = bytes(self._buffer[:end + 2]) + bytes(integrity)
            del self._buffer[:cursor]
            try:
                frames.append(decode_frame(wire))
            except ValueError:
                continue
        return frames


def strip_ftdi_status(raw: bytes, packet_size: int = 64) -> tuple[list[bytes], bytes]:
    """Remove the two FTDI status bytes at the start of each bulk USB packet."""
    statuses = []
    data = bytearray()
    for offset in range(0, len(raw), packet_size):
        packet = raw[offset:offset + packet_size]
        if len(packet) < 2:
            break
        statuses.append(packet[:2])
        data.extend(packet[2:])
    return statuses, bytes(data)


class FtdiBulkStream:
    """Split a continuous FTDI read stream into status bytes and payload bytes."""

    def __init__(self, packet_size: int = 64) -> None:
        self._packet_size = packet_size
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[list[bytes], bytes]:
        self._buffer.extend(data)
        complete = len(self._buffer) // self._packet_size * self._packet_size
        if not complete:
            return [], b""
        raw = bytes(self._buffer[:complete])
        del self._buffer[:complete]
        return strip_ftdi_status(raw, self._packet_size)


def xmodem_integrity(payload: bytes) -> bytes:
    """Return the confirmed SBS-3 integrity field (CRC-16/XMODEM, big-endian)."""
    return crc16(payload, 0x1021, 0x0000, False).to_bytes(2, "big")


def retune(frame: Frame, frequency_hz: int, sequence: Optional[int] = None) -> Frame:
    """Create a validated, captured-template frame with a new frequency.

    This only transforms data in memory. It never writes to USB.
    """
    if not 0 <= frequency_hz <= 0xFFFFFFFF:
        raise ValueError("frequency must fit in an unsigned 32-bit field")
    if frame.frequency_hz is None:
        raise ValueError("frame is not the observed frequency-bearing 0x54 message family")
    payload = bytearray(frame.payload)
    if sequence is not None:
        if not 0 <= sequence <= 0xFF:
            raise ValueError("sequence must be a byte")
        payload[1] = sequence
    payload[12:16] = frequency_hz.to_bytes(4, "little")
    payload = bytes(payload)
    return Frame(payload, xmodem_integrity(payload))
