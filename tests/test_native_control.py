import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from web_server import App
from history_tools import HistoryConflict


def snapshot(keys=('PAR',), deployed=('PAR',), code=0):
    return {'serial': '22005319', 'name': 'Test', 'observed_utc': datetime.now(timezone.utc).isoformat(),
            'logging': code == 1, 'status_code': code,
            'archive': {'serial': '22005319', 'name': 'Test', 'deployment': 23,
                        'launched_utc': '2026-10-28T13:00:00+00:00', 'wrap_enabled': True,
                        'logging_interval_seconds': 10, 'sampling_interval_seconds': 0,
                        'pointer_valid': True, 'metadata_valid': True,
                        'channels': [{'key': key} for key in deployed]},
            'sensors': [{'key': key, 'quality': 'ok', 'measurement': key,
                         'raw': 1, 'value': 2, 'serial': key} for key in keys]}


def fresh(app, data):
    app.state.update(snapshot=data, usb_connected=True, last_success_utc=data['observed_utc'])


def test_pause_is_serialized_without_backup_or_launch_and_pi_keeps_saving(tmp_path):
    app = App(tmp_path, 10)
    data = snapshot(code=1)
    fresh(app, data)
    app.queue_native('pause', data['serial'])
    assert app.device_busy()
    with pytest.raises(HistoryConflict):
        app.queue_native('pause', data['serial'])
    job = app.native_requested
    app.native_requested = None
    app.stop_native = Mock()
    with patch('native_control.apply_deployment') as write:
        app.execute_native(job)
    app.stop_native.assert_called_once_with(job)
    write.assert_not_called()
    assert app.native_policy['paused']
    app.reader.read = Mock(return_value=snapshot(code=0))
    app.poll_once()
    assert app.status()['usb_connected'] and not app.state['deployment']['running']
    with app.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM samples WHERE source='live'").fetchone()[0] == 1


def test_added_sensors_need_two_matching_observations_and_do_not_launch_in_poll(tmp_path):
    app = App(tmp_path, 10)
    data = snapshot(keys=('PAR', 'Temperature', 'RH'))
    with patch('native_control.apply_deployment') as write:
        app.observe_native(data)
        assert app.native_requested is None
        app.observe_native(deepcopy(data))
        assert app.native_requested['action'] == 'auto'
        assert app.native_requested['channels'] == ['PAR', 'RH', 'Temperature']
        write.assert_not_called()
    assert app.native_policy['blocked']
    restarted = App(tmp_path, 10)
    restarted.observe_native(data)
    restarted.observe_native(data)
    assert restarted.native_requested is None  # Never replay an interrupted launch.


@pytest.mark.parametrize('mutate', [
    lambda s: s['archive'].update(pointer_valid=False),
    lambda s: s['archive'].update(metadata_valid=False),
    lambda s: s['sensors'][0].update(quality='error'),
    lambda s: s['archive'].update(channels=[{'key': 'PAR'}, {'key': 'Removed'}]),
    lambda s: s.update(status_code=5),
])
def test_uncertain_reads_removals_and_pending_starts_never_auto_redeploy(tmp_path, mutate):
    app = App(tmp_path, 10)
    data = snapshot(keys=('PAR', 'RH'))
    mutate(data)
    app.observe_native(data)
    app.observe_native(data)
    assert app.native_requested is None


def test_disabled_policy_survives_restart_and_no_change_does_not_restart_logger(tmp_path):
    app = App(tmp_path, 10)
    app.queue_native('policy', enabled=False)
    app = App(tmp_path, 10)
    for _ in range(3):
        app.observe_native(snapshot(keys=('PAR', 'RH')))
    assert app.native_requested is None
    app.queue_native('policy', enabled=True)
    for _ in range(3):
        app.observe_native(snapshot())
    assert app.native_requested is None


def test_unstable_added_channel_set_requires_new_pair(tmp_path):
    app = App(tmp_path, 10)
    app.observe_native(snapshot(keys=('PAR', 'Temperature')))
    app.observe_native(snapshot(keys=('PAR', 'Temperature', 'RH')))
    assert app.native_requested is None
    app.observe_native(snapshot(keys=('PAR', 'Temperature', 'RH')))
    assert app.native_requested


