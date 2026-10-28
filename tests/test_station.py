import csv
import json
import sys
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from contextlib import nullcontext

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from hobo_u30 import (crc16_x25, decode_hobotalk, decode_oser, encode_oser,
                      HoboTalkReassembler, HoboProtocolError, HoboSessionUnavailable,
                      U30Client, parse_sensor_header)
from u30_data import parse_memory_header, iter_archive, get_bits, channel_value
from web_server import Store
from station import StationReader

FIXTURES = Path(__file__).parent/"fixtures"
HEADER = (FIXTURES/"header.bin").read_bytes()
INFO = parse_memory_header(HEADER)
IMAGE = Path(__file__).resolve().parents[1]/"data/backups/20261018T082232Z-22005319.bin"


def test_occupied_session_is_not_overridden_by_default():
    transport = Mock()
    transport.transact.side_effect = [Mock(payload=b""), Mock(payload=b"\x01\xff")]
    client = U30Client(transport)
    with pytest.raises(HoboSessionUnavailable) as error:
        client.open()
    assert error.value.occupied and error.value.status == 1
    assert transport.transact.call_count == 2
    transport.close.assert_called_once()
    assert client.host_address is None


@pytest.mark.parametrize("status", [1, 3, 4])
def test_verified_hoboware_takeover_recovers_occupied_session(status):
    transport = Mock()
    transport.transact.side_effect = [Mock(payload=b""), Mock(payload=bytes([status, 255])), Mock(payload=b"\0\xb0")]
    client = U30Client(transport)
    client.open(allow_session_override=True)
    assert client.host_address == 0xb0
    assert transport.transact.call_args.args == (255, 0, 5, b"\1", 6)
    transport.close.assert_not_called()


@pytest.mark.parametrize("payload", [b"\x02\xff", b"\x08\xff", b"", b"\0"])
def test_unknown_or_power_error_never_triggers_takeover(payload):
    transport = Mock()
    transport.transact.side_effect = [Mock(payload=b""), Mock(payload=payload)]
    with pytest.raises(HoboSessionUnavailable):
        U30Client(transport).open(allow_session_override=True)
    assert transport.transact.call_count == 2
    transport.close.assert_called_once()


def test_reader_requires_three_occupied_failures_and_rate_limits_recovery():
    reader = StationReader()
    reader._read = Mock(side_effect=HoboSessionUnavailable(b"\x01\xff"))
    clock = [100]
    with patch("station.time.monotonic", side_effect=lambda: clock[0]):
        for i in range(5):
            with pytest.raises(HoboSessionUnavailable):
                reader.read()
            reader.invalidate()  # Worker invalidation must retain recovery state.
        assert [c.args[0] for c in reader._read.call_args_list] == [False, False, False, True, False]
        clock[0] = 161
        with pytest.raises(HoboSessionUnavailable):
            reader.read()
        assert reader._read.call_args.args == (True,)
        reader._read.side_effect = None
        reader._read.return_value = {"fresh": True}
        assert reader.read() == {"fresh": True}
        assert reader._occupied_failures == 0


def test_all_203_recorded_frames_and_193_nested_messages():
    frames = json.loads((FIXTURES/"usb-frames.json").read_text())
    nested = 0
    for item in frames:
        raw = bytes.fromhex(item["ht_hex"])
        frame = decode_hobotalk(raw)
        parser = HoboTalkReassembler()
        parsed = None
        for offset in range(0, len(raw), 64):
            # Deliberately use a valid old frame in the remaining HID tail.
            chunk = raw[offset:offset+64]
            parsed = parser.feed((chunk+raw*64)[:64])
        assert parsed.raw == raw
        if item["oser"]:
            response = decode_oser(frame.payload)
            assert response.command == item["oser"]["command"]
            assert response.payload == bytes.fromhex(item["oser"]["data_hex"])
            nested += 1
    assert len(frames) == 203
    assert nested == 193


def test_escape_and_extended_response_length_and_corruption():
    raw = encode_oser(0x50, b"\xc0\xfd\x7d")
    assert decode_oser(raw).payload == b"\xc0\xfd\x7d"
    body = b"\x01\x01\xba"+bytes(range(256))
    encoded = bytearray(b"\xc0")
    for value in body+crc16_x25(body).to_bytes(2,"little"):
        encoded.extend((125, value^32) if value in (192,253,125) else (value,))
    encoded.append(253)
    assert decode_oser(bytes(encoded)).payload == bytes(range(256))
    encoded[7] ^= 1
    with pytest.raises(HoboProtocolError):
        decode_oser(bytes(encoded))


def test_live_ten_bit_alignment_and_missing_value():
    channel = INFO["channels"][3]
    descriptor = parse_sensor_header(1, bytes.fromhex(channel["header_hex"]))
    client = U30Client(Mock())
    client._oser = Mock(return_value=Mock(payload=bytes.fromhex("0a0100")))
    raw, value, response = client.measure(descriptor, trigger=False)
    assert raw == 4 and value == 11.25
    client._oser.return_value.payload = bytes.fromhex("0aff80")
    assert client.measure(descriptor, trigger=False)[1] is None


def test_header_labels_time_and_ten_channels():
    assert INFO["serial"] == "22005319"
    assert INFO["logging_interval_seconds"] == 600
    assert INFO["first_sample_utc"] == "2024-05-07T03:33:46+00:00"
    assert len(INFO["channels"]) == 10
    assert INFO["channels"][3]["label"] == "Par2"
    assert INFO["sample_bits"] == 138
    with pytest.raises(HoboProtocolError):
        list(iter_archive(HEADER))
    with pytest.raises(HoboProtocolError, match="circular"):
        list(iter_archive(HEADER, {**INFO, "wrap_count": 1}))


