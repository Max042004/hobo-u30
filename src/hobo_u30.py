#!/usr/bin/env python3
"""Read attached sensors from a HOBO U30 over its USB HID interface.

The program uses only the U30's read/query commands.  It never starts,
stops, launches, clears, or reconfigures the logger.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import select
import struct
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


VID = 0x0F1B
PID = 0x0301
REPORT_SIZE = 64

HT_START = b"\xc0\xfd"
HT_GET = 0x03
HT_GET_REPLY = 0x04
HT_OPEN = 0x05
HT_OPEN_REPLY = 0x06
HT_CLOSE = 0x07
HT_CLOSE_REPLY = 0x08
HT_OSER = 0x01
HT_OSER_REPLY = 0x02
LOCAL_SENSOR = 0x02


class HoboProtocolError(RuntimeError):
    """An invalid or unexpected U30 protocol frame was received."""


class HoboSessionUnavailable(HoboProtocolError):
    """A valid reply refused the session; distinguish it from USB errors."""

    def __init__(self, payload: bytes):
        self.status = payload[0] if payload else None
        self.occupied = len(payload) >= 2 and self.status in (1, 3, 4)
        detail = "existing session" if self.occupied else "session establishment failed"
        super().__init__(f"U30 session unavailable: {payload.hex()} ({detail})")


def crc16_x25(data: bytes) -> int:
    """Return the CRC-16/X-25 used by both U30 protocol layers."""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc ^ 0xFFFF


@dataclass(frozen=True)
class HoboTalkFrame:
    source: int
    destination: int
    command: int
    payload: bytes
    raw: bytes


def encode_hobotalk(source: int, destination: int, command: int, payload: bytes = b"") -> bytes:
    """Encode one outer HOBOtalk frame, without HID report padding."""
    if not all(0 <= value <= 0xFF for value in (source, destination, command)):
        raise ValueError("HOBOtalk addresses and command must be bytes")
    length = len(payload) + 1  # command plus payload
    if length > 0xFFFF:
        raise ValueError("HOBOtalk payload is too large")
    body = bytes((source, destination)) + length.to_bytes(2, "big")
    body += bytes(((sum(body) & 0xFF), command)) + payload
    return HT_START + body + crc16_x25(body).to_bytes(2, "little")


def decode_hobotalk(raw: bytes) -> HoboTalkFrame:
    if len(raw) < 10 or not raw.startswith(HT_START):
        raise HoboProtocolError("not a complete HOBOtalk frame")
    length = int.from_bytes(raw[4:6], "big")
    if len(raw) != length + 9:
        raise HoboProtocolError(f"HOBOtalk length is {length}, frame has {len(raw)} bytes")
    body = raw[2:-2]
    if raw[6] != (sum(raw[2:6]) & 0xFF):
        raise HoboProtocolError("HOBOtalk header checksum mismatch")
    actual_crc = int.from_bytes(raw[-2:], "little")
    if crc16_x25(body) != actual_crc:
        raise HoboProtocolError("HOBOtalk CRC mismatch")
    return HoboTalkFrame(raw[2], raw[3], raw[7], raw[8:-2], raw)


class HoboTalkReassembler:
    """Extract a frame from HID reports, whose tail is padded to 64 bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, report: Iterable[int] | bytes) -> Optional[HoboTalkFrame]:
        data = bytes(report)
        # A few HID stacks include report ID 0 in reads; U30 itself has no ID.
        if len(data) == REPORT_SIZE + 1 and data[0] == 0:
            data = data[1:]
        self._buffer.extend(data)
        while True:
            start = self._buffer.find(HT_START)
            if start < 0:
                self._buffer[:] = self._buffer[-1:] if self._buffer.endswith(b"\xc0") else b""
                return None
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 6:
                return None
            total = int.from_bytes(self._buffer[4:6], "big") + 9
            if total < 10 or total > 4096:
                del self._buffer[0]
                continue
            if len(self._buffer) < total:
                return None
            raw = bytes(self._buffer[:total])
            del self._buffer[:total]
            try:
                return decode_hobotalk(raw)
            except HoboProtocolError:
                # Re-synchronise at the next possible frame start.
                del self._buffer[:1]


