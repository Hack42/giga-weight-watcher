# UltraShip U-2 protocol notes

The UltraShip U-2 has appeared with more than one USB interface and wire
protocol. This project supports two known 14-byte USB-serial formats.

## Continuous ASCII format

An observed continuous-mode frame:

```text
Offset  Hex                                               ASCII
0       02                                                STX
1       0b                                                payload length (11)
2..12   44 20 20 20 30 2e 34 34 36 4b 4b                D   0.446KK
13      03                                                ETX
```

The reader validates framing and payload length, decodes the ASCII payload,
and extracts the signed decimal number. Unit conversion is configured by the
user because the numeric field itself does not unambiguously describe its unit.

## Legacy encrypted format

The legacy U-2 v2 packet is also 14 bytes:

| Offset | Meaning |
| ---: | --- |
| 0 | STX (`0x02`) |
| 1 | XOR key material |
| 2..10 | Encoded data |
| 11..12 | Big-endian additive checksum |
| 13 | ETX (`0x03`) |

The checksum is the sum of encoded bytes 1 through 10. The data key is byte 1
XOR `0x26`; decoded display bytes are offsets 3 through 10 XOR that key.

The original implementation of this decoder was written by Timothy Twillman
in 2012. See the copyright and BSD 2-Clause terms in `ultraship_u2v2.py`.

## Stream handling

Serial reads do not necessarily align with packets. The reader therefore:

1. buffers incoming bytes;
2. discards data before STX;
3. waits for a complete 14-byte candidate;
4. validates and decodes the candidate; and
5. searches for the next STX after malformed data.

This permits recovery after startup in the middle of a frame, partial reads,
noise, and USB reconnection.