def test_reconnecting_sensor_already_in_old_header_auto_deploys_after_explicit_pause(tmp_path):
    app = App(tmp_path, 10)
    disconnected = snapshot(keys=('PAR',), deployed=('PAR', 'RH'), code=1)
    app.stop_native = Mock()
    app.execute_native(app.native_job('pause', disconnected))
    app.observe_native(snapshot(keys=('PAR',), deployed=('PAR', 'RH')))
    assert app.native_requested is None
    reconnected = snapshot(keys=('PAR', 'RH'), deployed=('PAR', 'RH'))
    app.observe_native(reconnected)
    assert app.native_requested is None
    app.observe_native(reconnected)
    assert app.native_requested['action'] == 'auto'


def test_paused_baseline_survives_restart_but_cannot_affect_a_different_deployment(tmp_path):
    app = App(tmp_path, 10)
    app.stop_native = Mock()
    app.execute_native(app.native_job('pause', snapshot(keys=('PAR',), deployed=('PAR', 'RH'), code=1)))
    app = App(tmp_path, 10)
    connected = snapshot(keys=('PAR', 'RH'), deployed=('PAR', 'RH'))
    connected['archive']['deployment'] = 24
    app.observe_native(connected)
    app.observe_native(connected)
    assert app.native_requested is None


def test_pause_with_no_sensors_can_enroll_first_connected_sensor(tmp_path):
    app = App(tmp_path, 10)
    app.stop_native = Mock()
    app.execute_native(app.native_job('pause', snapshot(keys=(), deployed=('PAR',), code=1)))
    app.observe_native(snapshot())
    app.observe_native(snapshot())
    assert app.native_requested['channels'] == ['PAR']


@pytest.mark.parametrize('failure', ['backup', 'sensor_changed', 'device_changed'])
def test_failed_auto_job_does_not_replay_or_break_pi_collection(tmp_path, failure):
    app = App(tmp_path, 10)
    data = snapshot(keys=('PAR', 'RH'))
    job = app.native_job('auto', data)
    app.stop_native = Mock()
    changed = deepcopy(data)
    if failure == 'sensor_changed':
        changed['sensors'].pop()
    if failure == 'device_changed':
        changed['serial'] = 'OTHER'
    app.reader.read = Mock(side_effect=[changed, data])
    with patch('native_control.apply_deployment', side_effect=OSError('backup failed')) as write:
        app.execute_native(job)
    if failure != 'backup':
        write.assert_not_called()
        app.stop_native.assert_not_called()
    assert app.native_policy['blocked'] and app.state['deployment']['error']
    assert not app.state['deployment']['running'] and not app.state['sync']['running']
    app.reader.read = Mock(return_value=data)
    app.poll_once()
    app.poll_once()
    assert app.status()['usb_connected'] and app.native_requested is None
    with app.store.connect() as db:
        assert db.execute("SELECT COUNT(DISTINCT channel) FROM samples WHERE source='live'").fetchone()[0] == 2


def test_success_uses_current_interval_and_backup_path_and_verifies_before_pi_change(tmp_path):
    app = App(tmp_path, 10)
    data = snapshot(keys=('PAR', 'Temperature', 'RH'))
    app.reader.read = Mock(return_value=data)
    app.stop_native = Mock()
    app.store.import_archive = Mock()
    result = {'archive': {'deployment': 24}, 'backup': 'fresh.bin'}
    with patch('native_control.apply_deployment', return_value=result) as write:
        app.execute_native(app.native_job('auto', data))
    args = write.call_args.args
    assert args[0] == app.store.directory / 'backups'
    assert args[1]['interval'] == 10 and args[1]['memory_mode'] == 'wrap'
    assert args[1]['start_mode'] == 'immediate'
    assert args[4:] == (['PAR', 'RH', 'Temperature'], 23)
    assert not app.native_policy['blocked'] and not app.native_policy['paused']
    assert app.store.sampling_config()['deployment'] == 24


