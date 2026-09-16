# SPDX-License-Identifier: BSD-2-Clause
import unittest

from ultraship_u2v2 import UltrashipU2v2


def make_packet(reading, key=0x73):
    """Encode a display string solely for testing the existing decoder."""
    encoded = bytes([ord('\n') ^ key]) + bytes(
        value ^ key for value in reading.encode('ascii')
    )
    body = bytes([key ^ 0x26]) + encoded
    return b'\x02' + body + sum(body).to_bytes(2, 'big') + b'\x03'


class FakePort:
    def __init__(self, data):
        self.data = bytearray(data)
        self.baudrate = None

    def read(self, size):
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


class PacketTest(unittest.TestCase):
    def test_valid_legacy_packet(self):
        self.assertEqual(
            UltrashipU2v2.parse_packet(make_packet('+  0.000')),
            '+  0.000',
        )

    def test_bad_legacy_checksum(self):
        packet = bytearray(make_packet('+ 12.345'))
        packet[5] ^= 1
        self.assertIsNone(UltrashipU2v2.parse_packet(packet))

    def test_observed_continuous_packet(self):
        packet = b'\x02\x0bD   0.446KK\x03'
        self.assertEqual(UltrashipU2v2.parse_packet(packet), '0.446')

    def test_negative_continuous_packet(self):
        packet = b'\x02\x0bD  -0.125KK\x03'
        self.assertEqual(UltrashipU2v2.parse_packet(packet), '-0.125')

    def test_rejects_invalid_continuous_packet(self):
        self.assertIsNone(
            UltrashipU2v2.parse_packet(b'\x02\x0bD   0.446KK\x04')
        )

    def test_read_resynchronizes_mid_stream(self):
        packet = b'\x02\x0bD   0.446KK\x03'
        scale = UltrashipU2v2(FakePort(b'0.446KK\x03' + packet))
        self.assertEqual(scale.read(), '0.446')


if __name__ == '__main__':
    unittest.main()
