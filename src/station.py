"""U30 queries and verified memory backups, with explicit optional stop."""
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from hobo_u30 import HidU30Transport, U30Client, HoboProtocolError, HoboSessionUnavailable, _decode_tlvs
from u30_data import memory_layout, parse_memory_header, uint, tlvs


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def session(allow_session_override=False):
    client = U30Client(HidU30Transport())
    client.open(allow_session_override=allow_session_override)
    failed = False
    try:
        yield client
    except BaseException:
        failed = True
        raise
    finally:
        try:
            client.close()
        except Exception:
            if not failed:
                raise


def read_memory(client, address, count):
    if not (0 <= address < 524288 and 1 <= count <= 256 and address+count <= 524288):
        raise ValueError("invalid U30 memory range")
    data = client._oser(0x45, address.to_bytes(3, "big")+bytes((count % 256,))).payload
    if len(data) != count:
        raise HoboProtocolError(f"short memory block at {address}: wanted {count}, got {len(data)}")
    return data


def read_header(client):
    image = read_memory(client, 0, 256)
    layout = memory_layout(image)
    if not (413 <= layout["data_start"] <= 5912 and layout["memory_bytes"] == 524288):
        raise HoboProtocolError("unexpected U30 memory layout")
    while len(image) < layout["data_start"]:
        image += read_memory(client, len(image), min(256, layout["data_start"]-len(image)))
    return image, parse_memory_header(image)


class InvalidDataPointer(HoboProtocolError):
    """A CRC-verified M reply whose value cannot describe this memory."""
    def __init__(self, message, raw):
        super().__init__(message)
        self.raw_hex = raw.hex()


def read_data_pointer(client, info):
    data = client._oser(0x4D).payload
    if len(data) != 4 or data[3] > 7:
        raise InvalidDataPointer("invalid current U30 data pointer", data)
    address, bit = uint(data[:3]), data[3]
    if not info["data_start"] <= address <= info["memory_bytes"] or (address == info["memory_bytes"] and bit):
        raise InvalidDataPointer(f"current U30 data pointer is out of bounds: {data.hex()}", data)
    return {"data_end": address, "data_end_bit": bit}


def pointer_usage(info, pointer):
    info.update(pointer)
    info["used_percent"] = min(100, (pointer["data_end"]*8+pointer["data_end_bit"]+8)/(info["memory_bytes"]*8)*100)
    info["memory_full"] = pointer["data_end"] >= info["memory_bytes"] or bool(info["wrap_count"])
    if info["wrap_count"]:
        info["used_percent"] = 100


def live_archive_metadata(client, serial):
    """Optional memory diagnostics; live identity/calibration come from USB/P.

    Never reuse an old deployment if memory cannot identify the current one.
    Limit optional memory requests so a timeout does not stall polling for the
    full sensor conversion timeout. Archive backups use the strict reader.
    """
    timeout = getattr(client.transport, "timeout_ms", None)
    if type(timeout) in (int, float):
        client.transport.timeout_ms = min(timeout, 1000)
    try:
        try:
            _, archive = read_header(client)
            archive = dict(archive)
            if archive["serial"] != serial:
                raise HoboProtocolError("system and memory serial numbers do not match")
        except (HoboProtocolError, TimeoutError) as exc:
            return {"serial": serial, "product": "HOBO U30 Station", "name": "",
                    "metadata_valid": False, "metadata_error": str(exc),
                    "memory_health": "header_unavailable", "pointer_valid": False,
                    "pointer_error": None, "channels": [], "deployment": None,
                    "launched_utc": None, "first_sample_utc": None,
                    "memory_bytes": None, "data_start": None, "sensor_start": None,
                    "data_end": None, "data_end_bit": None, "used_percent": None,
                    "memory_full": None, "wrap_count": None, "wrap_enabled": None,
                    "logging_interval_seconds": None, "sampling_interval_seconds": None}
        archive.update(metadata_valid=True, memory_health="healthy")
        try:
            pointer_usage(archive, read_data_pointer(client, archive))
            archive["pointer_valid"] = True
        except (HoboProtocolError, TimeoutError) as exc:
            archive.update(pointer_valid=False, pointer_error=str(exc),
                           memory_health="pointer_invalid", data_end=None,
                           data_end_bit=None, used_percent=None, memory_full=None)
        return archive
    finally:
        if type(timeout) in (int, float):
            client.transport.timeout_ms = timeout


