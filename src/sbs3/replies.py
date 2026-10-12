"""Conservative classification of observed SBS-3 device-to-host frames."""

from dataclasses import dataclass

from .protocol import Frame, xmodem_integrity


@dataclass(frozen=True)
class ReplySummary:
    kind: str
    description: str
    crc_valid: bool


def summarize_reply(frame: Frame) -> ReplySummary:
    """Describe only fields whose role is evidenced by captures."""
    crc_valid = frame.integrity == xmodem_integrity(frame.payload)
    if len(frame.payload) >= 3 and frame.payload[0] == 0x20:
        sequence = frame.payload[1]
        result = frame.payload[2]
        return ReplySummary(
            "command-response",
            f"command response: sequence=0x{sequence:02X}, result=0x{result:02X}",
            crc_valid,
        )
    if frame.payload[:1] == b"\x38":
        return ReplySummary("status", "periodic device status/measurement frame", crc_valid)
    if frame.payload[:1] == b"\x57":
        return ReplySummary("device-information", "device-information frame", crc_valid)
    if frame.payload[:1] == b"\x58":
        return ReplySummary("acars-record", "decoded ACARS record frame", crc_valid)
    return ReplySummary("unknown", f"unclassified reply type 0x{frame.payload[0]:02X}" if frame.payload else "empty reply", crc_valid)