@pytest.mark.parametrize("busy", [False, True])
def test_fast_poll_reuses_station_metadata_but_measures_every_cycle(busy):
    client = Mock()
    client.host_address = 123
    code = [0]
    def oser(command, payload=b""):
        return Mock(payload={0x4F: bytes(code), 0x66: bytes.fromhex("02e464"), 0x4D: bytes.fromhex("08000000")}.get(command, b""))
    client._oser.side_effect = oser
    system = bytes.fromhex("00000000005533302020202020014fc64776322e30303176322e3030360000")
    client.transport.transact.side_effect = lambda host, target, command, payload, expected: Mock(
        payload=system if target == 0 else b"\0\0")
    client.attached_instruments.return_value = [1]
    client.descriptor.return_value = parse_sensor_header(1, bytes.fromhex(INFO["channels"][3]["header_hex"]))
    if busy:
        client.descriptor.side_effect = [HoboProtocolError("sensor busy")]+[client.descriptor.return_value]*4
    client.measure.side_effect = [(98, 246.25, "0a1880"), (6, 16.25, "0a0180"),
                                 (50, 126.25, "0a0c80"), (4, 11.25, "0a0100")]
    with patch("station.session", side_effect=lambda: nullcontext(client)), \
         patch("station.read_header", return_value=(HEADER, INFO)) as header, \
         patch("station.time.sleep"):
        reader = StationReader()
        assert reader.read()["sensors"][0]["value"] == 246.25
        assert reader.read()["sensors"][0]["value"] == 16.25
        assert header.call_count == 1
        assert client.measure.call_count == 2
        code[0] = 1  # Changed logging state invalidates cached metadata.
        assert reader.read()["sensors"][0]["value"] == 126.25
        assert header.call_count == 2
        reader.invalidate()  # A reconnect must fetch metadata again.
        assert reader.read()["sensors"][0]["value"] == 11.25
        assert header.call_count == 3
        assert client.descriptor.call_count == 4+int(busy)
        assert all(call.args[1] not in (7, 8) for call in client.transport.transact.call_args_list)


@pytest.mark.parametrize("status", [0, 1])
def test_invalid_archive_pointer_does_not_block_fresh_sensors_and_recovers(status):
    from copy import deepcopy
    client = Mock(host_address=123)
    pointer = [bytes.fromhex("0b78af00")]  # Observed on U30 22005319 after reconnect.
    client._oser.side_effect = lambda command, payload=b"": Mock(payload={
        0x4F: bytes([status]), 0x66: bytes.fromhex("02ba46"), 0x4D: pointer[0]
    }.get(command, b""))
    system = bytes.fromhex("00000000005533302020202020014fc64776322e30303176322e3030360000")
    client.transport.transact.side_effect = lambda host, target, command, payload, expected: Mock(
        payload=system if target == 0 else b"\0\0")
    client.attached_instruments.return_value = [1]
    client.descriptor.return_value = parse_sensor_header(1, bytes.fromhex(INFO["channels"][3]["header_hex"]))
    client.measure.side_effect = [(98, 246.25, "0a1880"), (6, 16.25, "0a0180")]
    with patch("station.session", side_effect=lambda: nullcontext(client)), \
         patch("station.read_header", side_effect=lambda _: (HEADER, deepcopy(INFO))), \
         patch("station.time.sleep"):
        reader = StationReader(metadata_interval=0)
        first = reader.read()
        assert first["sensors"][0]["value"] == 246.25
        assert first["logging"] is bool(status)
        assert first["archive"]["pointer_valid"] is False
        assert "0b78af00" in first["archive"]["pointer_error"]
        assert all(first["archive"][key] is None for key in ("data_end", "data_end_bit", "used_percent", "memory_full"))
        assert any(call.args[0] == 0x52 for call in client._oser.call_args_list) is (status == 0)
        pointer[0] = bytes.fromhex("08000000")
        second = reader.read()
        assert second["sensors"][0]["value"] == 16.25
        assert second["archive"]["pointer_valid"] is True
        assert second["archive"]["data_end"] == 524288
        assert second["archive"]["used_percent"] == 100
        assert "pointer_error" not in second["archive"]


@pytest.mark.skipif(not IMAGE.exists(), reason="hardware archive is local evidence")
def test_100_samples_against_original_hoboware_bit_reader_and_conversion():
    image = IMAGE.read_bytes()
    with (FIXTURES/"hoboware-reference.csv").open() as f:
        for row in csv.DictReader(f):
            c = int(row["channel"])
            channel = INFO["channels"][c]
            offset = 1755*8+int(row["sample"])*138+sum(x["sample_bits"] for x in INFO["channels"][:c])
            raw = get_bits(image, offset, channel["sample_bits"])
            assert raw == int(row["raw"])
            value, quality = channel_value(raw, channel)
            if quality == "ok":
                assert value == pytest.approx(float(row["value"]), abs=1e-10)


@pytest.mark.skipif(not IMAGE.exists(), reason="hardware archive is local evidence")
def test_archive_import_deduplication_and_taipei_date_filters(tmp_path):
    store = Store(tmp_path)
    result = store.import_archive(IMAGE)
    assert result["rows"] == 302910
    assert result["last_utc"] == "2024-12-03T11:53:46+00:00"
    assert store.import_archive(IMAGE)["id"] == result["id"]
    history = store.history({"source":["archive"],"channel":["21921906:1:PAR"],
                             "start":["2024-05-08"],"end":["2024-05-08"]})
    assert history["total"] == 144
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 302910
