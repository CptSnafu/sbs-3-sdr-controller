"""Command-line entry points for safe SBS-3 capture analysis."""

import argparse
import struct
import time
from .pcapng import usb_packets
from .protocol import DleFrameStream, FtdiBulkStream, decode_frame, encode_frame, retune, strip_ftdi_status, xmodem_integrity
from .integrity import CANDIDATES, matching_candidates
from .ftdi import inspect_device, list_devices, read_pending, write_and_observe
from .commands import tuner_configuration
from .replies import summarize_reply
from .acars import decode_acars_record


def _hex(value: str) -> bytes:
    return bytes.fromhex(value)


def decode_capture(path: str) -> int:
    packets = usb_packets(path)
    if not packets:
        print("No USBPcap packets found.")
        return 1
    start = packets[0].timestamp_us
    for packet in packets:
        if packet.endpoint != 0x04 or not packet.payload.startswith(b"\x10\x02"):
            continue
        try:
            frame = decode_frame(packet.payload)
        except ValueError as error:
            print(f"+{(packet.timestamp_us-start)/1e6:.6f}s invalid frame: {error}")
            continue
        frequency = frame.frequency_hz
        details = f" sequence={frame.sequence}" if frame.sequence is not None else ""
        if frequency is not None:
            details += f" frequency={frequency} Hz ({frequency / 1_000_000:.6f} MHz)"
        print(f"+{(packet.timestamp_us-start)/1e6:.6f}s{details} integrity={frame.integrity.hex(' ')}")
    return 0


def check_integrity(path: str) -> int:
    frames = [
        decode_frame(packet.payload)
        for packet in usb_packets(path)
        if packet.endpoint == 0x04 and packet.payload.startswith(b"\x10\x02")
    ]
    if not frames:
        print("No framed endpoint-0x04 messages found.")
        return 1
    matches = matching_candidates(frames)
    print(f"Checked {len(frames)} frame(s).")
    if matches:
        print("Verified candidate(s):", ", ".join(matches))
        return 0
    print("No common CRC-16 candidate matches every frame.")
    first = frames[0]
    print("First frame expected:", first.integrity.hex(" "))
    for candidate in CANDIDATES:
        value = candidate.calculate(first.payload)
        print(f"{candidate.name}: LE {value.to_bytes(2, 'little').hex(' ')} / BE {value.to_bytes(2, 'big').hex(' ')}")
    return 2


def retune_from_capture(path: str, frequency_hz: int, index: int) -> int:
    frames = [
        decode_frame(packet.payload)
        for packet in usb_packets(path)
        if packet.endpoint == 0x04 and packet.payload.startswith(b"\x10\x02")
    ]
    frequency_frames = [frame for frame in frames if frame.frequency_hz is not None]
    if not 0 <= index < len(frequency_frames):
        print(f"No frequency frame at index {index}; capture contains {len(frequency_frames)}.")
        return 1
    source = frequency_frames[index]
    result = retune(source, frequency_hz)
    print("Source frequency:", source.frequency_hz, "Hz")
    print("New frequency:", result.frequency_hz, "Hz")
    print("Wire frame (not sent):", encode_frame(result.payload, result.integrity).hex(" "))
    return 0


def probe_ftdi() -> int:
    try:
        devices = list_devices()
    except RuntimeError as error:
        print(error)
        return 1
    if not devices:
        print("No FTDI D2XX devices found.")
        return 1
    for device in devices:
        marker = "  <-- SBS-3 candidate" if device.is_sbs3 else ""
        print(
            f"[{device.index}] {device.vendor_id:04X}:{device.product_id:04X} "
            f"serial={device.serial!r} description={device.description!r}{marker}"
        )
    return 0


def inspect_ftdi(index: int) -> int:
    try:
        snapshot = inspect_device(index)
    except RuntimeError as error:
        print(error)
        return 1
    print(f"FTDI entry {snapshot.index}")
    print(f"Driver version: 0x{snapshot.driver_version:08X}")
    print(f"Receive queue: {snapshot.receive_queue_bytes} byte(s)")
    print(f"Modem status: 0x{snapshot.modem_status:08X}")
    print("No data was written and the device handle has been closed.")
    return 0


