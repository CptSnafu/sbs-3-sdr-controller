"""Read-only FTDI D2XX device discovery for Windows.

This module deliberately provides discovery only. It neither opens an FTDI
channel nor writes to the SBS-3.
"""

from dataclasses import dataclass
import ctypes
import os
import time
from typing import List


FT_OK = 0
SBS3_VENDOR_ID = 0x0403
SBS3_PRODUCT_ID = 0x6010


def decode_device_id(device_id: int) -> tuple[int, int]:
    """Decode the D2XX combined ID as vendor ID followed by product ID."""
    return (device_id >> 16) & 0xFFFF, device_id & 0xFFFF


@dataclass(frozen=True)
class FtdiDevice:
    index: int
    vendor_id: int
    product_id: int
    location_id: int
    serial: str
    description: str

    @property
    def is_sbs3(self) -> bool:
        return (self.vendor_id, self.product_id) == (SBS3_VENDOR_ID, SBS3_PRODUCT_ID)


@dataclass(frozen=True)
class FtdiSnapshot:
    index: int
    driver_version: int
    receive_queue_bytes: int
    modem_status: int


def list_devices() -> List[FtdiDevice]:
    """List D2XX devices without opening a device handle.

    Run this on the Windows computer with the SBS-3 and FTDI D2XX installed.
    """
    if os.name != "nt":
        raise RuntimeError("FTDI probing is available only on Windows; no USB access was attempted.")
    try:
        library = ctypes.WinDLL("ftd2xx.dll")
    except OSError as error:
        raise RuntimeError("ftd2xx.dll was not found. Install the official FTDI D2XX driver first.") from error

    count = ctypes.c_ulong()
    status = library.FT_CreateDeviceInfoList(ctypes.byref(count))
    if status != FT_OK:
        raise RuntimeError(f"FT_CreateDeviceInfoList failed with FT_STATUS {status}.")

    devices = []
    for index in range(count.value):
        flags = ctypes.c_ulong()
        device_type = ctypes.c_ulong()
        device_id = ctypes.c_ulong()
        location_id = ctypes.c_ulong()
        serial = ctypes.create_string_buffer(17)
        description = ctypes.create_string_buffer(65)
        # The last parameter receives a handle only for already-open devices;
        # passing a null pointer keeps this operation read-only.
        status = library.FT_GetDeviceInfoDetail(
            index,
            ctypes.byref(flags),
            ctypes.byref(device_type),
            ctypes.byref(device_id),
            ctypes.byref(location_id),
            serial,
            description,
            None,
        )
        if status != FT_OK:
            raise RuntimeError(f"FT_GetDeviceInfoDetail({index}) failed with FT_STATUS {status}.")
        vendor_id, product_id = decode_device_id(device_id.value)
        devices.append(
            FtdiDevice(
                index=index,
                vendor_id=vendor_id,
                product_id=product_id,
                location_id=location_id.value,
                serial=serial.value.decode("ascii", errors="replace"),
                description=description.value.decode("ascii", errors="replace"),
            )
        )
    return devices


def find_sbs3_control_device(devices: List[FtdiDevice]) -> FtdiDevice:
    """Return the Kinetic SBS-3's FTDI channel B without relying on an index.

    The SBS-3 exposes two FT2232 channels. Captures established channel B as
    the control channel. Requiring both the Kinetic description and its B
    suffix prevents an unrelated FTDI device with the same generic chip ID
    from being selected accidentally.
    """
    named = [
        device for device in devices
        if device.is_sbs3 and "kinetic sbs-3" in device.description.lower()
    ]
    control = [
        device for device in named
        if device.description.rstrip().upper().endswith(" B")
        or device.serial.rstrip().upper().endswith(" B")
    ]
    if len(control) == 1:
        return control[0]
    if not named:
        raise RuntimeError(
            "No Kinetic SBS-3 was found. Connect it and verify that the FTDI D2XX driver is installed."
        )
    if len(control) > 1:
        raise RuntimeError("More than one SBS-3 control channel was found. Disconnect additional SBS-3 devices.")
    raise RuntimeError("The SBS-3 control channel B was not found. Reconnect the device and retry.")


