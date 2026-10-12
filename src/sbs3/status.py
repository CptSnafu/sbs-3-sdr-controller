"""Conservative decoding of SBS-3 periodic status frames."""

from dataclasses import dataclass
from typing import Optional, Tuple
from .protocol import Frame


@dataclass(frozen=True)
class StatusSample:
    """The six receiver activity bytes from a periodic device status frame.

    Each receiver occupies a two-byte slot. The low byte is the displayed
    activity level; the high byte carries other observed status bits.
    """

    receiver_levels: Tuple[int, int, int, int, int, int]

    @property
    def sdr4_level(self) -> int:
        """Compatibility alias for the independently capture-verified slot."""
        return self.receiver_levels[3]


def decode_status(frame: Frame) -> Optional[StatusSample]:
    """Decode the six receiver activity slots from a periodic status frame."""
    payload = frame.payload
    if len(payload) < 19 or payload[0] != 0x38:
        return None
    return StatusSample(tuple(payload[offset] for offset in range(7, 19, 2)))