def encode_oser(command: int, payload: bytes = b"") -> bytes:
    """Encode an OSerial command nested in HOBOtalk command 1."""
    length = len(payload) + 1
    if not 0 <= command <= 0xFF or length > 0xFF:
        raise ValueError("invalid OSerial command or payload length")
    body = bytes((0, length, command)) + payload
    body += crc16_x25(body).to_bytes(2, "little")
    stuffed = bytearray()
    for value in body:
        if value in (0xC0, 0xFD, 0x7D):
            stuffed.extend((0x7D, value ^ 0x20))
        else:
            stuffed.append(value)
    return b"\xc0" + stuffed + b"\xfd"


@dataclass(frozen=True)
class OSerialFrame:
    command: int
    payload: bytes


def decode_oser(raw: bytes) -> OSerialFrame:
    if len(raw) < 7 or raw[0] != 0xC0 or raw[-1] != 0xFD:
        raise HoboProtocolError("not a complete OSerial frame")
    unstuffed = bytearray((0xC0,))
    escaped = False
    for value in raw[1:-1]:
        if escaped:
            unstuffed.append(value ^ 0x20)
            escaped = False
        elif value == 0x7D:
            escaped = True
        elif value in (0xC0, 0xFD):
            raise HoboProtocolError("unescaped delimiter in OSerial frame")
        else:
            unstuffed.append(value)
    if escaped:
        raise HoboProtocolError("truncated OSerial escape")
    raw = bytes(unstuffed) + b"\xfd"
    if raw[1] & 0xFE:
        raise HoboProtocolError("unexpected OSerial communication channel")
    length = ((raw[1] & 1) << 8) | raw[2]
    if len(raw) != length + 6:
        raise HoboProtocolError("OSerial length mismatch")
    body = raw[1:-3]
    if crc16_x25(body) != int.from_bytes(raw[-3:-1], "little"):
        raise HoboProtocolError("OSerial CRC mismatch")
    return OSerialFrame(raw[3], raw[4:-3])


def _decode_tlvs(data: bytes) -> dict[int, bytes]:
    """Decode the simple tag/length/value fields in a U30 sensor header."""
    fields: dict[int, bytes] = {}
    index = 0
    while index + 2 <= len(data):
        tag = data[index]
        index += 1
        if tag == 0:
            continue
        if tag == 0xFF:
            break
        if tag & 0x80:
            if index >= len(data):
                break
            tag = (tag << 8) | data[index]
            index += 1
        if index >= len(data):
            break
        size = data[index]
        index += 1
        if size & 0x80:
            if index >= len(data):
                break
            size = ((size & 0x7F) << 8) | data[index]
            index += 1
        if index + size > len(data):
            break
        fields[tag] = data[index:index + size]
        index += size
    return fields


@dataclass(frozen=True)
class SensorDescriptor:
    instrument: int
    model: str
    measurement: str
    units: str
    sample_bits: Optional[int]
    preconverter: Optional[int]
    transfer_function: Optional[int]
    lower: Optional[float]
    upper: Optional[float]
    header_hex: str


def _read_conversion(data: bytes) -> tuple[str, str, Optional[int], Optional[float], Optional[float]]:
    """Read tag 28: NUL strings, transfer code, and big-endian float limits."""
    values: list[str] = []
    start = 0
    while len(values) < 2:
        end = data.find(b"\0", start)
        if end < 0:
            return "", "", None, None, None
        values.append(data[start:end].decode("ascii", errors="replace"))
        start = end + 1
    if start + 2 > len(data):
        return values[0], values[1], None, None, None
    function = data[start]
    parameter_bytes = data[start + 1]
    parameters = data[start + 2:start + 2 + parameter_bytes]
    lower = upper = None
    if function == 1 and len(parameters) >= 8:
        lower, upper = struct.unpack(">ff", parameters[:8])
    return values[0], values[1], function, lower, upper