def inspect_device(index: int) -> FtdiSnapshot:
    """Open one D2XX entry only long enough to read driver state, then close it.

    No writes, baud-rate changes, resets, or FTDI mode changes are performed.
    BaseStation must be closed before invoking this function.
    """
    if os.name != "nt":
        raise RuntimeError("FTDI inspection is available only on Windows; no USB access was attempted.")
    try:
        library = ctypes.WinDLL("ftd2xx.dll")
    except OSError as error:
        raise RuntimeError("ftd2xx.dll was not found. Install the official FTDI D2XX driver first.") from error

    handle = ctypes.c_void_p()
    status = library.FT_Open(ctypes.c_int(index), ctypes.byref(handle))
    if status != FT_OK:
        raise RuntimeError(f"FT_Open({index}) failed with FT_STATUS {status}. Close BaseStation and retry.")
    try:
        driver_version = ctypes.c_ulong()
        queue_bytes = ctypes.c_ulong()
        modem_status = ctypes.c_ulong()
        for function, output in (
            (library.FT_GetDriverVersion, driver_version),
            (library.FT_GetQueueStatus, queue_bytes),
            (library.FT_GetModemStatus, modem_status),
        ):
            status = function(handle, ctypes.byref(output))
            if status != FT_OK:
                raise RuntimeError(f"{function.__name__} failed with FT_STATUS {status}.")
        return FtdiSnapshot(index, driver_version.value, queue_bytes.value, modem_status.value)
    finally:
        library.FT_Close(handle)


def write_frame(index: int, frame: bytes) -> int:
    """Write one already-validated frame to one FTDI D2XX entry.

    Callers must make the user-facing safety decision: BaseStation needs to be
    closed and the selected device index must be confirmed first.
    """
    if os.name != "nt":
        raise RuntimeError("FTDI writing is available only on Windows; no USB access was attempted.")
    if not frame:
        raise ValueError("refusing to send an empty frame")
    try:
        library = ctypes.WinDLL("ftd2xx.dll")
    except OSError as error:
        raise RuntimeError("ftd2xx.dll was not found. Install the official FTDI D2XX driver first.") from error
    handle = ctypes.c_void_p()
    status = library.FT_Open(ctypes.c_int(index), ctypes.byref(handle))
    if status != FT_OK:
        raise RuntimeError(f"FT_Open({index}) failed with FT_STATUS {status}. Close BaseStation and retry.")
    try:
        sent = ctypes.c_ulong()
        buffer = ctypes.create_string_buffer(frame)
        status = library.FT_Write(handle, buffer, len(frame), ctypes.byref(sent))
        if status != FT_OK:
            raise RuntimeError(f"FT_Write failed with FT_STATUS {status}.")
        if sent.value != len(frame):
            raise RuntimeError(f"FT_Write completed partially ({sent.value}/{len(frame)} byte(s)).")
        return sent.value
    finally:
        library.FT_Close(handle)


def _prepare_observed_ftdi_defaults(library: object, handle: ctypes.c_void_p) -> None:
    """Apply the non-baud FTDI defaults observed before SBS-3 frames.

    This is intentionally opt-in. It does not recreate an undocumented
    baud-rate setting from the capture.
    """
    operations = (
        ("FT_SetLatencyTimer", (handle, ctypes.c_ubyte(16))),
        ("FT_SetDataCharacteristics", (handle, ctypes.c_ubyte(8), ctypes.c_ubyte(0), ctypes.c_ubyte(0))),
        ("FT_SetFlowControl", (handle, ctypes.c_ushort(0), ctypes.c_ubyte(0), ctypes.c_ubyte(0))),
        ("FT_Purge", (handle, ctypes.c_ulong(0x0003))),
    )
    for name, arguments in operations:
        status = getattr(library, name)(*arguments)
        if status != FT_OK:
            raise RuntimeError(f"{name} failed with FT_STATUS {status}.")


class FtdiSession:
    """One persistent D2XX handle for serialized writes and continuous reads."""

    def __init__(self, index: int, prepare_ftdi: bool = False) -> None:
        if os.name != "nt":
            raise RuntimeError("FTDI access is available only on Windows.")
        self.library = ctypes.WinDLL("ftd2xx.dll")
        self.handle = ctypes.c_void_p()
        status = self.library.FT_Open(ctypes.c_int(index), ctypes.byref(self.handle))
        if status != FT_OK:
            raise RuntimeError(f"FT_Open({index}) failed with FT_STATUS {status}.")
        if prepare_ftdi:
            _prepare_observed_ftdi_defaults(self.library, self.handle)

    def write(self, frame: bytes) -> int:
        sent = ctypes.c_ulong()
        status = self.library.FT_Write(self.handle, ctypes.create_string_buffer(frame), len(frame), ctypes.byref(sent))
        if status != FT_OK or sent.value != len(frame):
            raise RuntimeError(f"FT_Write failed with FT_STATUS {status} ({sent.value}/{len(frame)} byte(s)).")
        return sent.value

    def read(self) -> bytes:
        available = ctypes.c_ulong()
        status = self.library.FT_GetQueueStatus(self.handle, ctypes.byref(available))
        if status != FT_OK:
            raise RuntimeError(f"FT_GetQueueStatus failed with FT_STATUS {status}.")
        if not available.value:
            return b""
        buffer = ctypes.create_string_buffer(available.value)
        received = ctypes.c_ulong()
        status = self.library.FT_Read(self.handle, buffer, available.value, ctypes.byref(received))
        if status != FT_OK:
            raise RuntimeError(f"FT_Read failed with FT_STATUS {status}.")
        return buffer.raw[:received.value]

    def close(self) -> None:
        if self.handle:
            self.library.FT_Close(self.handle)
            self.handle = ctypes.c_void_p()


