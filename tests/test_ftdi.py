import unittest

from sbs3.ftdi import FtdiDevice, decode_device_id, find_sbs3_control_device


class FtdiTests(unittest.TestCase):
    def test_d2xx_device_id_order(self):
        self.assertEqual(decode_device_id(0x0403_6010), (0x0403, 0x6010))

    def test_finds_kinetic_sbs3_channel_b_independent_of_ftdi_index(self):
        devices = [
            FtdiDevice(7, 0x0403, 0x6010, 0, "031903  A", "Kinetic SBS-3 Beavis 3A A"),
            FtdiDevice(12, 0x0403, 0x6010, 0, "031903  B", "Kinetic SBS-3 Beavis 3A B"),
        ]
        self.assertEqual(find_sbs3_control_device(devices).index, 12)

    def test_does_not_select_an_unrelated_ftdi_6010_device(self):
        device = FtdiDevice(1, 0x0403, 0x6010, 0, "OTHER B", "Unrelated FT2232 B")
        with self.assertRaisesRegex(RuntimeError, "No Kinetic SBS-3"):
            find_sbs3_control_device([device])
