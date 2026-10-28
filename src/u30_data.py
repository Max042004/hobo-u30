"""U30 memory header and MSB-first sample decoding, independent of USB."""
from dataclasses import asdict
from functools import lru_cache
from datetime import datetime, timezone
from hobo_u30 import HoboProtocolError, parse_sensor_header, convert_reading, _decode_tlvs

EPOCH = 946684800


def tlvs(data, offset=0):
    """Yield (tag, value, offset, end), honoring two-byte tags and lengths."""
    pos = offset
    while pos < len(data):
        start = pos
        tag = data[pos]
        pos += 1
        if tag == 0:
            continue
        if tag == 255:
            return
        if tag & 128:
            if pos >= len(data):
                raise HoboProtocolError("truncated TLV tag")
            tag = tag * 256 + data[pos]
            pos += 1
        if pos >= len(data):
            raise HoboProtocolError("truncated TLV size")
        size = data[pos]
        pos += 1
        if size & 128:
            if pos >= len(data):
                raise HoboProtocolError("truncated TLV size")
            size = ((size & 127) << 8) | data[pos]
            pos += 1
        end = pos + size
        if end > len(data):
            raise HoboProtocolError(f"truncated TLV 0x{tag:04x}")
        yield tag, data[pos:end], start, end
        pos = end


def uint(data):
    return int.from_bytes(data, "big")


def timestamp(seconds):
    return datetime.fromtimestamp(seconds + EPOCH, timezone.utc).isoformat(timespec="seconds")


def text(data):
    return data.split(b"\0", 1)[0].decode("utf-8", errors="replace").replace("\x1b", "").strip()


def memory_layout(first_blocks):
    if first_blocks[:4] != b"HOBO":
        raise HoboProtocolError("memory does not have HOBO signature")
    # The fixed U30 header ends at 412; user/sensor tags follow separately.
    tags = {}
    for tag, value, start, end in tlvs(first_blocks, 4):
        tags[tag] = value
        if tag == 12:
            break
    launch = tags.get(30, b"")
    if len(launch) < 33:
        raise HoboProtocolError("missing launch parameters")
    return {"memory_bytes": uint(tags.get(6, b"\x02\x00")) * 1024,
            "data_start": uint(tags.get(12, b"\0\0\0")[:3]),
            "sensor_start": uint(launch[18:21]), "data_end": uint(launch[22:25]),
            "data_end_bit": launch[25], "wrap_count": uint(launch[26:28]),
            "wrap_enabled": bool(launch[16]), "logging_interval_seconds": uint(launch[12:14]),
            "sampling_interval_seconds": launch[14], "deployment": uint(tags.get(10, b"\0")),
            "serial": str(uint(tags.get(1, b"\0"))),
            "name": text(launch[33:]), "launched_utc": timestamp(uint(launch[4:8])),
            "first_sample_utc": timestamp(uint(launch[8:12])),
            "timezone_offset_minutes": int.from_bytes(launch[29:33], "big", signed=True)}