def test_resume_refuses_stale_wrong_device_and_running_state(tmp_path):
    app = App(tmp_path, 10)
    data = snapshot(code=0)
    fresh(app, data)
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', 'OTHER')
    fresh(app, snapshot(code=1))
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', data['serial'])
    fresh(app, data)
    app.state['last_success_utc'] = '2000-01-01T00:00:00+00:00'
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', data['serial'])


def test_resume_aborts_if_state_changed_after_click(tmp_path):
    app = App(tmp_path, 10)
    stopped = snapshot()
    app.reader.read = Mock(return_value=snapshot(code=1))
    app.stop_native = Mock()
    app.execute_native(app.native_job('resume', stopped))
    app.stop_native.assert_not_called()
    assert app.native_policy['blocked']


def test_resume_confirmation_binds_deployment_and_exact_channel_list(tmp_path):
    app = App(tmp_path, 10)
    fresh(app, snapshot())
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', '22005319')
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', '22005319', expected_deployment=22, expected_channels=['PAR'])
    with pytest.raises(HistoryConflict):
        app.queue_native('resume', '22005319', expected_deployment=23, expected_channels=['OTHER'])
    app.queue_native('resume', '22005319', expected_deployment=23, expected_channels=['PAR'])
    assert app.native_requested['action'] == 'resume'


def test_optional_automation_error_does_not_mark_working_usb_disconnected(tmp_path):
    app = App(tmp_path, 10)
    app.reader.read = Mock(return_value=snapshot())
    app.observe_native = Mock(side_effect=OSError('policy storage unavailable'))
    app.poll_once()
    assert app.state['usb_connected'] and app.state['error'] is None
    assert app.native_policy['blocked']


def test_error_persist_failure_cannot_escape_worker_and_stop_polling(tmp_path):
    app = App(tmp_path, 10)
    app.stop_native = Mock(side_effect=OSError('USB stop failed'))
    app.save_native_policy = Mock(side_effect=OSError('policy storage failed'))
    app.execute_native(app.native_job('pause', snapshot(code=1)))
    assert not app.state['deployment']['running']
    assert app.native_policy['blocked']
    app.reader.read = Mock(return_value=snapshot())
    app.poll_once()
    assert app.state['usb_connected']


def test_native_control_http_requires_auth_confirmation_and_same_origin(http_app):
    app, request = http_app
    fresh(app, snapshot(code=1))
    body = {'action': 'pause', 'serial': '22005319', 'confirmed': True}
    assert request('/api/u30/control', body)[0] == 403
    body['setup_key'] = 'test-only-key'
    assert request('/api/u30/control', body, origin='http://evil.invalid')[0] == 403
    assert request('/api/u30/control', {**body, 'confirmed': False})[0] == 400
    assert request('/api/u30/control', body)[0] == 202
    assert app.native_requested['action'] == 'pause'
    assert request('/api/u30/control', body)[0] == 409


def test_stop_validates_physical_identity_without_reading_bad_memory(tmp_path):
    from contextlib import nullcontext
    app = App(tmp_path, 10)
    client = Mock(host_address=123)
    system = bytearray(29)
    system[13:17] = (22005319).to_bytes(4, 'big')
    client.transport.transact.return_value.payload = bytes(system)
    with patch('native_control.writer_session', return_value=nullcontext(client)), patch('native_control.read_header') as header:
        app.stop_native(app.native_job('pause', snapshot(code=1)))
    client.stop_logging.assert_called_once()
    header.assert_not_called()  # A bad memory header cannot prevent pausing.
    client.command.assert_not_called()  # No launch/erase/parameter writes.
    system[13:17] = (123).to_bytes(4, 'big')
    client.transport.transact.return_value.payload = bytes(system)
    client.stop_logging.reset_mock()
    with patch('native_control.writer_session', return_value=nullcontext(client)):
        with pytest.raises(HistoryConflict):
            app.stop_native(app.native_job('pause', snapshot(code=1)))
    client.stop_logging.assert_not_called()


# Reuse the real HTTP server/auth fixture rather than bypassing Handler checks.
from test_history_tools import http_app
