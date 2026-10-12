"""Serialized SBS-3 commands with continuous device-status reading.

One thread owns the active USB or TCP connection.  This avoids a second reader
racing a control command and consuming its reply bytes.
"""

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable, List, Optional, Tuple

from .ftdi import FtdiSession
from .network import DeviceEndpoint, NetworkSession
from .protocol import DleFrameStream, NetworkFrameStream, decode_frame
from .replies import summarize_reply
from .status import StatusSample, decode_status
from .acars import AcarsRecord, decode_acars_record
from .protocol import xmodem_integrity


@dataclass(frozen=True)
class PlannedFrame:
    """A complete command and the sequence number its acknowledgement uses."""

    label: str
    wire: bytes
    sequence: int


@dataclass(frozen=True)
class DeviceRequest:
    """An operation built only after it reaches the front of the queue."""

    label: str
    build: Callable[[int], List[PlannedFrame]]
    completed: Callable[[List[str], int, bool], None]
    network_login: bool = False


class SBS3DeviceWorker:
    """Keep one SBS-3 session open and serialize command/reply exchanges."""

    def __init__(
        self,
        initial_sequence: int,
        status_received: Callable[[StatusSample], None],
        acars_received: Optional[Callable[[AcarsRecord], None]] = None,
        audio_received: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self._sequence = initial_sequence & 0xFF
        self._status_received = status_received
        self._acars_received = acars_received
        self._audio_received = audio_received
        self._requests: queue.Queue[Optional[Tuple[DeviceEndpoint, bool, DeviceRequest]]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def next_sequence(self) -> int:
        return self._sequence

    def submit(self, endpoint: DeviceEndpoint, prepare_ftdi: bool, request: DeviceRequest) -> None:
        self._requests.put((endpoint, prepare_ftdi, request))

    def close(self) -> None:
        self._requests.put(None)

    def _deliver(self, stream: object, raw: bytes, expected_sequence: Optional[int] = None) -> Tuple[bool, List[str]]:
        """Decode data, forward periodic status, and look for one response."""
        matched, lines = False, []
        for item in stream.feed(raw):
            try:
                frame = item if hasattr(item, "payload") else decode_frame(item)
            except ValueError as error:
                lines.append("Ignored malformed device frame: {0}".format(error))
                continue
            status = decode_status(frame)
            if status is not None:
                self._status_received(status)
            summary = summarize_reply(frame)
            if frame.payload[:1] == b"\x3b" and len(frame.payload) == 129:
                if summary.crc_valid and self._audio_received is not None:
                    self._audio_received(frame.payload[1:])
                # Audio arrives hundreds of times per second. It belongs to the
                # player, not the Device Log.
                continue
            if summary.kind == "acars-record" and self._acars_received is not None:
                try:
                    record = decode_acars_record(frame)
                except ValueError as error:
                    lines.append("Ignored malformed ACARS record: {0}".format(error))
                else:
                    if record.crc_valid:
                        self._acars_received(record)
            lines.append("{0}; CRC {1}".format(summary.description, "OK" if summary.crc_valid else "FAIL"))
            if expected_sequence is not None:
                matched |= frame.payload[:3] == bytes((0x20, expected_sequence, 0x00)) and summary.crc_valid
        return matched, lines

    def _run(self) -> None:
        session = None
        session_endpoint = None
        stream = DleFrameStream()
        try:
            while True:
                try:
                    queued = self._requests.get(timeout=0.04)
                except queue.Empty:
                    if session is not None:
                        try:
                            raw = session.read()
                            if raw:
                                self._deliver(stream, raw)
                        except Exception:
                            # A dropped idle TCP connection must not stop the
                            # worker. The next command opens a fresh session.
                            session.close()
                            session = None
                            session_endpoint = None
                    continue
                if queued is None:
                    return
                endpoint, prepare_ftdi, request = queued
                try:
                    if session is None or endpoint != session_endpoint:
                        if session is not None:
                            session.close()
                        if endpoint.transport == "usb":
                            if endpoint.device_index is None:
                                raise RuntimeError("USB endpoint is missing an FTDI device index.")
                            session = FtdiSession(endpoint.device_index, prepare_ftdi=prepare_ftdi)
                            stream = DleFrameStream()
                        elif endpoint.transport == "network":
                            if endpoint.host is None or endpoint.port is None:
                                raise RuntimeError("Network endpoint is missing its address or port.")
                            session = NetworkSession(endpoint.host, endpoint.port)
                            stream = NetworkFrameStream()
                        else:
                            raise RuntimeError(f"Unsupported SBS-3 transport: {endpoint.transport}")
                        session_endpoint = endpoint

                    start_sequence = self._sequence
                    if request.network_login and endpoint.transport == "network":
                        session.write_payload(bytes((0x17, 0x00)), xmodem_integrity(bytes((0x17, 0x00))))
                        time.sleep(0.7)
                    frames = request.build(start_sequence)
                    lines, successful, confirmed = [], True, 0
                    for planned in frames:
                        count = session.write(planned.wire)
                        lines.append("{0}: sent {1} byte(s).".format(planned.label, count))
                        # Network mode can interleave capability/status frames
                        # before the matching acknowledgement. The SBS-3 has
                        # been observed to apply a command before that reply
                        # arrives, so allow the same 3.5-second window used by
                        # the macOS controller.
                        deadline = time.monotonic() + 3.5
                        confirmed_this_frame = False
                        while time.monotonic() < deadline:
                            raw = session.read()
                            if raw:
                                matched, received_lines = self._deliver(stream, raw, planned.sequence)
                                lines.extend(received_lines)
                                if matched:
                                    confirmed_this_frame = True
                                    confirmed += 1
                                    break
                            time.sleep(0.015)
                        if not confirmed_this_frame:
                            lines.append("{0} was not confirmed within 3.5 seconds.".format(planned.label))
                            successful = False
                            break
                    self._sequence = (start_sequence + confirmed) & 0xFF
                    request.completed(lines, self._sequence, successful)
                except Exception as error:
                    if session is not None:
                        session.close()
                    session = None
                    session_endpoint = None
                    request.completed(["SBS-3 worker error: {0}".format(error)], self._sequence, False)
        finally:
            if session is not None:
                session.close()
