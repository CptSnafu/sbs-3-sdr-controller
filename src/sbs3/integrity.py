"""Offline checks for candidate SBS-3 frame integrity algorithms."""

from dataclasses import dataclass
from typing import Callable, Iterable


def crc16(data: bytes, polynomial: int, initial: int, reflected: bool, final_xor: int = 0) -> int:
    value = initial
    if reflected:
        for byte in data:
            value ^= byte
            for _ in range(8):
                value = (value >> 1) ^ (polynomial if value & 1 else 0)
        return (value ^ final_xor) & 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (polynomial if value & 0x8000 else 0)) & 0xFFFF
    return (value ^ final_xor) & 0xFFFF


@dataclass(frozen=True)
class Candidate:
    name: str
    calculate: Callable[[bytes], int]


CANDIDATES = (
    Candidate("CRC-16/ARC", lambda data: crc16(data, 0xA001, 0x0000, True)),
    Candidate("CRC-16/MODBUS", lambda data: crc16(data, 0xA001, 0xFFFF, True)),
    Candidate("CRC-16/X25", lambda data: crc16(data, 0x8408, 0xFFFF, True, 0xFFFF)),
    Candidate("CRC-16/KERMIT", lambda data: crc16(data, 0x8408, 0x0000, True)),
    Candidate("CRC-16/CCITT-FALSE", lambda data: crc16(data, 0x1021, 0xFFFF, False)),
    Candidate("CRC-16/XMODEM", lambda data: crc16(data, 0x1021, 0x0000, False)),
    Candidate("CRC-16/GENIBUS", lambda data: crc16(data, 0x1021, 0xFFFF, False, 0xFFFF)),
)


def matching_candidates(frames: Iterable[object]) -> list[str]:
    """Return matching algorithm names and on-wire byte order."""
    pairs = list(frames)
    matches = []
    for candidate in CANDIDATES:
        value = lambda frame: candidate.calculate(frame.payload)
        if all(value(frame) == int.from_bytes(frame.integrity, "little") for frame in pairs):
            matches.append(f"{candidate.name} (little-endian)")
        if all(value(frame) == int.from_bytes(frame.integrity, "big") for frame in pairs):
            matches.append(f"{candidate.name} (big-endian)")
    return matches
