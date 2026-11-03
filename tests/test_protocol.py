"""Protocol tests using frames captured from real hardware."""
import pytest

from tu13.protocol import (
    build_data_packet,
    build_start_packet,
    crc16,
    iter_data_packets,
    parse_measurement,
    validate_handshake_ack,
)

# Live measurement reply: 5.101675 V, 0.483040 A, 2.464313 W, 0.63838 Wh,
# 0.001137 Ah, captured with a 5 V load attached.
MEASUREMENT_FRAME = bytes.fromhex(
    "aa406205 0b00eb01"
    " 6bd84d00 e05e0700 399a2500 acbd0900"
    " 71040000 e3080000 c7110000 e05a4002"
    " 14c28902 0000900a 0000fc71 4e000100"
    " 00002a01 00000100"
)

# Bootloader reply to an AA 06 7e 18 start packet (count 6270).
HANDSHAKE_ACK = bytes.fromhex("aabb067e 18e56b00" + "00" * 56)


class TestCrc16:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (bytes.fromhex("aa067e18"), 0x57E0),
            (bytes.fromhex("bb067e18"), 0x6BE5),
            (bytes.fromhex("aa060000"), 0xFDC1),
            (bytes.fromhex("bb060000"), 0xC1C4),
        ],
    )
    def test_known_vectors(self, data, expected):
        assert crc16(data) == expected

    def test_empty(self):
        assert crc16(b"") == 0xFFFF


class TestMeasurement:
    def test_parses_captured_frame(self):
        values = parse_measurement(MEASUREMENT_FRAME)
        assert values is not None
        assert values["Voltage"] == pytest.approx(5.101675)
        assert values["Current"] == pytest.approx(0.48304)
        assert values["Power"] == pytest.approx(2.464313)
        assert values["Energy"] == pytest.approx(0.63838)
        assert values["Capacity"] == pytest.approx(0.001137)

    def test_power_matches_voltage_times_current(self):
        values = parse_measurement(MEASUREMENT_FRAME)
        assert values["Power"] == pytest.approx(
            values["Voltage"] * values["Current"], rel=1e-3
        )

    def test_rejects_other_frames(self):
        assert parse_measurement(None) is None
        assert parse_measurement(b"") is None
        assert parse_measurement(b"\xaa\x05\x40" + b"\x00" * 61) is None
        assert parse_measurement(b"\x00" * 64) is None


class TestBootloader:
    def test_start_packet(self):
        assert build_start_packet(6270) == bytes.fromhex("aa067e18e057")
        assert build_start_packet(0) == bytes.fromhex("aa060000c1fd")

    def test_handshake_ack_valid(self):
        assert validate_handshake_ack(HANDSHAKE_ACK)

    def test_handshake_ack_bad_crc(self):
        corrupt = bytearray(HANDSHAKE_ACK)
        corrupt[6] ^= 0xFF
        assert not validate_handshake_ack(bytes(corrupt))

    def test_handshake_ack_wrong_type(self):
        assert not validate_handshake_ack(b"\xaa\xdd\x06\x7e\x18\x00\x00" + b"\x00" * 57)

    def test_full_data_packet(self):
        chunk = bytes(58)
        packet = build_data_packet(0, chunk, remaining=58)
        assert len(packet) == 63
        assert packet[0] == 0xCC
        assert packet[1] == 0x40
        assert packet[2:4] == b"\x00\x00"
        assert packet[-1] == crc16(packet[:-1]) & 0xFF

    def test_last_data_packet_length(self):
        chunk = bytes(22)
        packet = build_data_packet(6269, chunk, remaining=22)
        assert len(packet) == 27
        assert packet[1] == 22 + 6
        assert packet[2:4] == bytes([6269 & 0xFF, 6269 >> 8])
        assert packet[-1] == crc16(packet[:-1]) & 0xFF

    def test_iter_data_packets(self):
        image = bytes(range(256)) * 1421  # 363,776 bytes
        packets = list(iter_data_packets(image))
        assert len(packets) == (len(image) + 57) // 58
        assert [index for index, _ in packets] == list(range(len(packets)))
        for _index, packet in packets:
            assert packet[-1] == crc16(packet[:-1]) & 0xFF

    def test_image_chunking_is_lossless(self):
        image = bytes((i * 7 + 3) & 0xFF for i in range(1000))
        rebuilt = b"".join(packet[4:-1] for _i, packet in iter_data_packets(image))
        assert rebuilt == image