def parse_sensor_header(instrument: int, response: bytes) -> SensorDescriptor:
    """Decode enough of a U30 ``P`` response to convert normal linear sensors.

    The directly attached S-LIA-XXXX PAR sensor stores 10-bit samples and a
    0..2560 engineering range in this header.  The parser keeps unsupported
    transfer functions as raw readings rather than guessing a conversion.
    """
    header_start = 1 if len(response) > 2 and response[1] == 0x27 else response.find(b"\x27")
    if header_start < 0 or header_start + 2 > len(response):
        raise HoboProtocolError("U30 returned no sensor instrument block")
    block_size = response[header_start + 1]
    block = response[header_start + 2:header_start + 2 + block_size]
    tags = _decode_tlvs(block)
    model = tags.get(50, b"").decode("ascii", errors="replace").strip("\0")
    sample_bits = tags.get(32, b"")
    preconverter = tags.get(38, b"")
    measurement, units, transfer, lower, upper = _read_conversion(tags.get(28, b""))
    if not measurement:
        strings = re.findall(rb"[ -~]{3,}", response)
        measurement = strings[-1].decode("ascii", errors="replace") if strings else "unknown"
    return SensorDescriptor(
        instrument=instrument,
        model=model or "unknown",
        measurement=measurement,
        units=units,
        sample_bits=sample_bits[0] if sample_bits else None,
        preconverter=preconverter[0] if preconverter else None,
        transfer_function=transfer,
        lower=lower,
        upper=upper,
        header_hex=response.hex(),
    )


def convert_reading(raw: int, descriptor: SensorDescriptor) -> Optional[float]:
    """Convert a raw sample where the U30 header declares a linear transfer."""
    bits = descriptor.sample_bits
    if (bits is None or bits <= 0 or descriptor.transfer_function != 1 or
            descriptor.lower is None or descriptor.upper is None):
        return None
    scale = 1 << bits
    if descriptor.preconverter == 1:  # PC01: centre point of the ADC bin
        normalized = (raw + 0.5) / scale
    elif descriptor.preconverter == 2:  # PC02: direct endpoint map
        normalized = raw / max(scale - 1, 1)
    elif descriptor.preconverter == 3:
        normalized = raw / scale
    else:
        return None
    return descriptor.lower + normalized * (descriptor.upper - descriptor.lower)


class HidU30Transport:
    """HID transport for a single U30, opened lazily so unit tests need no HID."""

    def __init__(self, path: Optional[str] = None, timeout_ms: int = 5000,
                 report_id_prefix: str = "auto") -> None:
        self.path = path
        self.timeout_ms = timeout_ms
        self.report_id_prefix = report_id_prefix
        self.device = None
        self.hidraw_fd: Optional[int] = None
        self.reassembler = HoboTalkReassembler()

    @staticmethod
    def _linux_hidraw_paths() -> list[Path]:
        """Return hidraw nodes whose kernel HID identity is the U30.

        Debian's ``python3-hid`` is built against HIDAPI's libusb backend.  It
        can enumerate the U30 but cannot claim the interface after the Linux
        ``hid-generic`` driver binds it.  hidraw is the native, permissioned
        byte-stream interface and preserves the same 64-byte reports.
        """
        nodes: list[Path] = []
        root = Path("/sys/class/hidraw")
        if not root.exists():
            return nodes
        identity = f"HID_ID=0003:{VID:08X}:{PID:08X}"
        for entry in sorted(root.glob("hidraw*")):
            try:
                uevent = (entry / "device" / "uevent").read_text(encoding="ascii")
            except OSError:
                continue
            if identity in uevent:
                nodes.append(Path("/dev") / entry.name)
        return nodes

    def open(self) -> None:
        if os.name == "posix":
            paths = self._linux_hidraw_paths()
            if self.path and self.path.startswith("/dev/"):
                paths = [Path(self.path)] if Path(self.path).exists() else []
            if paths:
                self.hidraw_fd = os.open(paths[0], os.O_RDWR | os.O_NONBLOCK)
                return
            raise RuntimeError("No HOBO U30 (USB VID 0x0f1b, PID 0x0301) is connected")
        try:
            import hid  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install python3-hid (Debian package) before running this program") from exc
        devices = hid.enumerate(VID, PID)
        if self.path:
            wanted = self.path.encode() if isinstance(self.path, str) else self.path
            devices = [device for device in devices if device["path"] == wanted]
        if not devices:
            raise RuntimeError("No HOBO U30 (USB VID 0x0f1b, PID 0x0301) is connected")
        self.device = hid.device()
        self.device.open_path(devices[0]["path"])

    def close(self) -> None:
        if self.hidraw_fd is not None:
            os.close(self.hidraw_fd)
            self.hidraw_fd = None
        if self.device is not None:
            self.device.close()
            self.device = None

    def write_frame(self, frame: bytes) -> None:
        if self.device is None and self.hidraw_fd is None:
            raise RuntimeError("U30 HID device is not open")
        if len(frame) > REPORT_SIZE:
            raise HoboProtocolError("U30 request exceeds the 64-byte HID report")
        report = frame.ljust(REPORT_SIZE, b"\0")
        if self.hidraw_fd is not None:
            written = os.write(self.hidraw_fd, b"\0" + report)
            if written != len(report) + 1:
                raise HoboProtocolError("the U30 hidraw write was incomplete")
            return
        # hidraw/hidapi expects a leading report-ID byte for unnumbered reports.
        if self.report_id_prefix in ("auto", "always"):
            report = b"\0" + report
        written = self.device.write(report)
        if written <= 0:
            raise HoboProtocolError("the U30 HID write failed")

    def read_frame(self, expected_command: int, source: int, destination: int) -> HoboTalkFrame:
        if self.device is None and self.hidraw_fd is None:
            raise RuntimeError("U30 HID device is not open")
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.hidraw_fd is not None:
                remaining = max(0, deadline - time.monotonic())
                if not select.select([self.hidraw_fd], [], [], min(0.25, remaining))[0]:
                    continue
                report = os.read(self.hidraw_fd, REPORT_SIZE + 1)
            else:
                report = self.device.read(REPORT_SIZE + 1, 250)
            if not report:
                continue
            frame = self.reassembler.feed(report)
            if frame is None:
                continue
            # The remaining bytes in a fixed HID report are padding or stale data.
            self.reassembler.clear()
            if frame.source != source or frame.destination != destination:
                continue
            if frame.command != expected_command:
                raise HoboProtocolError(
                    f"unexpected HOBOtalk command 0x{frame.command:02x}; expected 0x{expected_command:02x}")
            return frame
        raise TimeoutError(f"Timed out waiting for U30 HOBOtalk command 0x{expected_command:02x}")

    def transact(self, source: int, destination: int, command: int, payload: bytes,
                 expected_command: int) -> HoboTalkFrame:
        self.write_frame(encode_hobotalk(source, destination, command, payload))
        return self.read_frame(expected_command, destination, source)