def header_identity(header):
    result = bytearray(header)
    for tag, value, start, end in tlvs(header[:413], 4):
        if tag == 30:
            offset = end-len(value)
            result[offset+22:offset+28] = bytes(6)
    return bytes(result)


def descriptor_channel(descriptor, archive):
    data = bytes.fromhex(descriptor.header_hex)
    offset = data.find(b"\x27")
    if offset < 0:
        raise HoboProtocolError("no sensor tag")
    size = data[offset+1]
    tags = _decode_tlvs(data[offset+2:offset+2+size])
    serial = str(uint(tags.get(1, b"\0")))
    sub = uint(tags.get(0x8134, b"\1"))
    key = f"{serial}:{sub}:{descriptor.measurement}"
    known = next((c for c in archive["channels"] if c["key"] == key), {})
    return {**asdict(descriptor), "key": key, "serial": serial,
            "subchannel": sub, "label": known.get("label", ""), "kind": "sensor"}


class StationReader:
    """Keep slow station metadata separate from fresh sensor conversions."""

    def __init__(self, metadata_interval=60):
        self.metadata_interval = metadata_interval
        self._occupied_failures = 0
        self._next_override = 0
        self.invalidate()

    def invalidate(self):
        self.archive = None
        self.metadata_at = 0
        self.metadata_observed_utc = None
        self.last_status = None
        self.battery_raw = b""

    def read(self):
        recover = self._occupied_failures >= 3 and time.monotonic() >= self._next_override
        if recover:
            self._next_override = time.monotonic() + 60
            logging.getLogger("u30").warning("Trying HOBOware session takeover after repeated occupied-session replies")
        try:
            result = self._read(recover)
        except HoboSessionUnavailable as exc:
            self._occupied_failures = self._occupied_failures + 1 if exc.occupied else 0
            raise
        except Exception:
            self._occupied_failures = 0
            raise
        self._occupied_failures = 0
        return result

    def _read(self, recover=False):
        context = session(allow_session_override=True) if recover else session()
        with context as client:
            status_raw = client._oser(0x4F).payload
            code = status_raw[0] if status_raw else -1
            system = client.transport.transact(client.host_address, 0, 1, b"", 2).payload
            if len(system) < 29:
                raise HoboProtocolError("system identity response too short")
            serial = str(uint(system[13:17]))
            refresh = (self.archive is None or serial != self.archive["serial"] or
                       code != self.last_status or time.monotonic()-self.metadata_at >= self.metadata_interval)
            if refresh:
                archive = live_archive_metadata(client, serial)
                battery_raw = client._oser(0x66).payload
                if len(battery_raw) < 3 or battery_raw[2] > 100:
                    raise HoboProtocolError("invalid battery response")
                self.archive, self.battery_raw = archive, battery_raw
                self.metadata_at = time.monotonic()
                self.metadata_observed_utc = now()
                self.last_status = code
            archive = self.archive
            slots = set()
            if code == 0:
                client._oser(0x52)
            for attempt in range(3):
                slots.update(client.attached_instruments())
                if attempt < 2:
                    time.sleep(.25)
            # Refresh sensor identity every cycle so a replacement in the same
            # slot never inherits the previous sensor's calibration.
            descriptors = []
            for slot in sorted(slots):
                for attempt in range(4):
                    try:
                        descriptors.append(client.descriptor(slot))
                        break
                    except HoboProtocolError:
                        if attempt == 3:
                            raise
                        # Native logging can briefly occupy a sensor during P.
                        # Retry fresh identity; never reuse old calibration.
                        time.sleep((.1, .25, .5)[attempt])
            if descriptors and code != 1:
                client._oser(0x43, b"\0")
                time.sleep(1)
            sensors = []
            for descriptor in descriptors:
                channel = descriptor_channel(descriptor, archive)
                try:
                    raw, value, response = client.measure(descriptor, trigger=False)
                    quality = "ok" if value is not None else "missing_or_unsupported"
                    channel.update(raw=raw, value=value, quality=quality, response_hex=response)
                except (HoboProtocolError, TimeoutError) as exc:
                    channel.update(raw=None, value=None, quality="error", error=str(exc))
                sensors.append(channel)
            return {"observed_utc": now(), "serial": serial, "product": archive["product"],
                    "name": archive["name"], "battery_percent": self.battery_raw[2],
                    "battery_response_hex": self.battery_raw.hex(), "status_code": code,
                    "logging": code == 1, "firmware": system[23:29].decode("ascii", errors="replace").strip("\0 "),
                    "hardware_firmware": system[17:23].decode("ascii", errors="replace").strip("\0 "),
                    "metadata_observed_utc": self.metadata_observed_utc,
                    "sensor_slots": sorted(slots),
                    "sensors": sensors, "archive": archive}


