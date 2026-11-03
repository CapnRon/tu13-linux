"""Protocol and device layer for the ATORCH TU13 USB/Type-C tester.

Reverse-engineered from the vendor's Windows application (UsbTester.exe 1.0.5)
using Ghidra, and verified against hardware (TU13 V1.1.5, bootloader
C13P V1.0.2).  See docs/PROTOCOL.md for the full write-up.

Transport: USB HID, VID 0483 PID 5750, report ID 0x55 out / 0xAA in,
63-byte payloads (64 bytes total including the report ID).
"""
from __future__ import annotations

import os
import select
import time

VID = 0x0483
PID = 0x5750

# Measurement poll, sent continuously by the vendor UI (~2 Hz):
#   55 05 22 05 0b 00 8c ee ff
# Reply: AA 40 62 05 ... with nine little-endian u32 fields.
POLL_CMD = bytes([0x05, 0x22, 0x05, 0x0B, 0x00, 0x8C, 0xEE, 0xFF])

# Verified fields of the AA 40 62 05 reply: (offset, label, unit, format, divisor)
MEAS_FIELDS = [
    (0x08, "Voltage", "V", ".3f", 1e6),
    (0x0C, "Current", "A", ".3f", 1e6),
    (0x10, "Power", "W", ".3f", 1e6),
    (0x14, "Energy", "Wh", ".4f", 1e6),
    (0x18, "Capacity", "Ah", ".4f", 1e6),
]

# Offsets 0x1C/0x20 carry values that decay by halving (4551 -> 2275 -> 1137)
# and move between slots; 0x24/0x28 are frozen across reboots and even a
# firmware reflash.  None of them are exposed as tiles until identified.

CHART_FIELDS = [0, 1, 2, 3]  # V, A, W, Energy


def crc16(data):
    """CRC-16/MODBUS (init 0xFFFF, reflected poly 0xA001)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def find_device():
    """Return the /dev/hidraw node for a TU13 (app or bootloader), or None."""
    base = "/sys/bus/hid/devices"
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        parts = name.split(":")
        if len(parts) < 3:
            continue
        try:
            vid = int(parts[1].split(".")[0], 16)
            pid = int(parts[2].split(".")[0], 16)
        except ValueError:
            continue
        if (vid, pid) != (VID, PID):
            continue
        hidraw = os.path.join(base, name, "hidraw")
        if os.path.isdir(hidraw):
            for node in os.listdir(hidraw):
                return "/dev/" + node
    return None


def device_info():
    """Return (product, manufacturer, serial) from sysfs, or None."""
    base = "/sys/bus/usb/devices"
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        d = os.path.join(base, name)

        def rd(attr):
            try:
                with open(os.path.join(d, attr)) as f:
                    return f.read().strip()
            except OSError:
                return ""

        if rd("idVendor") == f"{VID:04x}" and rd("idProduct") == f"{PID:04x}":
            return rd("product"), rd("manufacturer"), rd("serial")
    return None


def in_bootloader():
    """True when the device currently enumerates as the C13P IAP bootloader."""
    info = device_info()
    if not info:
        return False
    product, manufacturer, _serial = info
    text = f"{product} {manufacturer}".upper()
    return "IAP" in text or "C13P" in text


def parse_measurement(report):
    """Parse a 64-byte measurement reply into a dict, or None."""
    if not report or len(report) < 0x2C:
        return None
    if report[0] != 0xAA or report[1] != 0x40 or report[2] != 0x62 or report[3] != 0x05:
        return None
    out = {}
    for off, label, _unit, _fmt, div in MEAS_FIELDS:
        out[label] = int.from_bytes(report[off:off + 4], "little") / div
    out["raw"] = bytes(report)
    out["ts"] = time.time()
    return out


def build_start_packet(packet_count):
    """Bootloader start-of-transfer packet (6 bytes behind the report ID)."""
    header = bytes([0xAA, 0x06, packet_count & 0xFF, (packet_count >> 8) & 0xFF])
    crc = crc16(header)
    return header + bytes([crc & 0xFF, crc >> 8])


def validate_handshake_ack(report):
    """Validate the bootloader's AA BB reply (length byte + 16-bit CRC)."""
    if not report or len(report) < 7:
        return False
    if report[0] != 0xAA or report[1] != 0xBB:
        return False
    payload = report[1:]
    length = payload[1]
    if length < 2 or length > len(payload):
        return False
    if length == 2:
        return True  # vendor skips the CRC in this degenerate case
    crc = crc16(payload[:length - 2])
    return crc == int.from_bytes(payload[length - 2:length], "little")