def write_and_observe(index: int, frame: bytes, observe_seconds: float = 0.5, prepare_ftdi: bool = False) -> tuple[int, bytes]:
    """Write one frame and collect immediate reply bytes before closing.

    No reset, purge, mode setting, or other configuration operation is used.
    """
    if os.name != "nt":
        raise RuntimeError("FTDI writing is available only on Windows; no USB access was attempted.")
    if not frame:
        raise ValueError("refusing to send an empty frame")
    try:
        library = ctypes.WinDLL("ftd2xx.dll")
    except OSError as error:
        raise RuntimeError("ftd2xx.dll was not found. Install the official FTDI D2XX driver first.") from error
    handle = ctypes.c_void_p()
    status = library.FT_Open(ctypes.c_int(index), ctypes.byref(handle))
    if status != FT_OK:
        raise RuntimeError(f"FT_Open({index}) failed with FT_STATUS {status}. Close BaseStation and retry.")
    try:
        if prepare_ftdi:
            _prepare_observed_ftdi_defaults(library, handle)
        sent = ctypes.c_ulong()
        buffer = ctypes.create_string_buffer(frame)
        status = library.FT_Write(handle, buffer, len(frame), ctypes.byref(sent))
        if status != FT_OK:
            raise RuntimeError(f"FT_Write failed with FT_STATUS {status}.")
        if sent.value != len(frame):
            raise RuntimeError(f"FT_Write completed partially ({sent.value}/{len(frame)} byte(s)).")
        replies = bytearray()
        deadline = time.monotonic() + observe_seconds
        while time.monotonic() < deadline:
            available = ctypes.c_ulong()
            status = library.FT_GetQueueStatus(handle, ctypes.byref(available))
            if status != FT_OK:
                raise RuntimeError(f"FT_GetQueueStatus failed with FT_STATUS {status}.")
            if available.value:
                read_buffer = ctypes.create_string_buffer(available.value)
                received = ctypes.c_ulong()
                status = library.FT_Read(handle, read_buffer, available.value, ctypes.byref(received))
                if status != FT_OK:
                    raise RuntimeError(f"FT_Read failed with FT_STATUS {status}.")
                replies.extend(read_buffer.raw[:received.value])
            time.sleep(0.02)
        return sent.value, bytes(replies)
    finally:
        library.FT_Close(handle)


def read_pending(index: int) -> bytes:
    """Read bytes already queued by one FTDI D2XX entry, without writing.

    The function performs no reset, purge, mode change, or timeout change.
    BaseStation must be closed before use.
    """
    if os.name != "nt":
        raise RuntimeError("FTDI reading is available only on Windows; no USB access was attempted.")
    try:
        library = ctypes.WinDLL("ftd2xx.dll")
    except OSError as error:
        raise RuntimeError("ftd2xx.dll was not found. Install the official FTDI D2XX driver first.") from error
    handle = ctypes.c_void_p()
    status = library.FT_Open(ctypes.c_int(index), ctypes.byref(handle))
    if status != FT_OK:
        raise RuntimeError(f"FT_Open({index}) failed with FT_STATUS {status}. Close BaseStation and retry.")
    try:
        available = ctypes.c_ulong()
        status = library.FT_GetQueueStatus(handle, ctypes.byref(available))
        if status != FT_OK:
            raise RuntimeError(f"FT_GetQueueStatus failed with FT_STATUS {status}.")
        if not available.value:
            return b""
        buffer = ctypes.create_string_buffer(available.value)
        received = ctypes.c_ulong()
        status = library.FT_Read(handle, buffer, available.value, ctypes.byref(received))
        if status != FT_OK:
            raise RuntimeError(f"FT_Read failed with FT_STATUS {status}.")
        return buffer.raw[:received.value]
    finally:
        library.FT_Close(handle)
