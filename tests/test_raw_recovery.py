import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from hobo_u30 import HoboProtocolError
from station import backup
from u30_data import parse_memory_header
from u30_settings import apply_deployment
from web_server import Store

HEADER = (Path(__file__).parent/'fixtures/header.bin').read_bytes()
INFO = parse_memory_header(HEADER)
IMAGE = HEADER+bytes([0xA5])*(524288-len(HEADER))
BAD = bytes.fromhex('0b78af00')


def capture(path, pointers=(BAD, BAD), statuses=(b'\0', b'\0', b'\0'), mismatch=None, failure=None, headers=None):
    client = Mock()
    pointers, statuses = iter(pointers), iter(statuses)
    client._oser.side_effect = lambda command: Mock(payload=next(pointers) if command == 0x4D else next(statuses))
    reads = []
    def memory(_, address, count):
        assert 0 <= address < 524288 and 1 <= count <= 256 and address+count <= 524288
        reads.append(address)
        if failure == address and len(reads) <= 2048:
            raise HoboProtocolError('USB CRC failure')
        if len(reads) > 2048 and address == mismatch:
            return bytes(count)
        return IMAGE[address:address+count]
    progress = Mock()
    with patch('station.session', return_value=nullcontext(client)), \
         patch('station.read_header', side_effect=headers or [(HEADER, dict(INFO))]*3), \
         patch('station.read_memory', side_effect=memory):
        result = backup(path, progress)
    return result, reads, progress


@pytest.mark.parametrize('pointers', [(BAD, BAD), (bytes.fromhex('00050300'), BAD), (BAD, bytes.fromhex('00050300')),
                                     (b'', BAD)])
def test_bad_eof_keeps_complete_bounded_image_and_verifies_twice_without_decoding(tmp_path, pointers):
    (path, manifest), reads, progress = capture(tmp_path/'backups', pointers=pointers)
    assert path.read_bytes() == IMAGE and len(reads) == 4096
    assert reads[:2048] == reads[2048:]
    assert manifest['complete_image'] and manifest['recovery_verified'] and manifest['memory_verified_twice']
    assert not manifest['consistent_snapshot'] and manifest['snapshot_bounds'] is None
    assert manifest['sha256'] == hashlib.sha256(IMAGE).hexdigest()
    assert manifest['pointer_errors'] and json.loads(path.with_suffix('.json').read_text())['recovery_verified']
    progress.assert_called_with(1048576, 1048576)
    store = Store(tmp_path/'db')
    with patch('web_server.iter_archive') as decoder:
        result = store.import_archive(path)
        decoder.assert_not_called()
    assert not result['decoded'] and result['rows'] == 0 and result['recovery_verified']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 0


@pytest.mark.parametrize('statuses', [(b'\1', b'\1', b'\1'), (b'\0', b'\0', b'\1')])
def test_running_or_changed_state_never_authorizes_recovery(tmp_path, statuses):
    (path, manifest), reads, _ = capture(tmp_path, statuses=statuses)
    assert path.read_bytes() == IMAGE and manifest['raw_recovery']
    assert not manifest['recovery_verified'] and not manifest['consistent_snapshot']
    assert manifest['verification_error']


def test_second_pass_mismatch_preserves_first_image_but_blocks_relaunch(tmp_path):
    (path, manifest), reads, _ = capture(tmp_path, mismatch=256)
    assert path.read_bytes() == IMAGE and len(reads) == 2050
    assert not manifest['memory_verified_twice'] and '256' in manifest['verification_error']
    config = {'interval': 14400, 'sample_interval': 0, 'memory_mode': 'stop', 'start_mode': 'immediate', 'name': 'Test'}
    with patch('station.backup', return_value=(path, manifest)), patch('u30_settings.writer_session') as writer:
        with pytest.raises(HoboProtocolError, match='兩次'):
            apply_deployment(tmp_path, config, INFO['serial'], Mock(), allow_raw_recovery=True)
        writer.assert_not_called()


def test_first_pass_crc_failure_never_becomes_a_complete_backup(tmp_path):
    with pytest.raises(HoboProtocolError, match='CRC'):
        capture(tmp_path, failure=256)
    assert list(tmp_path.glob('*.part')) and not list(tmp_path.glob('*.bin'))


def test_transport_failure_is_not_misclassified_as_an_invalid_pointer(tmp_path):
    client = Mock()
    client._oser.side_effect = [Mock(payload=b'\0'), HoboProtocolError('bad response CRC')]
    with patch('station.session', return_value=nullcontext(client)), patch('station.read_header', return_value=(HEADER, INFO)), \
         patch('station.read_memory') as read:
        with pytest.raises(HoboProtocolError, match='CRC'):
            backup(tmp_path)
        read.assert_not_called()


def test_recovery_relaunch_requires_ack_then_performs_normal_bounded_write_verification(tmp_path):
    (path, manifest), _, _ = capture(tmp_path)
    config = {'interval': 14400, 'sample_interval': 0, 'memory_mode': 'stop', 'start_mode': 'immediate', 'name': 'Test'}
    client = Mock()
    client.attached_instruments.return_value = []
    client._oser.return_value = Mock(payload=b'\1')
    chunks = {}
    def write(op, payload=b''):
        if op == 0x42:
            chunks[int.from_bytes(payload[:3], 'big')] = payload[4:]
    client.command.side_effect = write
    after = {**INFO, 'logging_interval_seconds': 14400, 'sampling_interval_seconds': 0, 'wrap_enabled': False}
    with patch('station.backup', return_value=(path, manifest)), patch('u30_settings.writer_session', return_value=nullcontext(client)) as writer, \
         patch('station.read_header', side_effect=[(HEADER, INFO), (HEADER, after)]), \
         patch('station.read_memory', side_effect=lambda c,a,n: chunks[a]), patch('u30_settings.time.sleep'):
        with pytest.raises(HoboProtocolError, match='確認'):
            apply_deployment(tmp_path, config, INFO['serial'], Mock())
        writer.assert_not_called()
        result = apply_deployment(tmp_path, config, INFO['serial'], Mock(), allow_raw_recovery=True)
    assert result['phase'] == 'verified' and result['raw_recovery']
    assert not result['historical_bounds_verified'] and path.read_bytes() == IMAGE
    assert client.command.call_args_list[-1].args == (0x46, b'\0')


def test_same_image_can_upgrade_from_unverified_to_verified_raw_backup(tmp_path):
    (path, manifest), _, _ = capture(tmp_path/'backups')
    original = dict(manifest)
    manifest['recovery_verified'] = False
    path.with_suffix('.json').write_text(json.dumps(manifest))
    store = Store(tmp_path/'db')
    assert not store.import_archive(path)['recovery_verified']
    path.with_suffix('.json').write_text(json.dumps(original))
    assert store.import_archive(path)['recovery_verified']
    assert len(store.backups()) == 1


def test_header_change_during_two_passes_cannot_authorize_relaunch(tmp_path):
    changed = bytearray(HEADER)
    changed[90] ^= 1
    changed = bytes(changed)
    (path, manifest), _, _ = capture(tmp_path, headers=[(HEADER, INFO), (changed, INFO), (changed, INFO)])
    assert path.read_bytes() == IMAGE and not manifest['stable_header']
    assert not manifest['recovery_verified']


def test_raw_verification_claim_with_wrong_hash_is_not_displayed_as_verified(tmp_path):
    (path, manifest), _, _ = capture(tmp_path/'backups')
    manifest['sha256'] = '0'*64
    path.with_suffix('.json').write_text(json.dumps(manifest))
    assert not Store(tmp_path/'db').import_archive(path)['recovery_verified']