def build_tuner_frame(tuner: str, frequency_hz: int, enabled: bool, sequence: int, device_index: int, send: bool, base_station_closed: bool, observe_ms: int, prepare_ftdi: bool) -> int:
    frame = tuner_configuration(tuner, frequency_hz, enabled, sequence)
    wire = encode_frame(frame.payload, frame.integrity)
    print(f"Tuner {tuner}: {'on' if enabled else 'off'} at {frequency_hz} Hz")
    print("Wire frame:", wire.hex(" "))
    if not send:
        print("Dry run only: no data was sent.")
        return 0
    if not base_station_closed:
        print("Refusing to send: pass --i-closed-basestation only after BaseStation is fully closed.")
        return 2
    try:
        count, replies = write_and_observe(device_index, wire, observe_ms / 1_000, prepare_ftdi)
    except (RuntimeError, ValueError) as error:
        print(error)
        return 1
    print(f"Sent {count} byte(s) to FTDI entry {device_index}.")
    if prepare_ftdi:
        print("Applied observed FTDI latency/data/flow-control/purge defaults before sending.")
    print(f"Received {len(replies)} byte(s) during the {observe_ms} ms observation window.")
    if replies:
        print("Raw reply:", replies.hex(" "))
        live_stream = DleFrameStream()
        decoded = 0
        for reply_wire in live_stream.feed(replies):
            try:
                reply = decode_frame(reply_wire)
            except ValueError as error:
                print("Unparsed reply:", error)
                continue
            summary = summarize_reply(reply)
            print(
                f"Decoded reply {decoded}: {summary.description}; "
                f"CRC-XMODEM={'OK' if summary.crc_valid else 'FAIL'}; "
                f"payload={reply.payload.hex(' ')}"
            )
            decoded += 1
    return 0


FTDI_REQUESTS = {
    0: "reset/purge",
    1: "modem control",
    2: "flow control",
    3: "baud rate",
    4: "data characteristics",
    5: "poll modem status",
    6: "event character",
    7: "error character",
    9: "latency timer",
    10: "get latency timer",
    11: "bit mode",
}


def show_ftdi_control(path: str) -> int:
    packets = usb_packets(path)
    if not packets:
        print("No USBPcap packets found.")
        return 1
    start = packets[0].timestamp_us
    count = 0
    for packet in packets:
        if packet.endpoint != 0 or packet.info != 0 or len(packet.payload) < 8:
            continue
        request_type, request, value, index, length = struct.unpack_from("<BBHHH", packet.payload)
        if request_type not in (0x40, 0xC0):
            continue
        name = FTDI_REQUESTS.get(request, "vendor-specific")
        print(
            f"+{(packet.timestamp_us-start)/1e6:.6f}s "
            f"{'IN' if request_type == 0xC0 else 'OUT'} request=0x{request:02X} ({name}), "
            f"value=0x{value:04X}, index={index}, length={length}"
        )
        count += 1
    print(f"{count} FTDI vendor control request(s) found.")
    return 0


def show_inbound_frames(path: str) -> int:
    """Display DLE-framed device replies after the two FTDI status bytes."""
    packets = usb_packets(path)
    if not packets:
        print("No USBPcap packets found.")
        return 1
    start = packets[0].timestamp_us
    streams = {0x81: DleFrameStream(), 0x83: DleFrameStream()}
    count = 0
    for packet in packets:
        if packet.endpoint not in (0x81, 0x83) or packet.info != 1 or len(packet.payload) <= 2:
            continue
        statuses, body = strip_ftdi_status(packet.payload)
        status_text = "/".join(status.hex(" ") for status in statuses)
        for wire in streams[packet.endpoint].feed(body):
            try:
                frame = decode_frame(wire)
            except ValueError as error:
                print(
                    f"+{(packet.timestamp_us-start)/1e6:.6f}s endpoint=0x{packet.endpoint:02X} "
                    f"unparsed frame: {error}; wire={wire.hex(' ')}"
                )
                continue
            summary = summarize_reply(frame)
            print(
                f"+{(packet.timestamp_us-start)/1e6:.6f}s endpoint=0x{packet.endpoint:02X} "
                f"FTDI-status={status_text} payload={frame.payload.hex(' ')} "
                f"integrity={frame.integrity.hex(' ')} type={summary.kind} "
                f"CRC-XMODEM={'OK' if summary.crc_valid else 'FAIL'}"
            )
            count += 1
    print(f"{count} complete framed device reply/replies found.")
    return 0