def snapshot():
    return StationReader().read()


def backup(directory, progress=lambda done, total: None, stop_logging=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    from u30_settings import writer_session
    context = writer_session() if stop_logging else session()
    with context as client:
        status_before = client._oser(0x4F).payload.hex()
        if stop_logging and status_before[:2] in ("01", "03", "05"):
            client.stop_logging()
        header, info = read_header(client)
        pointer_errors = {}
        def pointer(label, layout):
            try:
                return read_data_pointer(client, layout)
            except InvalidDataPointer as exc:
                # Do not swallow USB, CRC or timeout errors. Only an intact M
                # reply with an unusable value permits bounded raw readout.
                pointer_errors[label] = {"error": str(exc), "raw_hex": exc.raw_hex}
                return None
        pointer_before = pointer("before", info)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + info["serial"]
        image_path = directory / (name + ".bin")
        temporary = directory / (name + ".part")
        size = info["memory_bytes"]
        with temporary.open("xb") as target:
            for address in range(0, size, 256):
                target.write(read_memory(client, address, min(256, size-address)))
                if (address+256) % 8192 == 0:
                    progress(address+256, size*2 if pointer_errors else size)
            target.flush()
            os.fsync(target.fileno())
        after, after_info = read_header(client)
        pointer_after = pointer("after", after_info)
        image = temporary.read_bytes()
        stable = header == after == image[:len(header)]
        status_after = client._oser(0x4F).payload.hex()
        stopped = status_after == "00" and (status_before == "00" or stop_logging)
        # A non-wrapping logger can append while USB downloads older bytes.
        # Freeze the completed prefix at the BEFORE pointer, never the later one.
        safe_prefix = (pointer_before is not None and pointer_after is not None and
                       (not info["wrap_enabled"] or stopped) and not info["wrap_count"] and not after_info["wrap_count"] and
                       header_identity(header) == header_identity(after) and image[:len(header)] == header and
                       pointer_before["data_end"]*8+pointer_before["data_end_bit"] <=
                       pointer_after["data_end"]*8+pointer_after["data_end_bit"])
        consistent = safe_prefix and status_before in ("00", "01", "03", "05") and status_after in ("00", "01", "03", "05")
        recovery = bool(pointer_errors)
        memory_verified, verification_error = False, None
        if recovery:
            # Verify every byte a second time. EOF is deliberately irrelevant:
            # both passes only touch the independently validated capacity.
            progress(size, size*2)
            try:
                for address in range(0, size, 256):
                    count = min(256, size-address)
                    if read_memory(client, address, count) != image[address:address+count]:
                        raise HoboProtocolError(f"raw memory changed during verification at {address}")
                    if (address+256) % 8192 == 0:
                        progress(size+address+count, size*2)
                final_header, final_info = read_header(client)
                final_status = client._oser(0x4F).payload.hex()
                memory_verified = (stable and final_header == header and final_info["serial"] == info["serial"]
                                   and status_before == status_after == final_status == "00")
                if not memory_verified:
                    verification_error = "raw image read twice, but stopped state or header stability could not be verified"
                status_after = final_status
            except (HoboProtocolError, TimeoutError, OSError) as exc:
                # The complete first pass is still evidence worth preserving.
                # It is not sufficient to authorize a new native deployment.
                verification_error = str(exc)
        manifest = {"id": name, "created_utc": now(), "serial": info["serial"],
                    "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                    "stable_header": stable, "status_before_hex": status_before,
                    "complete_image": len(image) == size,
                    "raw_recovery": recovery, "memory_verified_twice": memory_verified,
                    "recovery_verified": recovery and memory_verified,
                    "verification_error": verification_error,
                    "pointer_errors": pointer_errors,
                    "requested_stop": stop_logging,
                    "consistent_snapshot": consistent,
                    "snapshot_bounds": pointer_before if consistent else None,
                    "pointer_after": pointer_after,
                    "status_after_hex": status_after, "archive": info}
        # Preserve raw data even if logging changed the header during transfer.
        temporary.rename(image_path)
        manifest_path = image_path.with_suffix(".json")
        with manifest_path.open("w", encoding="utf-8") as target:
            target.write(json.dumps(manifest, ensure_ascii=False, indent=2))
            target.flush()
            os.fsync(target.fileno())
        progress(size*2 if recovery else size, size*2 if recovery else size)
        return image_path, manifest
