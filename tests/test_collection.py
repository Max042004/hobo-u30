import math
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from web_server import Store, App
from pi_collection import utc
from u30_settings import validate_settings, launch_patch, U30Writer, apply_deployment
from hobo_u30 import HoboProtocolError, decode_oser, encode_oser
from u30_data import tlvs, EPOCH, memory_layout

BASE = 1704067200
HEADER = (Path(__file__).parent/"fixtures/header.bin").read_bytes()


def store_at(path):
    store = Store(path)
    with store.connect() as db:
        for key in ("PAR", "TEMP"):
            store.channel(db, {"key": key, "serial": "sensor", "kind": "sensor", "measurement": key, "units": "uE"})
    return store


def config(**kw):
    return {"channel": "PAR", "enabled": True, "interval": 30, "sample_interval": 10,
            "statistics": ["min", "max", "mean", "stddev"], "start_mode": "immediate", **kw}


def sample(store, second, value=1, deployment=1):
    store.save_snapshot({"serial": "u30", "observed_utc": utc(BASE+second),
        "archive": {"deployment": deployment, "launched_utc": utc(BASE), "channels": []},
        "sensors": [{"key": "PAR", "serial": "sensor", "measurement": "PAR", "units": "uE", "kind": "sensor",
                     "raw": value, "value": value, "quality": "ok"}]})


def readings(store, key="PAR"):
    with store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM samples WHERE channel=? ORDER BY timestamp_utc", (key,))]


def test_statistics_population_and_reboot_persistence(tmp_path):
    store = store_at(tmp_path)
    store.configure_collection(config(), BASE)
    sample(store, 0, 1)
    sample(store, 2, 999)  # Too early: not a scheduled sample.
    sample(store, 10, 2)
    store = Store(tmp_path)  # Persist the unfinished Welford accumulator.
    sample(store, 20, 3)
    sample(store, 30, 100)
    assert not readings(store)
    for name, expected in (("min", 1), ("max", 3), ("mean", 2), ("stddev", math.sqrt(2/3))):
        row = readings(store, "PAR:stat:"+name)[0]
        assert row["value"] == pytest.approx(expected)
        assert row["timestamp_utc"] == utc(BASE+30)
        assert row["quality"] == "statistics_n=3;expected=3;interval=30"
    assert store.collection_status()["channels"]["PAR"]["runtime"]["n"] == 1


def test_outage_never_fabricates_samples_and_deployment_does_not_mix(tmp_path):
    store = store_at(tmp_path)
    store.configure_collection(config(), BASE)
    sample(store, 0, 1)
    sample(store, 100, 7)
    row = readings(store, "PAR:stat:mean")[0]
    assert row["value"] == 1 and "n=1;expected=3" in row["quality"]
    sample(store, 110, 9, deployment=2)
    sample(store, 120, 11, deployment=2)
    rows = readings(store, "PAR:stat:mean")
    assert len(rows) == 2 and rows[1]["value"] == 9


def test_raw_intervals_and_start_modes(tmp_path):
    store = store_at(tmp_path)
    store.configure_collection(config(interval=5, statistics=[], start_mode="button"), BASE)
    sample(store, 0)
    assert not readings(store)
    store.start_collection("PAR", BASE+3)
    sample(store, 4)
    sample(store, 5)
    sample(store, 8)
    assert [r["timestamp_utc"] for r in readings(store)] == [utc(BASE+4), utc(BASE+8)]
    with pytest.raises(ValueError):
        store.start_collection("PAR", BASE+9)
    store.configure_collection(config(interval=5, statistics=[], start_mode="delayed", start_at=utc(BASE+20)), BASE+10)
    sample(store, 19)
    sample(store, 20)
    assert len(readings(store)) == 3
    store.configure_collection(config(interval=3600, statistics=[], start_mode="aligned"), BASE+1)
    runtime = store.collection_status()["channels"]["PAR"]["runtime"]
    assert runtime["start"] == BASE+3600
    sample(store, 3600)
    assert len(readings(store)) == 4
    store.configure_collection(config(enabled=False), BASE+4000)
    sample(store, 5000)
    assert len(readings(store)) == 4


@pytest.mark.parametrize("bad", [dict(interval=0),dict(interval=64801),dict(interval=True),dict(sample_interval=7),
    dict(statistics=["mean", "mean"]),dict(statistics=["median"]),dict(start_mode="x"),
    dict(start_mode="delayed",start_at="2020-01-01T00:00:00"),dict(channel=["PAR"])])
def test_schedule_validation(tmp_path, bad):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.configure_collection(config(**bad), BASE)
    assert not store.collection_status()["channels"]