class U30Client:
    """Read-only U30 sensor client."""

    def __init__(self, transport: HidU30Transport) -> None:
        self.transport = transport
        self.host_address: Optional[int] = None
        self._late_memory_replies: set[int] = set()

    def open(self, allow_session_override: bool = False) -> None:
        # HOBOware's pipelineOPENSYScommand uses payload 01 to take over an
        # occupied session (statuses 1/3/4). It is never a logger launch/erase.
        self._late_memory_replies.clear()
        self.transport.open()
        try:
            self.transport.transact(0xFF, 0x00, HT_GET, b"", HT_GET_REPLY)
            reply = self.transport.transact(0xFF, 0x00, HT_OPEN, b"\0", HT_OPEN_REPLY)
            if (allow_session_override and len(reply.payload) >= 2 and
                    reply.payload[0] in (1, 3, 4)):
                reply = self.transport.transact(0xFF, 0x00, HT_OPEN, b"\1", HT_OPEN_REPLY)
            if len(reply.payload) < 2 or reply.payload[0] != 0:
                raise HoboSessionUnavailable(reply.payload)
            self.host_address = reply.payload[1]
        except BaseException:
            self.transport.close()
            raise

    def close(self) -> None:
        try:
            if self.host_address is not None:
                self.transport.transact(self.host_address, 0x00, HT_CLOSE, b"\0", HT_CLOSE_REPLY)
        finally:
            self.host_address = None
            self.transport.close()

    def _oser(self, command: int, payload: bytes = b"") -> OSerialFrame:
        # No arbitrary device writes are exposed by this reader.
        if command not in (0x41, 0x43, 0x45, 0x4B, 0x4D, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x66):
            raise ValueError("command is outside the read/query allowlist")
        if self.host_address is None:
            raise RuntimeError("U30 session is not open")
        expected = command ^ 0xFF
        if expected in self._late_memory_replies:
            raise HoboProtocolError('a new session is required before retrying a timed-out memory query')
        original_timeout = getattr(self.transport, 'timeout_ms', None)
        budget_ms = original_timeout if type(original_timeout) in (int, float) else 5000
        deadline = time.monotonic() + budget_ms / 1000
        try:
            reply = self.transport.transact(
                self.host_address, LOCAL_SENSOR, HT_OSER, encode_oser(command, payload), HT_OSER_REPLY)
            response = decode_oser(reply.payload)
            while response.command != expected:
                # Only discard a CRC-verified reply to a memory request that
                # actually timed out earlier in this session. Do not resend
                # commands, accept arbitrary mismatches, or extend the deadline.
                if response.command not in self._late_memory_replies:
                    raise HoboProtocolError(
                        f"unexpected OSerial command 0x{response.command:02x}; expected 0x{expected:02x}")
                self._late_memory_replies.remove(response.command)
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('U30 query deadline expired after delayed memory reply')
                if type(original_timeout) in (int, float):
                    self.transport.timeout_ms = max(1, int(remaining*1000))
                reply = self.transport.read_frame(HT_OSER_REPLY, LOCAL_SENSOR, self.host_address)
                response = decode_oser(reply.payload)
            return response
        except TimeoutError:
            if command in (0x45, 0x4D):
                self._late_memory_replies.add(expected)
            raise
        finally:
            if type(original_timeout) in (int, float):
                self.transport.timeout_ms = original_timeout

    def attached_instruments(self) -> list[int]:
        mask_data = self._oser(0x51).payload
        mask = int.from_bytes(mask_data, "big")
        return [number for number in range(1, 16) if mask & (1 << number)]

    def descriptor(self, instrument: int) -> SensorDescriptor:
        return parse_sensor_header(instrument, self._oser(0x50, bytes((instrument,))).payload)

    def measure(self, descriptor: SensorDescriptor, settle_seconds: float = 1.0,
                trigger: bool = True) -> tuple[int, Optional[float], str]:
        if trigger:
            self._oser(0x43, b"\0")
            time.sleep(settle_seconds)
        for attempt in range(25):
            data = self._oser(0x43, bytes((descriptor.instrument,))).payload
            bits = descriptor.sample_bits
            if bits and data and data[0] == bits and len(data[1:]) * 8 >= bits:
                raw = int.from_bytes(data[1:], "big") >> (len(data[1:]) * 8 - bits)
                if raw >= (1 << bits) - 2:
                    return raw, None, data.hex()
                return raw, convert_reading(raw, descriptor), data.hex()
            if not data or data[0] != 0:
                raise HoboProtocolError(f"sensor reading has unexpected format: {data.hex()}")
            time.sleep(.075)
        raise TimeoutError("sensor conversion did not complete")


