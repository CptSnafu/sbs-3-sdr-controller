"""Conservative decoder for the observed SBS-3 ACARS record frames.

This format is based on three CRC-valid ``0x58`` device-to-host frames that
match rows displayed by BaseStation's ACARS Messages window.  Only the fields
whose positions are independently corroborated are named here.  The remaining
header bytes are intentionally retained as raw data rather than guessed at.
"""

from dataclasses import dataclass

from .protocol import Frame, xmodem_integrity


ACARS_RECORD_TYPE = 0x58
_MINIMUM_RECORD_LENGTH = 32


def decode_marked_ascii(value: bytes) -> str:
    """Decode the observed 7-bit text representation.

    In captured records printable text can appear either as normal ASCII or
    with bit 7 set.  Masking bit 7 reproduces the text shown by BaseStation.
    """
    return bytes(item & 0x7F for item in value).decode("ascii", errors="replace")


@dataclass(frozen=True)
class AcarsRecord:
    """Fields verified against BaseStation's ACARS message display."""

    receiver_index: int
    mode: str
    registration: str
    sequence: str
    flight_number: str
    message_text: str
    header_bytes: bytes
    crc_valid: bool


def decode_acars_record(frame: Frame) -> AcarsRecord:
    """Decode one observed SBS-3 ``0x58`` ACARS record frame.

    Raises ``ValueError`` for a non-record or a frame too short to contain the
    corroborated fixed-position fields.  The trailing three payload bytes have
    not yet been assigned a meaning and are deliberately excluded from text.
    """
    payload = frame.payload
    if not payload or payload[0] != ACARS_RECORD_TYPE:
        raise ValueError("frame is not an observed SBS-3 ACARS record")
    if len(payload) < _MINIMUM_RECORD_LENGTH:
        raise ValueError("ACARS record is too short")

    # Byte 2 equals the record payload length minus five in every observed
    # message.  Reject a mismatch rather than silently slicing a different
    # future 0x58 layout as if it were this format.
    if payload[2] + 5 != len(payload):
        raise ValueError("ACARS record length does not match observed layout")

    registration = decode_marked_ascii(payload[8:14]).lstrip(".").rstrip()
    return AcarsRecord(
        receiver_index=payload[1],
        mode=decode_marked_ascii(payload[6:7]),
        registration=registration,
        sequence=decode_marked_ascii(payload[19:23]),
        flight_number=decode_marked_ascii(payload[23:29]).rstrip(),
        message_text=decode_marked_ascii(payload[29:-3]).rstrip(),
        header_bytes=payload[15:18],
        crc_valid=frame.integrity == xmodem_integrity(payload),
    )
