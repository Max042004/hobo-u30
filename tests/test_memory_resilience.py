import itertools
import json
import sys
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from hobo_u30 import HoboProtocolError, parse_sensor_header, U30Client, encode_oser
from history_tools import HistoryConflict
from station import StationReader, live_archive_metadata, read_data_pointer
from u30_data import parse_memory_header
from web_server import App, Store

HEADER=(Path(__file__).parent/'fixtures/header.bin').read_bytes()
INFO=parse_memory_header(HEADER)
SERIAL=INFO['serial']

def client_for_readings():
    client=Mock(host_address=123)
    client.transport.timeout_ms=5000
    client._oser.side_effect=lambda command,payload=b'': Mock(payload={
        0x4F:b'\x00',0x66:bytes.fromhex('02ba46'),0x4D:bytes.fromhex('08000000')
    }.get(command,b''))
    system=bytes.fromhex('00000000005533302020202020014fc64776322e30303176322e3030360000')
    client.transport.transact.side_effect=lambda host,target,command,payload,expected: Mock(payload=system if target==0 else b'\0\0')
    channel=next(c for c in INFO['channels'] if c['measurement']=='PAR')
    client.attached_instruments.return_value=[1]
    client.descriptor.return_value=parse_sensor_header(1,bytes.fromhex(channel['header_hex']))
    client.measure.side_effect=[(10,26.25,'0a0280'),(20,51.25,'0a0500'),(30,76.25,'0a0780'),(40,101.25,'0a0a00')]
    return client

@pytest.mark.parametrize('failure',[HoboProtocolError('bad memory header'),TimeoutError('memory timed out'),'wrong_serial'])
def test_fresh_samples_survive_header_failure_and_recovery_without_old_deployment(tmp_path,failure):
    client=client_for_readings()
    times=itertools.count()
    clock=lambda: (datetime(2026,10,26,tzinfo=timezone.utc)+timedelta(seconds=next(times))).isoformat(timespec='seconds')
    bad=(HEADER,{**deepcopy(INFO),'serial':'other-device'}) if failure=='wrong_serial' else failure
    store=Store(tmp_path)
    with patch('station.session',side_effect=lambda:nullcontext(client)), \
         patch('station.read_header',side_effect=[(HEADER,deepcopy(INFO)),bad,bad,(HEADER,deepcopy(INFO))]), \
         patch('station.time.sleep'),patch('station.now',side_effect=clock):
        reader=StationReader(metadata_interval=0)
        snapshots=[]
        for _ in range(4):
            snap=reader.read()
            store.save_snapshot(snap)
            snapshots.append(snap)
    assert [s['sensors'][0]['value'] for s in snapshots]==[26.25,51.25,76.25,101.25]
    assert [s['archive']['metadata_valid'] for s in snapshots]==[True,False,False,True]
    assert snapshots[1]['archive']['channels']==[]
    assert snapshots[1]['archive']['deployment'] is None
    assert snapshots[1]['archive']['used_percent'] is None
    assert snapshots[1]['archive']['memory_full'] is None
    assert snapshots[1]['sensors'][0]['label']==''  # No stale header labels.
    assert client.descriptor.call_count==4
    assert client.transport.timeout_ms==5000
    with store.connect() as db:
        samples=db.execute('SELECT deployment,value FROM samples ORDER BY timestamp_utc').fetchall()
        assert len(samples)==4
        assert [r['deployment'] for r in samples][1:3]==['live-unverified']*2
        assert samples[0]['deployment']==samples[3]['deployment']!=samples[1]['deployment']
        events=[json.loads(r[0]) for r in db.execute("SELECT details FROM events WHERE event_type='memory_state' ORDER BY timestamp_utc")]
        assert [e['after'] for e in events]==['header_unavailable','healthy']
        assert events[0]['error']
    assert not any(call.args[0] in (0x42,0x44,0x46,0x4C) for call in client._oser.call_args_list)

def test_optional_memory_timeout_is_bounded_and_restored_on_usb_failure():
    client=client_for_readings()
    def fail(_):
        assert client.transport.timeout_ms==1000
        raise OSError(5,'USB I/O failed')
    with patch('station.read_header',side_effect=fail):
        with pytest.raises(OSError):live_archive_metadata(client,SERIAL)
    assert client.transport.timeout_ms==5000