def show_acars_frames(path: str) -> int:
    """List conservatively decoded ACARS records from a USBPcap capture."""
    packets = usb_packets(path)
    if not packets:
        print("No USBPcap packets found.")
        return 1
    start = packets[0].timestamp_us
    streams = {0x81: DleFrameStream(), 0x83: DleFrameStream()}
    count = 0
    for packet in packets:
        if packet.endpoint not in streams or packet.info != 1 or len(packet.payload) <= 2:
            continue
        _, body = strip_ftdi_status(packet.payload)
        for wire in streams[packet.endpoint].feed(body):
            try:
                frame = decode_frame(wire)
            except ValueError:
                continue
            if frame.payload[:1] != b"\x58":
                continue
            try:
                record = decode_acars_record(frame)
            except ValueError as error:
                print(f"+{(packet.timestamp_us-start)/1e6:.6f}s unparsed ACARS record: {error}")
                continue
            crc = "OK" if record.crc_valid else "FAIL"
            print(
                f"+{(packet.timestamp_us-start)/1e6:.6f}s endpoint=0x{packet.endpoint:02X} "
                f"sdr={record.receiver_index + 1} mode={record.mode} registration={record.registration or '-'} "
                f"sequence={record.sequence} flight={record.flight_number or '-'} "
                f"CRC-XMODEM={crc} text={record.message_text!r}"
            )
            count += 1
    print(f"{count} decoded ACARS record(s) found.")
    return 0


def show_outbound_frames(path: str) -> int:
    """Display every complete framed command written to FTDI bulk OUT endpoints."""
    packets = usb_packets(path)
    if not packets:
        print("No USBPcap packets found.")
        return 1
    start = packets[0].timestamp_us
    streams = {0x02: DleFrameStream(), 0x04: DleFrameStream()}
    count = 0
    for packet in packets:
        if packet.endpoint not in streams or packet.info != 0 or not packet.payload:
            continue
        for wire in streams[packet.endpoint].feed(packet.payload):
            try:
                frame = decode_frame(wire)
            except ValueError as error:
                print(
                    f"+{(packet.timestamp_us-start)/1e6:.6f}s endpoint=0x{packet.endpoint:02X} "
                    f"unparsed frame: {error}; wire={wire.hex(' ')}"
                )
                continue
            frequency = frame.frequency_hz
            details = ""
            if frequency is not None:
                details = f" frequency={frequency} Hz ({frequency / 1_000_000:.6f} MHz)"
            crc = "OK" if xmodem_integrity(frame.payload) == frame.integrity else "FAIL"
            print(
                f"+{(packet.timestamp_us-start)/1e6:.6f}s endpoint=0x{packet.endpoint:02X} "
                f"payload={frame.payload.hex(' ')} integrity={frame.integrity.hex(' ')} "
                f"CRC-XMODEM={crc}{details}"
            )
            count += 1
    print(f"{count} complete framed host command(s) found.")
    return 0


def monitor_ftdi(index: int, seconds: float, base_station_closed: bool) -> int:
    """Read and decode a short FTDI sample without sending anything."""
    if not base_station_closed:
        print("Refusing to open FTDI: pass --i-closed-basestation only after BaseStation is fully closed.")
        return 2
    if seconds <= 0 or seconds > 10:
        print("Seconds must be greater than 0 and no more than 10.")
        return 2
    deadline = time.monotonic() + seconds
    received = 0
    frames = 0
    bulk_stream = FtdiBulkStream()
    frame_stream = DleFrameStream()
    while time.monotonic() < deadline:
        try:
            raw = read_pending(index)
        except RuntimeError as error:
            print(error)
            return 1
        received += len(raw)
        statuses, payload = bulk_stream.feed(raw)
        for wire in frame_stream.feed(payload):
            try:
                frame = decode_frame(wire)
            except ValueError as error:
                print("Unparsed SBS-3 frame:", error)
                continue
            summary = summarize_reply(frame)
            status_text = "/".join(status.hex(" ") for status in statuses) or "(previous USB packet)"
            print(
                f"FTDI-status={status_text} payload={frame.payload.hex(' ')} "
                f"type={summary.kind} CRC-XMODEM={'OK' if summary.crc_valid else 'FAIL'}"
            )
            frames += 1
        time.sleep(0.05)
    print(f"Read {received} byte(s), decoded {frames} complete frame(s); no bytes were sent.")
    return 0


