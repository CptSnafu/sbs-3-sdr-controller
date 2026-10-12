"""Minimal reader for USBPcap packets inside PCAPNG captures."""

from dataclasses import dataclass
import struct
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class UsbPacket:
    timestamp_us: int
    endpoint: int
    info: int
    function: int
    transfer: int
    payload: bytes


def usb_packets(path: Union[str, Path]) -> list[UsbPacket]:
    raw = Path(path).read_bytes()
    offset = 0
    packets: list[UsbPacket] = []
    while offset + 12 <= len(raw):
        block_type, block_size = struct.unpack_from("<II", raw, offset)
        if block_size < 12 or offset + block_size > len(raw):
            raise ValueError(f"invalid PCAPNG block at offset {offset:#x}")
        if block_type == 6:
            timestamp_high, timestamp_low, captured_length = struct.unpack_from("<III", raw, offset + 12)
            packet = raw[offset + 28 : offset + 28 + captured_length]
            if len(packet) >= 27:
                header_length, _irp, _status, function, info, _bus, _device, endpoint, transfer, _data_length = struct.unpack_from("<HQIHBHHBBI", packet)
                packets.append(UsbPacket((timestamp_high << 32) | timestamp_low, endpoint, info, function, transfer, packet[header_length:]))
        offset += block_size
    return packets