CSV_COLUMNS = [
    "timestamp_utc", "instrument", "model", "measurement", "units", "raw", "sample_bits",
    "engineering_value", "reading_response_hex", "header_hex",
]


def append_csv(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(record)


def read_once(client: U30Client, settle_seconds: float) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for instrument in client.attached_instruments():
        descriptor = client.descriptor(instrument)
        raw, value, response_hex = client.measure(descriptor, settle_seconds)
        records.append({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "raw": raw,
            "engineering_value": value,
            "reading_response_hex": response_hex,
            **asdict(descriptor),
        })
    return records


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read HOBO U30 attached-sensor data via USB HID")
    parser.add_argument("--once", action="store_true", help="read each detected sensor once and exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between read cycles (default: 30)")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds for each sensor conversion (default: 1)")
    parser.add_argument("--csv", type=Path, default=Path("readings.csv"), help="CSV output path")
    parser.add_argument("--path", help="optional hidapi device path when more than one U30 is connected")
    parser.add_argument("--report-id-prefix", choices=("auto", "always", "none"), default="auto")
    parser.add_argument("--wait-for-device", action="store_true",
                        help="keep trying when the U30 is unplugged (for a system service)")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.settle < 0:
        parser.error("--interval must be positive and --settle cannot be negative")

    transport = HidU30Transport(args.path, report_id_prefix=args.report_id_prefix)
    while True:
        transport = HidU30Transport(args.path, report_id_prefix=args.report_id_prefix)
        client = U30Client(transport)
        try:
            client.open()
            while True:
                for record in read_once(client, args.settle):
                    append_csv(args.csv, record)
                    print(json.dumps(record, ensure_ascii=False, allow_nan=False), flush=True)
                if args.once:
                    return 0
                time.sleep(args.interval)
        except (RuntimeError, TimeoutError, HoboProtocolError) as exc:
            if not args.wait_for_device or args.once:
                raise
            print(f"hobo-u30: {exc}; retrying in 15 seconds", file=sys.stderr, flush=True)
            time.sleep(15)
        finally:
            client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TimeoutError, HoboProtocolError) as exc:
        print(f"hobo-u30: {exc}", file=sys.stderr)
        raise SystemExit(2)