def launch_windows_gui() -> int:
    try:
        from .windows_gui import launch
        launch()
    except RuntimeError as error:
        print(error)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline SBS-3 capture and frame tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("decode-capture", help="decode frames sent to endpoint 0x04 in a USBPcap capture")
    capture.add_argument("capture")
    integrity = sub.add_parser("check-integrity", help="test common CRC-16 variants against captured frames")
    integrity.add_argument("capture")
    retune_parser = sub.add_parser("retune-from-capture", help="generate, but never send, a retuned captured template")
    retune_parser.add_argument("capture")
    retune_parser.add_argument("frequency_hz", type=int)
    retune_parser.add_argument("--frame-index", type=int, default=0)
    sub.add_parser("probe-ftdi", help="Windows-only, read-only FTDI D2XX device listing")
    inspect_parser = sub.add_parser("inspect-ftdi", help="Windows-only, open/read/close one FTDI entry; never writes")
    inspect_parser.add_argument("index", type=int)
    build = sub.add_parser("build-tuner-frame", help="generate a confirmed tuner frame; never sends it")
    build.add_argument("tuner", choices=("A", "B"))
    build.add_argument("frequency_hz", type=int)
    build.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    build.add_argument("--sequence", type=lambda value: int(value, 0), required=True)
    build.add_argument("--device-index", type=int, default=0)
    build.add_argument("--send", action="store_true", help="actually write to USB; omitted by default")
    build.add_argument("--i-closed-basestation", action="store_true", help="required together with --send")
    build.add_argument("--observe-ms", type=int, default=500, choices=range(100, 2001), metavar="100-2000")
    build.add_argument("--prepare-ftdi", action="store_true", help="apply observed non-baud FTDI defaults before sending")
    control = sub.add_parser("show-ftdi-control", help="list FTDI vendor control transfers from a USBPcap capture")
    control.add_argument("capture")
    inbound = sub.add_parser("show-inbound-frames", help="list framed replies received through FTDI bulk IN endpoints")
    inbound.add_argument("capture")
    acars = sub.add_parser("show-acars-frames", help="list conservatively decoded ACARS records from a USBPcap capture")
    acars.add_argument("capture")
    outbound = sub.add_parser("show-outbound-frames", help="list framed commands sent through FTDI bulk OUT endpoints")
    outbound.add_argument("capture")
    monitor = sub.add_parser("monitor-ftdi", help="Windows-only, short read-only sample of an FTDI entry")
    monitor.add_argument("index", type=int)
    monitor.add_argument("--seconds", type=float, default=2.0)
    monitor.add_argument("--i-closed-basestation", action="store_true", help="required before opening FTDI")
    sub.add_parser("windows-gui", help="launch the Windows SBS-3 tuner-control surface")
    encode = sub.add_parser("encode", help="create a frame using supplied integrity bytes; never sends it")
    encode.add_argument("--hex", required=True, dest="payload")
    encode.add_argument("--checksum", required=True)
    args = parser.parse_args()
    if args.command == "decode-capture":
        return decode_capture(args.capture)
    if args.command == "check-integrity":
        return check_integrity(args.capture)
    if args.command == "retune-from-capture":
        return retune_from_capture(args.capture, args.frequency_hz, args.frame_index)
    if args.command == "probe-ftdi":
        return probe_ftdi()
    if args.command == "inspect-ftdi":
        return inspect_ftdi(args.index)
    if args.command == "build-tuner-frame":
        return build_tuner_frame(
            args.tuner, args.frequency_hz, args.enabled, args.sequence,
            args.device_index, args.send, args.i_closed_basestation, args.observe_ms, args.prepare_ftdi,
        )
    if args.command == "show-ftdi-control":
        return show_ftdi_control(args.capture)
    if args.command == "show-inbound-frames":
        return show_inbound_frames(args.capture)
    if args.command == "show-acars-frames":
        return show_acars_frames(args.capture)
    if args.command == "show-outbound-frames":
        return show_outbound_frames(args.capture)
    if args.command == "monitor-ftdi":
        return monitor_ftdi(args.index, args.seconds, args.i_closed_basestation)
    if args.command == "windows-gui":
        return launch_windows_gui()
    print(encode_frame(_hex(args.payload), _hex(args.checksum)).hex(" "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
