import unittest
import threading

from sbs3.protocol import DleFrameStream, FtdiBulkStream, NetworkFrameStream, decode_frame, encode_frame, encode_network_frame, retune, strip_ftdi_status, xmodem_integrity
from sbs3.integrity import crc16
from sbs3.commands import digital_audio_mixer, mixer_configuration, sdr5_modulation_configuration, sdr5_receiver_configuration, sdr5_squelch_update, sdr_receiver_configuration, sdr_squelch_update, tuner_configuration
from sbs3.replies import summarize_reply
from sbs3.acars import decode_acars_record
from sbs3.command_queue import CommandQueue, QueuedCommand
from sbs3.device_worker import SBS3DeviceWorker
from sbs3.network import DeviceEndpoint
from sbs3.status import decode_status


class ProtocolTests(unittest.TestCase):
    def test_decodes_confirmed_sdr4_status_level(self):
        frame = decode_frame(bytes.fromhex("10 02 38 00 00 06 02 02 05 38 c0 2d c0 3f 80 68 00 38 c0 3e c0 09 36 03 1c 38 1e 36 04 00 52 00 10 03 35 5c"))
        self.assertEqual(decode_status(frame).receiver_levels, (56, 45, 63, 104, 56, 62))
        self.assertEqual(decode_status(frame).sdr4_level, 104)
    def test_command_queue_assigns_sequences_in_order(self):
        done, calls, event = [], [], threading.Event()
        def completed(label, sequence, ok):
            done.append((label, sequence, ok))
            if len(done) == 2:
                event.set()
        queue = CommandQueue(0xFE, completed)
        queue.submit(QueuedCommand("first", lambda sequence: calls.append(sequence) is None))
        queue.submit(QueuedCommand("second", lambda sequence: calls.append(sequence) is None))
        self.assertTrue(event.wait(1))
        self.assertEqual(calls, [0xFE, 0xFF])
        self.assertEqual(done, [("first", 0xFE, True), ("second", 0xFF, True)])
    def test_round_trip_observed_125mhz_frame(self):
        wire = bytes.fromhex(
            "10 02 54 47 01 00 10 10 00 00 00 03 00 0A 00 40 59 73 07 "
            "00 12 7A 00 FF 01 10 03 2E 03"
        )
        frame = decode_frame(wire)
        self.assertEqual(frame.sequence, 0x47)
        self.assertEqual(frame.frequency_hz, 125_000_000)
        self.assertEqual(frame.integrity, bytes.fromhex("2e 03"))
        self.assertEqual(encode_frame(frame.payload, frame.integrity), wire)
        self.assertEqual(crc16(frame.payload, 0x1021, 0x0000, False), 0x2E03)
        changed = retune(frame, 125_001_000, sequence=0x49)
        self.assertEqual(changed.frequency_hz, 125_001_000)
        self.assertEqual(changed.integrity, bytes.fromhex("a0 b0"))

    def test_known_tuner_a_frame(self):
        frame = tuner_configuration("A", 125_000_000, True, 0x47)
        self.assertEqual(
            encode_frame(frame.payload, frame.integrity).hex(" "),
            "10 02 54 47 01 00 10 10 00 00 00 03 00 0a 00 40 59 73 07 "
            "00 12 7a 00 ff 01 10 03 2e 03",
        )

    def test_known_tuner_b_frame(self):
        frame = tuner_configuration("B", 123_800_000, True, 0x17)
        self.assertEqual(frame.integrity, bytes.fromhex("49 9d"))

    def test_observed_sdr5_am_fm_switch(self):
        am = sdr5_modulation_configuration(True, 0x1C)
        fm = sdr5_modulation_configuration(False, 0x1B)
        self.assertEqual(am.payload.hex(" "), "54 1c 01 00 00 00 04 00 04 00 08 00 38 fa 06 08 00 01 47 00")
        self.assertEqual(fm.payload.hex(" "), "54 1b 01 00 00 00 04 00 04 00 08 00 38 fa 06 08 00 00 47 00")
        self.assertEqual(am.integrity, bytes.fromhex("0a f2"))
        self.assertEqual(fm.integrity, bytes.fromhex("07 dd"))
        changed = sdr5_receiver_configuration(129_050_000, True, 0x20)
        self.assertEqual(changed.frequency_hz, 129_050_000)

    def test_observed_sdr5_source_mute_and_squelch(self):
        source_b = sdr5_receiver_configuration(134_675_000, True, 0x1F, source="B", squelch=0x4F)
        muted = sdr5_receiver_configuration(134_675_000, True, 0x21, source="A", squelch=0x4F, muted=True)
        update = sdr5_squelch_update(0x4C, 0x24)
        self.assertEqual(source_b.integrity, bytes.fromhex("87 ef"))
        self.assertEqual(muted.integrity, bytes.fromhex("44 0a"))
        self.assertEqual(encode_frame(update.payload, update.integrity).hex(" "), "10 02 54 24 01 00 06 00 04 00 04 00 01 00 4c 10 03 5e c4")

    def test_individually_captured_sdr_indices(self):
        profiles = ((0, 74), (1, 76), (2, 68), (3, 79), (5, 82))
        for receiver, squelch in profiles:
            frame = sdr_receiver_configuration(receiver, 134_675_000, True, 0x56, source="A", squelch=squelch)
            update = sdr_squelch_update(receiver, squelch, 0x57)
            self.assertEqual(frame.payload[6], receiver)
            self.assertEqual(frame.frequency_hz, 134_675_000)
            self.assertEqual(update.payload[6], receiver)

    def test_observed_sdr3_mixer_slot(self):
        frame = mixer_configuration([100, 100, 99, 100, 100, 100], 0xB9)
        self.assertEqual(frame.payload[48:52], bytes.fromhex("63 00 63 00"))
        self.assertEqual(frame.integrity, bytes.fromhex("ba 53"))

    def test_digital_audio_mixer_routes_one_receiver(self):
        frame = digital_audio_mixer(2, True, 0xD5)
        self.assertEqual(frame.payload[:16], bytes.fromhex("54 d5 01 00 00 00 01 00 05 00 40 00 06 00 83 03"))
        self.assertEqual(frame.payload[48:56], bytes.fromhex("02 00 00 00 64 00 64 00"))
        self.assertEqual(digital_audio_mixer(None, False, 0xD6).payload[14], 0x03)

    def test_reassembles_a_frame_split_across_usb_packets(self):
        wire = bytes.fromhex(
            "10 02 54 47 01 00 10 10 00 00 00 03 00 0A 00 40 59 73 07 "
            "00 12 7A 00 FF 01 10 03 2E 03"
        )
        stream = DleFrameStream()
        self.assertEqual(stream.feed(wire[:9]), [])
        self.assertEqual(stream.feed(wire[9:-1]), [])
        self.assertEqual(stream.feed(wire[-1:]), [wire])

    def test_network_framing_stuffs_crc_and_reassembles_frame(self):
        payload = bytes.fromhex("54 10 00")
        integrity = bytes.fromhex("10 aa")
        wire = encode_network_frame(payload, integrity)
        self.assertEqual(wire.hex(" "), "10 02 54 10 10 00 10 03 10 10 aa")
        stream = NetworkFrameStream()
        self.assertEqual(stream.feed(wire[:7]), [])
        frames = stream.feed(wire[7:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, payload)
        self.assertEqual(frames[0].integrity, integrity)

    def test_network_endpoint_validates_address_and_port(self):
        endpoint = DeviceEndpoint.network("192.168.1.170", 10001)
        self.assertEqual(endpoint.label, "Network 192.168.1.170:10001")
        with self.assertRaises(ValueError):
            DeviceEndpoint.network("", 10001)
        with self.assertRaises(ValueError):
            DeviceEndpoint.network("192.168.1.170", 0)

    def test_strips_ftdi_status_from_each_64_byte_usb_packet(self):
        first = bytes.fromhex("01 60") + b"A" * 62
        second = bytes.fromhex("01 62") + b"B" * 10
        statuses, data = strip_ftdi_status(first + second)
        self.assertEqual(statuses, [bytes.fromhex("01 60"), bytes.fromhex("01 62")])
        self.assertEqual(data, b"A" * 62 + b"B" * 10)

    def test_reassembles_ftdi_bulk_packets_across_reads(self):
        packet = bytes.fromhex("01 60") + b"A" * 62
        stream = FtdiBulkStream()
        self.assertEqual(stream.feed(packet[:17]), ([], b""))
        statuses, data = stream.feed(packet[17:])
        self.assertEqual(statuses, [bytes.fromhex("01 60")])
        self.assertEqual(data, b"A" * 62)

    def test_decodes_observed_device_confirmation(self):
        wire = bytes.fromhex("10 02 20 47 00 10 03 12 9d")
        frame = decode_frame(wire)
        self.assertEqual(frame.payload, bytes.fromhex("20 47 00"))
        self.assertEqual(frame.integrity, bytes.fromhex("12 9d"))
        self.assertEqual(frame.integrity, xmodem_integrity(frame.payload))
        summary = summarize_reply(frame)
        self.assertEqual(summary.kind, "command-response")
        self.assertTrue(summary.crc_valid)
        self.assertIn("sequence=0x47", summary.description)

    def test_decodes_acars_record_confirmed_by_basestation_window(self):
        wire = bytes.fromhex(
            "10 02 58 01 4f 00 00 01 32 ae ae 4f 45 c7 da 46 15 51 51 b6 02 "
            "cd b0 34 c1 da c4 b0 b0 b0 31 4c 4f 57 57 4c d9 c2 45 31 b0 b0 "
            "b5 0d 8a b0 b0 31 46 45 b0 b9 31 b0 b0 b5 31 b6 ce 34 38 b0 37 "
            "ae b0 45 b0 31 b6 b3 32 ae 31 b0 32 34 31 34 b3 20 20 20 20 83 19 75 "
            "10 03 98 97"
        )
        frame = decode_frame(wire)
        record = decode_acars_record(frame)
        self.assertTrue(record.crc_valid)
        self.assertEqual(record.receiver_index, 1)
        self.assertEqual(record.mode, "2")
        self.assertEqual(record.registration, "OEGZF")
        self.assertEqual(record.sequence, "M04A")
        self.assertEqual(record.flight_number, "ZD0001")
        self.assertTrue(record.message_text.startswith("LOWWLYBE1005"))
        self.assertEqual(summarize_reply(frame).kind, "acars-record")

    def test_worker_forwards_only_crc_valid_acars_records(self):
        wire = bytes.fromhex(
            "10 02 58 01 4f 00 00 01 32 ae ae 4f 45 c7 da 46 15 51 51 b6 02 "
            "cd b0 34 c1 da c4 b0 b0 b0 31 4c 4f 57 57 4c d9 c2 45 31 b0 b0 "
            "b5 0d 8a b0 b0 31 46 45 b0 b9 31 b0 b0 b5 31 b6 ce 34 38 b0 37 "
            "ae b0 45 b0 31 b6 b3 32 ae 31 b0 32 34 31 34 b3 20 20 20 20 83 19 75 "
            "10 03 98 97"
        )
        statuses, records = [], []
        worker = object.__new__(SBS3DeviceWorker)
        worker._status_received = statuses.append
        worker._acars_received = records.append
        matched, lines = worker._deliver(DleFrameStream(), wire)
        self.assertFalse(matched)
        self.assertEqual(statuses, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].registration, "OEGZF")
        self.assertIn("decoded ACARS record frame", lines[0])