def parse_memory_header(image):
    info = memory_layout(image)
    start = info["data_start"]
    sensor_start = info["sensor_start"]
    if not (413 <= sensor_start <= start <= info["memory_bytes"]):
        raise HoboProtocolError("invalid sensor/data offsets")
    if len(image) < start:
        raise HoboProtocolError("incomplete memory header")
    fixed = dict((tag, value) for tag, value, _, _ in tlvs(image[:413], 4))
    info["log_battery"] = fixed.get(0x8135, b"\0") == b"\1"
    info["firmware_major"] = uint(fixed.get(2, b"\0"))
    info["product"] = text(fixed.get(51, b"HOBO U30 Station"))
    labels = {}
    user_tags = list(tlvs(image[413:sensor_start]))
    for tag, value, _, _ in user_tags:
        if tag == 0x8106:
            pos = 0
            while pos + 5 <= len(value):
                serial = uint(value[pos:pos+4])
                sub = value[pos+4]
                if not serial:
                    break
                end = value.find(b"\0", pos+5)
                if end < 0:
                    break
                labels[(str(serial), sub)] = text(value[pos+5:end])
                pos = end+1
        if tag == 52:
            info["notes"] = text(value)
    channels = []
    for tag, value, offset, end in tlvs(image[sensor_start:start]):
        if tag != 39:
            continue
        desc = parse_sensor_header(len(channels)+1, image[sensor_start+offset:sensor_start+end])
        tags = _decode_tlvs(value)
        serial = str(uint(tags.get(1, b"\0")))
        sub = uint(tags.get(0x8134, b"\1"))
        channel = {**asdict(desc), "serial": serial, "subchannel": sub,
                   "label": labels.get((serial, sub), ""), "kind": "sensor"}
        channel["key"] = f"{serial}:{sub}:{desc.measurement}"
        channels.append(channel)
    if not channels:
        raise HoboProtocolError("archive has no sensor channels")
    if info["log_battery"]:
        channels.append({"key": info["serial"]+":battery", "serial": info["serial"],
                         "subchannel": 0, "label": "電池電壓", "measurement": "Battery",
                         "units": "V", "kind": "battery", "sample_bits": 10 if info["firmware_major"] > 1 else 8,
                         "model": "U30 internal battery", "instrument": 0})
    info["channels"] = channels
    info["sample_bits"] = sum(c["sample_bits"] or 0 for c in channels)
    info["used_percent"] = min(100, (info["data_end"]*8+info["data_end_bit"]+8) / (info["memory_bytes"]*8)*100)
    info["memory_full"] = info["data_end"] >= info["memory_bytes"]
    return info


def get_bits(data, offset, count):
    if count <= 0 or count > 64 or offset < 0 or offset + count > len(data)*8:
        raise HoboProtocolError("sample bit range out of bounds")
    first, inner = divmod(offset, 8)
    size = (inner+count+7)//8
    return (uint(data[first:first+size]) >> (size*8-inner-count)) & ((1 << count)-1)


@lru_cache(maxsize=256)
def cached_descriptor(instrument, header_hex):
    return parse_sensor_header(instrument, bytes.fromhex(header_hex))


def channel_value(raw, channel):
    bits = channel["sample_bits"]
    if raw >= (1 << bits)-2:
        return None, "missing" if raw == (1 << bits)-2 else "eof"
    if channel["kind"] == "battery":
        return raw*.02414/(4 if bits == 10 else 1), "ok"
    desc = cached_descriptor(channel["instrument"], channel["header_hex"])
    value = convert_reading(raw, desc)
    return value, "ok" if value is not None else "unsupported_conversion"


def iter_archive(image, info=None):
    """Non-wrapping archives; refuse circular layouts until validated."""
    info = info or parse_memory_header(image)
    if info["wrap_count"]:
        raise HoboProtocolError("circular memory decoding is not yet validated; raw backup preserved")
    if len(image) != info["memory_bytes"]:
        raise HoboProtocolError("full memory image required")
    bits_per = info["sample_bits"]
    if bits_per <= 0 or any(not c.get("sample_bits") for c in info["channels"]):
        raise HoboProtocolError("unknown channel sample width")
    limit = min(len(image)*8, info["data_end"]*8+info["data_end_bit"])
    offset = info["data_start"]*8
    epoch = datetime.fromisoformat(info["first_sample_utc"]).timestamp()
    index = 0
    while offset + bits_per <= limit:
        first_bits = info["channels"][0]["sample_bits"]
        if get_bits(image, offset, first_bits) == (1 << first_bits)-1:
            break
        when = datetime.fromtimestamp(epoch + index*info["logging_interval_seconds"], timezone.utc).isoformat(timespec="seconds")
        cursor = offset
        for channel in info["channels"]:
            raw = get_bits(image, cursor, channel["sample_bits"])
            value, quality = channel_value(raw, channel)
            yield {"timestamp_utc": when, "channel_key": channel["key"],
                   "raw": raw, "value": value, "quality": quality, "sample_index": index}
            cursor += channel["sample_bits"]
        offset += bits_per
        index += 1
