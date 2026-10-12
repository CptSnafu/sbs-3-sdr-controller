"""Kinetic SBS-3 TCP transport for the documented Ethernet API."""

from dataclasses import dataclass
import socket
from typing import Optional

from .protocol import decode_frame, encode_network_frame


DEFAULT_SBS3_HOST = "192.168.1.170"
DEFAULT_SBS3_PORT = 10001


@dataclass(frozen=True)
class DeviceEndpoint:
    """A USB control channel or a documented SBS-3 TCP endpoint."""

    transport: str
    device_index: Optional[int] = None
    host: Optional[str] = None
    port: Optional[int] = None

    @classmethod
    def usb(cls, device_index: int) -> "DeviceEndpoint":
        return cls("usb", device_index=device_index)

    @classmethod
    def network(cls, host: str, port: int) -> "DeviceEndpoint":
        host = host.strip()
        if not host:
            raise ValueError("Enter an SBS-3 network address.")
        if not 1 <= port <= 65535:
            raise ValueError("The SBS-3 TCP port must be between 1 and 65535.")
        return cls("network", host=host, port=port)

    @property
    def label(self) -> str:
        if self.transport == "usb":
            return f"USB FTDI entry {self.device_index}"
        return f"Network {self.host}:{self.port}"


class NetworkSession:
    """One non-blocking TCP connection to an SBS-3 main API port."""

    def __init__(self, host: str, port: int, timeout: float = 3.0) -> None:
        try:
            self._socket = socket.create_connection((host, port), timeout=timeout)
        except OSError as error:
            raise RuntimeError(f"Could not connect to SBS-3 at {host}:{port}: {error}") from error
        self._socket.setblocking(False)

    def write(self, usb_wire: bytes) -> int:
        """Convert a captured USB-form frame to documented TCP framing."""
        frame = decode_frame(usb_wire)
        return self.write_payload(frame.payload, frame.integrity)

    def write_payload(self, payload: bytes, integrity: bytes) -> int:
        """Write a protocol frame already expressed as payload plus CRC."""
        wire = encode_network_frame(payload, integrity)
        try:
            self._socket.setblocking(True)
            self._socket.settimeout(3.0)
            self._socket.sendall(wire)
        except OSError as error:
            raise RuntimeError(f"SBS-3 network write failed: {error}") from error
        finally:
            self._socket.setblocking(False)
        return len(wire)

    def read(self) -> bytes:
        received = bytearray()
        while True:
            try:
                chunk = self._socket.recv(65536)
            except BlockingIOError:
                break
            except OSError as error:
                raise RuntimeError(f"SBS-3 network read failed: {error}") from error
            if not chunk:
                raise RuntimeError("SBS-3 network connection was closed.")
            received.extend(chunk)
            if len(chunk) < 65536:
                break
        return bytes(received)

    def close(self) -> None:
        self._socket.close()