def build_data_packet(index, chunk, remaining):
    """Build one 0xCC data packet (report ID is added by the transport).

    The vendor writes a 64-byte buffer but the HID report carries only 63
    payload bytes, so the CRC high byte is never transmitted.
    """
    length = remaining + 6 if remaining <= 58 else 0x40
    packet = bytes([0xCC, length, index & 0xFF, (index >> 8) & 0xFF]) + chunk
    return packet + bytes([crc16(packet) & 0xFF])


def iter_data_packets(image):
    """Yield (index, packet) for a firmware image, 58 bytes per packet."""
    size = len(image)
    count = (size + 57) // 58
    for index in range(count):
        offset = index * 58
        remaining = size - offset
        chunk = image[offset:offset + 58]
        yield index, build_data_packet(index, chunk, remaining)


class TU13Error(Exception):
    pass


class TU13:
    """HID session for the TU13 (measurements, pages, bootloader flashing)."""

    def __init__(self, path=None):
        self.path = path or find_device()
        self.fd = None

    # -- transport ---------------------------------------------------------
    def open(self):
        if self.fd is not None:
            return
        if not self.path:
            raise TU13Error("device not found")
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise TU13Error(f"open {self.path}: {exc}") from exc

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _write(self, payload):
        if self.fd is None:
            raise TU13Error("device not open")
        buf = bytearray(64)
        buf[0] = 0x55
        buf[1:1 + len(payload)] = payload
        os.write(self.fd, bytes(buf))

    def _read(self, timeout=0.25):
        if self.fd is None:
            raise TU13Error("device not open")
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        try:
            return os.read(self.fd, 64)
        except OSError:
            return None

    # -- commands ----------------------------------------------------------
    def poll(self):
        """Send the measurement poll; return parsed values or None."""
        self._write(POLL_CMD)
        return parse_measurement(self._read(1.0))

    def page(self, page, param=0):
        """Send a display/settings page command.

        Note: pages do not answer this command on the tested firmware; the
        device only replies to the measurement poll.  Kept for experiments.
        """
        self._write(bytes([0x05, 0x09, page, param, 0x00, 0x00, 0xEE, 0xFF]))
        return self._read(0.8)

    def enter_bootloader(self, timeout=15.0):
        """Ask the app firmware to reboot into the IAP bootloader."""
        self._write(bytes([0x05, 0x09, 0x03, 0x00, 0x00, 0x00, 0xEE, 0xFF]))
        self._read(1.0)
        self.close()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if in_bootloader():
                return True
            time.sleep(0.5)
        return in_bootloader()

    # -- bootloader --------------------------------------------------------
    def flash(self, image, progress_cb=None, log_cb=None):
        """Flash a firmware image (device must already be in IAP mode)."""
        image = bytes(image)
        size = len(image)
        count = (size + 57) // 58

        def log(msg):
            if log_cb:
                log_cb(msg)

        log(f"image: {size} bytes, {count} packets")

        self._write(build_start_packet(count))
        ack = self._read(3.0)
        if not validate_handshake_ack(ack):
            raise TU13Error("bootloader handshake failed (device not in IAP mode?)")
        log("handshake ok")

        started = time.time()
        for index, packet in iter_data_packets(image):
            for _attempt in range(5):
                self._write(packet)
                reply = self._read(2.0)
                if not reply:
                    continue
                if reply[0] == 0xAA and reply[1] == 0xDD:
                    if int.from_bytes(reply[3:5], "little") == index:
                        break
                elif reply[0] == 0xAA and reply[1] == 0xFF:
                    break
            else:
                raise TU13Error(f"packet {index} not acknowledged")
            if progress_cb and (index % 100 == 0 or index == count - 1):
                progress_cb(index + 1, count)

        log(f"all {count} packets acknowledged in {time.time() - started:.1f}s")
        log("transfer complete - the device flashes and reboots on its own")