def test_capacity_stop_and_wrap_only_touch_active_live_records(tmp_path):
    store = store_at(tmp_path)
    sample(store, 0)
    with store.connect() as db:
        store.insert_samples(db, [("u30","old","archive","PAR",utc(BASE),1,1,"ok")])
    store.configure_collection({"target":"storage","max_rows":2,"mode":"stop"})
    sample(store, 2)
    sample(store, 4)
    assert len(readings(store)) == 3  # Two live rows, one archive.
    assert store.collection_status()["storage"]["halted"]
    with pytest.raises(ValueError):
        store.configure_collection({"target":"storage","max_rows":2,"mode":"wrap"})
    store.configure_collection({"target":"storage","max_rows":2,"mode":"wrap","overwrite_confirmed":True})
    sample(store, 6)
    assert [r["timestamp_utc"] for r in readings(store) if r["source"]=="live"] == [utc(BASE+2),utc(BASE+6)]
    assert len([r for r in readings(store) if r["source"]=="archive"]) == 1
    assert not store.trash()  # Circular overwrite has explicit separate consent.


def native(**kw):
    return {"name":"New launch", "interval":600,"sample_interval":0,"start_mode":"immediate","memory_mode":"stop",**kw}


def test_native_launch_layout_matches_hoboware_olaunch_fields():
    c = validate_settings(native(), BASE)
    offset, value, rtc = launch_patch(HEADER,c,BASE)
    assert HEADER[offset:offset+4] == next(v for tag,v,*_ in tlvs(HEADER[:413],4) if tag==30)[:4]
    assert value[:4] == (BASE-EPOCH).to_bytes(4,"big")
    assert value[4:8] == bytes(4)
    assert int.from_bytes(value[8:12],"big") == BASE-EPOCH+1
    assert value[12:17] == bytes.fromhex("0258000000")
    assert int.from_bytes(value[18:21],"big") == memory_layout(HEADER)["sensor_start"]
    assert value[22:28] == bytes(6)
    assert value[29:33] == bytes.fromhex("000001e0")
    assert value[33:].startswith(b"New launch\0")
    for mode, flag, delay in (("button",1,0),("delayed",0,60),("aligned",0,600)):
        c=validate_settings(native(start_mode=mode,start_at=utc(BASE+60),memory_mode="wrap"),BASE)
        _,v,_=launch_patch(HEADER,c,BASE)
        assert v[15:17] == bytes([flag,1])
        assert int.from_bytes(v[8:12],"big")-rtc == delay


@pytest.mark.parametrize("bad", [dict(interval=64801),dict(interval=1.5),dict(memory_mode="x"),dict(name="x"*41),
    dict(sample_interval=10),dict(sample_interval=7,averaging_supported_confirmed=True),
    dict(start_mode="delayed",start_at=utc(BASE)),dict(name="bad\0name")])
def test_invalid_native_configuration_never_builds_writes(bad):
    with pytest.raises(ValueError):
        validate_settings(native(**bad),BASE)


def test_write_client_keeps_read_client_allowlist_and_validates_reply():
    transport=Mock()
    writer=U30Writer(transport)
    writer.host_address=176
    transport.transact.return_value=Mock(payload=encode_oser(0xbb))
    writer.stop_logging = Mock()  # Exercise the bounded command encoder separately.
    writer.command(0x44)
    assert decode_oser(transport.transact.call_args.args[3]).command == 0x44
    with pytest.raises(ValueError):
        writer._oser(0x44)
    with pytest.raises(ValueError):
        writer.command(0x4a)
    transport.transact.return_value=Mock(payload=encode_oser(0xba))
    with pytest.raises(HoboProtocolError):
        writer.command(0x44)


def test_no_device_write_when_backup_is_unstable_or_wrong_serial(tmp_path):
    image=HEADER+bytes(524288-len(HEADER))
    path=tmp_path/"backup.bin"
    path.write_bytes(image)
    import hashlib
    manifest={"sha256":hashlib.sha256(image).hexdigest(),"serial":"22005319","stable_header":False}
    with patch("station.backup",return_value=(path,manifest)),patch("u30_settings.writer_session") as writer:
        with pytest.raises(HoboProtocolError):
            apply_deployment(tmp_path,native(),"22005319",lambda *_:None)
        writer.assert_not_called()
        manifest["stable_header"]=True
        with pytest.raises(HoboProtocolError):
            apply_deployment(tmp_path,native(),"other",lambda *_:None)
        writer.assert_not_called()