def test_optional_memory_pointer_timeout_keeps_verified_header_and_does_not_read_at_bad_address():
    client=client_for_readings()
    def fail_pointer(_, info):
        assert client.transport.timeout_ms==1000
        raise TimeoutError('M timeout')
    with patch('station.read_header',return_value=(HEADER,deepcopy(INFO))),patch('station.read_data_pointer',side_effect=fail_pointer):
        info=live_archive_metadata(client,SERIAL)
    assert info['metadata_valid'] is True
    assert info['memory_health']=='pointer_invalid'
    assert info['deployment']==INFO['deployment']
    assert info['used_percent'] is None
    assert client.transport.timeout_ms==5000
    client._oser.assert_not_called()

def test_pointer_with_extra_bytes_is_not_silently_accepted():
    client=Mock()
    client._oser.return_value=Mock(payload=bytes.fromhex('0800000000'))
    with pytest.raises(HoboProtocolError):read_data_pointer(client,INFO)

def test_unverified_memory_blocks_native_write_previews(tmp_path):
    app=App(tmp_path,2)
    app.state.update(usb_connected=True,last_success_utc=datetime.now(timezone.utc).isoformat(),snapshot={
        'serial':SERIAL,'archive':{'metadata_valid':False},'sensors':[{'key':'PAR'}]})
    config={'interval':10,'start_mode':'immediate','memory_mode':'stop','sample_interval':0,'name':'Test'}
    with pytest.raises(HistoryConflict,match='部署資訊'):app.preview_deployment(config)


@pytest.mark.parametrize('memory_command',[0x45,0x4D])
def test_delayed_timed_out_memory_reply_cannot_poison_next_sensor_query(memory_command):
    transport=Mock(timeout_ms=1000)
    client=U30Client(transport)
    client.host_address=123
    transport.transact.side_effect=[TimeoutError('memory late'),Mock(payload=encode_oser(memory_command^255,b'\0\0\0\0'))]
    transport.read_frame.return_value=Mock(payload=encode_oser(0x99,bytes.fromhex('02ba46')))
    with pytest.raises(TimeoutError):client._oser(memory_command)
    assert client._oser(0x66).payload==bytes.fromhex('02ba46')
    assert transport.transact.call_count==2  # Never replay either query.
    transport.read_frame.assert_called_once_with(2,2,123)
    assert transport.timeout_ms==1000


def test_unexpected_reply_without_previous_memory_timeout_is_still_rejected():
    transport=Mock(timeout_ms=1000)
    client=U30Client(transport)
    client.host_address=123
    transport.transact.return_value=Mock(payload=encode_oser(0xB2,b'\0\0\0\0'))
    with pytest.raises(HoboProtocolError):client._oser(0x66)
    transport.read_frame.assert_not_called()


def test_late_memory_reply_cannot_extend_query_deadline():
    transport=Mock(timeout_ms=1000)
    client=U30Client(transport)
    client.host_address=123
    transport.transact.side_effect=TimeoutError('memory late')
    with pytest.raises(TimeoutError):client._oser(0x4D)
    transport.transact.side_effect=None
    transport.transact.return_value=Mock(payload=encode_oser(0xB2,b'\0\0\0\0'))
    with patch('hobo_u30.time.monotonic',side_effect=[100,102]):
        with pytest.raises(TimeoutError,match='deadline'):client._oser(0x66)
    transport.read_frame.assert_not_called()
    assert transport.timeout_ms==1000


def test_timed_out_memory_request_is_not_reissued_in_same_session():
    transport=Mock(timeout_ms=1000)
    client=U30Client(transport)
    client.host_address=123
    transport.transact.side_effect=TimeoutError('memory late')
    with pytest.raises(TimeoutError):client._oser(0x45,b'\0\0\0\0')
    with pytest.raises(HoboProtocolError,match='new session'):client._oser(0x45,b'\0\1\0\0')
    assert transport.transact.call_count==1
