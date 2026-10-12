"""Small Windows-only PCM player for the SBS-3 digital mixer stream."""

from __future__ import annotations

import ctypes
import os
import threading


class WindowsDigitalAudioPlayer:
    """Play SBS-3 big-endian stereo PCM as duplicated-left stereo on Windows."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._buffers: list[tuple[ctypes.Array[ctypes.c_char], ctypes.Structure]] = []
        self._handle = None
        self._winmm = None

    def start(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Direct digital playback is available on Windows only.")
        with self._lock:
            if self._handle is not None:
                return
            winmm = ctypes.WinDLL("winmm")

            class WAVEFORMATEX(ctypes.Structure):
                _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort), ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint), ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort), ("cbSize", ctypes.c_ushort)]

            self._header_type = type("WAVEHDR", (ctypes.Structure,), {"_fields_": [
                ("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint), ("dwBytesRecorded", ctypes.c_uint),
                ("dwUser", ctypes.c_void_p), ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p),
            ]})
            format_ = WAVEFORMATEX(1, 2, 16_000, 64_000, 4, 16, 0)
            handle = ctypes.c_void_p()
            result = winmm.waveOutOpen(ctypes.byref(handle), 0xFFFFFFFF, ctypes.byref(format_), 0, 0, 0)
            if result != 0:
                raise RuntimeError(f"Windows audio output could not be opened (error {result}).")
            self._handle, self._winmm = handle, winmm

    def enqueue(self, sbs_pcm: bytes) -> None:
        """Convert 16-bit BE stereo frames; SBS-3 signal is in the left channel."""
        with self._lock:
            if self._handle is None:
                return
            for offset in range(0, len(sbs_pcm) - 3, 4):
                left_le = bytes((sbs_pcm[offset + 1], sbs_pcm[offset]))
                self._pending.extend(left_le + left_le)
            self._reap_done()
            if len(self._pending) < 2048 or len(self._buffers) >= 12:
                return
            block = bytes(self._pending[:2048])
            del self._pending[:2048]
            raw = ctypes.create_string_buffer(block)
            header = self._header_type(ctypes.cast(raw, ctypes.c_void_p), len(block), 0, None, 0, 0, None, None)
            if self._winmm.waveOutPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header)) == 0:
                if self._winmm.waveOutWrite(self._handle, ctypes.byref(header), ctypes.sizeof(header)) == 0:
                    self._buffers.append((raw, header))

    def stop(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._winmm.waveOutReset(self._handle)
            for _raw, header in self._buffers:
                self._winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
            self._buffers.clear()
            self._pending.clear()
            self._winmm.waveOutClose(self._handle)
            self._handle, self._winmm = None, None

    def _reap_done(self) -> None:
        if self._handle is None:
            return
        alive = []
        for raw, header in self._buffers:
            if header.dwFlags & 0x00000001:  # WHDR_DONE
                self._winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
            else:
                alive.append((raw, header))
        self._buffers = alive