@pytest.mark.parametrize("fail_readback", [False, True])
def test_full_native_write_is_ordered_and_never_launches_on_readback_failure(tmp_path, fail_readback):
    import hashlib
    from contextlib import nullcontext
    image=HEADER+bytes(524288-len(HEADER))
    path=tmp_path/"before.bin"
    path.write_bytes(image)
    manifest={"sha256":hashlib.sha256(image).hexdigest(),"serial":"22005319","stable_header":True}
    info=memory_layout(HEADER)
    after={**info,"logging_interval_seconds":600,"sampling_interval_seconds":0,"wrap_enabled":False}
    client=Mock()
    client.attached_instruments.return_value=[]
    client._oser.return_value=Mock(payload=b"\x01")
    chunks={}
    def command(op,payload=b""):
        if op==0x42:
            chunks[int.from_bytes(payload[:3],"big")]=payload[4:]
    client.command.side_effect=command
    def readback(_client, address, count):
        return bytes(count) if fail_readback else chunks[address]
    with patch("station.backup",return_value=(path,manifest)),patch("station.read_header",side_effect=[(HEADER,info),(HEADER,after)]), \
         patch("station.read_memory",side_effect=readback),patch("u30_settings.writer_session",return_value=nullcontext(client)), \
         patch("u30_settings.time.time",return_value=BASE),patch("u30_settings.time.sleep"):
        if fail_readback:
            with pytest.raises(HoboProtocolError):
                apply_deployment(tmp_path,native(),"22005319",lambda *_:None)
            assert all(c.args[0]!=0x46 for c in client.command.call_args_list)
        else:
            result=apply_deployment(tmp_path,native(),"22005319",lambda *_:None)
            assert result["phase"]=="verified"
            ops=[c.args[0] for c in client.command.call_args_list]
            assert ops[0]==0x4c and ops[-1]==0x46 and set(ops[1:-1])=={0x42}
            client.stop_logging.assert_called_once()
            assert client.command.call_args_list[-1].args==(0x46,b"\0")


def test_slow_pi_intervals_keep_chart_segments_with_per_record_schedule(tmp_path):
    store=store_at(tmp_path)
    store.configure_collection(config(interval=30,statistics=[]),BASE)
    for t in (0,30,60,150):
        sample(store,t,101)
    result=store.history({"source":["live"],"channel":["PAR"],"value_op":["gt"],"value":["100"]})
    assert result["interval_count"]==2


def test_native_alignment_uses_integer_rtc_seconds():
    c=validate_settings(native(start_mode="aligned"), BASE+.9)
    _, value, rtc=launch_patch(HEADER,c,BASE+.9)
    assert int.from_bytes(value[8:12],"big")+EPOCH==BASE+600


@pytest.mark.parametrize("wrap", [False, True])
def test_running_backup_freezes_before_pointer_and_never_imports_later_samples(tmp_path, wrap):
    from contextlib import nullcontext
    from station import backup
    from u30_data import parse_memory_header
    header=bytearray(HEADER)
    value,begin,end=next((v,b,e) for tag,v,b,e in tlvs(HEADER[:413],4) if tag==30)
    offset=end-len(value)
    header[offset+22:offset+26]=bytes(4)  # Real U30 keeps a stale header while logging.
    header[offset+16]=int(wrap)
    header=bytes(header)
    info=parse_memory_header(header)
    image=header+bytes(524288-len(header))
    before_bits=info["data_start"]*8+2*info["sample_bits"]
    after_bits=info["data_start"]*8+4*info["sample_bits"]
    pointers=[(b//8).to_bytes(3,"big")+bytes([b%8]) for b in (before_bits,after_bits)]
    client=Mock()
    def reply(command):
        return Mock(payload=pointers.pop(0) if command==0x4d else b"\x01")
    client._oser.side_effect=reply
    with patch("station.session",return_value=nullcontext(client)), \
         patch("station.read_header",side_effect=[(header,info),(header,info)]), \
         patch("station.read_memory",side_effect=lambda c,a,n:image[a:a+n]):
        path,manifest=backup(tmp_path/"backups")
    assert manifest["stable_header"]
    assert manifest["consistent_snapshot"] is (not wrap)
    result=Store(tmp_path/"db").import_archive(path)
    assert result["decoded"] is (not wrap)
    assert result["rows"] == (0 if wrap else 2*len(info["channels"]))


@pytest.mark.parametrize("pointer",[b"",bytes.fromhex("00000100"),bytes.fromhex("08000100"),bytes.fromhex("08000001"),bytes.fromhex("00050308")])
def test_invalid_native_memory_pointers_fail_closed(pointer):
    from station import read_data_pointer
    client=Mock()
    client._oser.return_value=Mock(payload=pointer)
    with pytest.raises(HoboProtocolError):read_data_pointer(client,memory_layout(HEADER))
