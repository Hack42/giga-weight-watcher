#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause

"""
ultraship_u2v2_serial.py

Code for interfacing with MyWeigh Ultraship U2 USB scales that use a PL2303
USB serial interface.

Licensed under a Simplified BSD License:

Copyright (c) 2012, Timothy Twillman
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright notice,
       this list of conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL
TIMOTHY TWILLMAN OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The views and conclusions contained in the software and documentation are
those of the authors and should not be interpreted as representing official
policies, either expressed or implied, of Timothy Twillman.
"""

import argparse
import re
import struct
import sys


PACKET_SIZE = 14
CONTINUOUS_PAYLOAD_SIZE = 0x0B
WEIGHT_RE = re.compile(r'[+-]?\s*\d+(?:\.\d+)?')
UNIT_CODES = {
    'G': 'g',
    'K': 'kg',
    'L': 'lb',
    'O': 'oz',
}


class UltrashipU2v2:

    """Class for interfacing with USB-Serial version of UltraShip U2 scales.

    This handles packets sent on demand with "SEND" as well as packets emitted
    automatically when the scale is configured for continuous mode.

    Note: It is not able to request data from the scale, or to get any more
    information than what is displayed on the top line of the display, and
    it's unknown whether those features might be possible with this scale.

    Version 1 of the scale used a USB HID interface; this class does not
    support that version.
    """

    def __init__(self, port):
        """Initialize the scale object."""
        port.baudrate = 9600
        self._port = port
        self._buf = bytearray()

    def fill_buffer(self):
        c = self._port.read(PACKET_SIZE - len(self._buf))
        self._buf.extend(c)

    @staticmethod
    def parse_continuous_measurement(pkt):
        """Parse a value and unit from a plain continuous-mode packet.

        Observed packet layout::

            02 0b 44 20 20 20 30 2e 34 34 36 4b 4b 03
            STX 11          D   0.446KK             ETX

        Byte 1 specifies the 11-byte ASCII payload size. The first payload byte
        is status information, the next eight bytes contain the value, and the
        final two bytes describe the two display units. The transmitted value
        uses the first display unit.
        """
        if len(pkt) < PACKET_SIZE:
            return None
        packet = bytes(pkt[:PACKET_SIZE])
        if not (
                packet[0] == 0x02
                and packet[1] == CONTINUOUS_PAYLOAD_SIZE
                and packet[13] == 0x03):
            return None

        try:
            payload = packet[2:13].decode('ascii')
        except UnicodeDecodeError:
            return None

        match = WEIGHT_RE.search(payload)
        if match is None:
            return None

        value = match.group(0).replace(' ', '')
        unit = UNIT_CODES.get(payload[-2].upper())
        if unit is None:
            unit = UNIT_CODES.get(payload[-1].upper())
        return value, unit

    @classmethod
    def parse_continuous_packet(cls, pkt):
        """Parse the value from a plain continuous-mode packet."""
        measurement = cls.parse_continuous_measurement(pkt)
        if measurement is None:
            return None
        return measurement[0]

    @staticmethod
    def parse_legacy_packet(pkt):
        """Parse the older encrypted/checksummed U-2 v2 packet format.

        Args:
            pkt: A bytes-like object containing one 14-byte scale packet.

        Returns:
            A string containing the packet's decoded contents, or None if the
            passed in array is not a valid packet.


        A properly formed packet is 14 bytes long.
         0: STX (0x02)
         1: XOR key for following bytes (needs to also be XORed with 0x26)
         2..10: Data Bytes
        11: Checksum High Byte
        12: Checksum Low Byte
        13: ETX (0x03)

        Checksum is calculated by simply adding together all of the bytes
        from index 1..10, before decoding with the key.
        """
        if len(pkt) >= PACKET_SIZE:
            # The H is for the (big endian, 2-byte) checksum.
            data = struct.unpack('>BBBBBBBBBBBHB', pkt[0:PACKET_SIZE])

            if data[0] == 0x02 and data[12] == 0x03 and sum(data[1:11]) == data[11]:
                # Decode data bytes
                key = data[1] ^ 0x26
                # data[2] is a newline; toss it out.
                data = [(data[i] ^ key) for i in range(3, 11)]

                try:
                    return struct.pack('BBBBBBBB', *data).decode('ascii')
                except UnicodeDecodeError:
                    return None

        return None

    @classmethod
    def parse_packet(cls, pkt):
        """Parse either continuous ASCII or legacy encrypted scale data."""
        result = cls.parse_continuous_packet(pkt)
        if result is not None:
            return result
        return cls.parse_legacy_packet(pkt)

    @classmethod
    def parse_measurement(cls, pkt):
        """Parse a packet into ``(value, unit)``.

        Legacy packets do not carry a usable unit and therefore return
        ``None`` as their unit.
        """
        result = cls.parse_continuous_measurement(pkt)
        if result is not None:
            return result
        value = cls.parse_legacy_packet(pkt)
        if value is None:
            return None
        return value, None

    def read_measurement(self):
        """Read a decoded ``(value, unit)`` measurement from the scale.

        This will keep reading bytes until it gets a valid packet, then will
        decode the packet and return its numeric text and detected unit.
        """
        while True:
            self.fill_buffer()

            # Dump characters until finding the packet's STX byte.
            start = self._buf.find(b'\x02')
            if start < 0:
                self._buf.clear()
                continue
            del self._buf[:start]

            while len(self._buf) >= PACKET_SIZE:
                # Try to parse the packet... hopefully it's valid.
                result = self.parse_measurement(self._buf)
                if result is not None:
                    del self._buf[:PACKET_SIZE]
                    return result
                else:
                    # Bad packet.  Dump everything up to next STX and
                    # continue looking for a valid packet.
                    next_start = self._buf.find(b'\x02', 1)
                    if next_start < 0:
                        self._buf.clear()
                        break
                    del self._buf[:next_start]

    def read(self):
        """Read only the display value, preserving the original API."""
        return self.read_measurement()[0]


def main():
    """Basic main function for getting data from a scale and printing it out.

    Pass the USB serial device name on the command line, or omit it to use
    /dev/ultraship-u2. The program outputs the number shown on the scale's
    display.
    """
    import serial

    parser = argparse.ArgumentParser(
        description='Read a My Weigh UltraShip U-2 v2 over USB serial.'
    )
    parser.add_argument(
        'device', nargs='?', default='/dev/ultraship-u2',
        help='serial device (default: /dev/ultraship-u2)',
    )
    parser.add_argument(
        '--changes-only', action='store_true',
        help='only print when the measured value changes',
    )
    args = parser.parse_args()

    try:
        with serial.Serial(args.device, 9600) as port:
            scale = UltrashipU2v2(port)
            previous = None
            print('Listening on {} at 9600 8N1...'.format(args.device),
                  file=sys.stderr)
            while True:
                reading = scale.read()
                if not args.changes_only or reading != previous:
                    print(reading, flush=True)
                previous = reading
    except serial.SerialException as error:
        print('Cannot open {}: {}'.format(args.device, error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
