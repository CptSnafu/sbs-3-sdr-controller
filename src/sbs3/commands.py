"""Confirmed SBS-3 configuration frame builders.

These functions generate bytes only. Sending them to an SBS-3 is intentionally
outside this module.
"""

from .protocol import Frame, xmodem_integrity
from typing import Optional


def tuner_configuration(tuner: str, frequency_hz: int, enabled: bool, sequence: int) -> Frame:
    """Build the observed 0x54 configuration family for tuner A or B.

    This field layout is confirmed by controlled captures for tuner A and B.
    The result is suitable for offline inspection only until session handshake
    behaviour is independently verified.
    """
    if tuner not in {"A", "B"}:
        raise ValueError("tuner must be 'A' or 'B'")
    if not 0 <= frequency_hz <= 0xFFFFFFFF:
        raise ValueError("frequency must fit in an unsigned 32-bit field")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be a byte")
    tuner_id = 0 if tuner == "A" else 1
    payload = bytes([
        0x54, sequence, 0x01, 0x00, 0x10, 0x00, tuner_id, 0x00,
        0x03, 0x00, 0x0A, 0x00,
    ]) + frequency_hz.to_bytes(4, "little") + bytes([
        0x00, 0x12, 0x7A, 0x00, 0xFF if enabled else 0x00, 0x01,
    ])
    return Frame(payload, xmodem_integrity(payload))


def sdr_receiver_configuration(
    receiver: int,
    frequency_hz: int,
    am: bool,
    sequence: int,
    *,
    source: str = "A",
    squelch: int = 0x47,
    muted: bool = False,
) -> Frame:
    """Build an observed SDR receiver command with a chosen frequency.

    The entire payload is an observed BaseStation template from ``test.2``.
    The frequency field is independently observed across all six SDR rows.
    The four bytes after the frequency were isolated in BaseStation captures:
    source A/B (00/01), modulation AM/FM (01/00), squelch (0..255), and
    mute (00/80).
    """
    if not 0 <= receiver <= 5:
        raise ValueError("receiver must be in the range 0..5")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be a byte")
    if not 0 <= frequency_hz <= 0xFFFFFFFF:
        raise ValueError("frequency must fit in an unsigned 32-bit field")
    if source not in {"A", "B"}:
        raise ValueError("source must be 'A' or 'B'")
    if not 0 <= squelch <= 0xFF:
        raise ValueError("squelch must be a byte")
    payload = bytes([
        0x54, sequence, 0x01, 0x00, 0x00, 0x00, receiver, 0x00,
        0x04, 0x00, 0x08, 0x00,
    ]) + frequency_hz.to_bytes(4, "little") + bytes([
        0x00 if source == "A" else 0x01,
        0x01 if am else 0x00,
        squelch,
        0x80 if muted else 0x00,
    ])
    return Frame(payload, xmodem_integrity(payload))


def sdr5_receiver_configuration(
    frequency_hz: int, am: bool, sequence: int, *, source: str = "A", squelch: int = 0x47, muted: bool = False,
) -> Frame:
    """Compatibility shortcut for the fifth SDR receiver (wire index 4)."""
    return sdr_receiver_configuration(4, frequency_hz, am, sequence, source=source, squelch=squelch, muted=muted)


def sdr_squelch_update(receiver: int, squelch: int, sequence: int) -> Frame:
    """Build the paired short SDR squelch update captured from BaseStation."""
    if not 0 <= receiver <= 5:
        raise ValueError("receiver must be in the range 0..5")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be a byte")
    if not 0 <= squelch <= 0xFF:
        raise ValueError("squelch must be a byte")
    payload = bytes([0x54, sequence, 0x01, 0x00, 0x06, 0x00, receiver, 0x00, 0x04, 0x00, 0x01, 0x00, squelch])
    return Frame(payload, xmodem_integrity(payload))


def sdr5_squelch_update(squelch: int, sequence: int) -> Frame:
    """Compatibility shortcut for the fifth SDR receiver (wire index 4)."""
    return sdr_squelch_update(4, squelch, sequence)


def sdr5_modulation_configuration(am: bool, sequence: int) -> Frame:
    """Compatibility shortcut for the original observed 134.675 MHz frame."""
    return sdr5_receiver_configuration(134_675_000, am, sequence)


def mixer_configuration(volumes: list[int], sequence: int, *, output: int = 0) -> Frame:
    """Build the captured six-channel stereo mixer table (0..100 percent)."""
    if len(volumes) != 6 or any(not 0 <= value <= 100 for value in volumes):
        raise ValueError("volumes must contain six values in the range 0..100")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be a byte")
    if output not in {0, 1}:
        raise ValueError("output must be 0 (analogue) or 1 (digital)")
    pair = lambda value: value.to_bytes(2, "little") * 2
    payload = bytes([0x54, sequence, 0x01, 0x00, 0x00, 0x00, output, 0x00, 0x05, 0x00, 0x40, 0x00, 0x06, 0x00, 0x00, 0x03])
    payload += pair(100) + bytes(12) + pair(volumes[0])
    for index, value in enumerate(volumes[1:], start=1):
        payload += index.to_bytes(4, "little") + pair(value)
    return Frame(payload, xmodem_integrity(payload))


def digital_audio_mixer(receiver: Optional[int], enabled: bool, sequence: int) -> Frame:
    """Route one SDR through the SBS-3 digital output without touching analogue audio."""
    if receiver is not None and not 0 <= receiver <= 5:
        raise ValueError("receiver must be in the range 0..5")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be a byte")
    selected = receiver if enabled else None
    payload = bytearray([
        0x54, sequence, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
        0x05, 0x00, 0x40, 0x00, 0x06, 0x00, 0x83 if enabled else 0x03, 0x03,
    ])

    def append_stereo(level: int, muted: bool) -> None:
        flag = 0x80 if muted else 0x00
        payload.extend((level, flag, level, flag))

    append_stereo(100, False)  # Digital master left/right.
    payload.extend(bytes(12))
    for index in range(6):
        payload.extend(index.to_bytes(4, "little"))
        append_stereo(100 if index == selected else 0, index != selected)
    return Frame(bytes(payload), xmodem_integrity(bytes(payload)))
