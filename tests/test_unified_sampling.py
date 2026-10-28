import json
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import web_server
from web_server import App, Store
from history_tools import HistoryConflict
from pi_collection import utc

BASE = 1793167200


def snapshot(interval=14400, stamp=BASE):
    return {'serial': 'TEST', 'observed_utc': utc(stamp), 'logging': True, 'status_code': 1,
            'archive': {'serial': 'TEST', 'deployment': 22, 'launched_utc': utc(BASE-1),
                        'channels': [], 'logging_interval_seconds': interval, 'sampling_interval_seconds': 0},
            'sensors': [{'key': 'PAR', 'measurement': 'PAR', 'serial': 'S', 'raw': 1, 'value': 2, 'quality': 'ok'}]}


def channel(store):
    with store.connect() as db:
        store.channel(db, {'key': 'PAR', 'kind': 'sensor', 'serial': 'S', 'measurement': 'PAR'})


def test_one_interval_replaces_old_channel_rates_and_survives_restart(tmp_path):
    store = Store(tmp_path)
    channel(store)
    store.configure_collection({'channel': 'PAR', 'interval': 30, 'sample_interval': 10, 'statistics': ['mean'],
                                'start_mode': 'immediate', 'enabled': True}, BASE)
    store.unify_sampling(14400, 'TEST', 22, BASE)
    app = App(tmp_path, 2)  # The old service command must not restore 2 s polling.
    assert app.interval == app.store.collection_interval(2) == 14400
    cfg = app.store.collection_status()['channels']['PAR']['config']
    assert cfg['sample_interval'] == 14400 and cfg['interval'] == 43200 and cfg['aggregate_count'] == 3
    cfg = app.store.configure_collection({'channel': 'PAR', 'interval': 2, 'sample_interval': 1,
        'aggregate_count': 6, 'statistics': ['mean'], 'start_mode': 'immediate', 'enabled': True}, BASE)
    assert cfg['sample_interval'] == 14400 and cfg['interval'] == 86400
    assert app.store.collection_interval(2) == 14400
    assert app.store.collection_status()['channels']['PAR']['runtime']['n'] == 0


def test_adopting_existing_u30_interval_keeps_existing_rows(tmp_path):
    store = Store(tmp_path)
    store.save_snapshot(snapshot(10))
    app = App(tmp_path, 2)
    assert app.interval == 10
    with app.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 1
    assert app.store.sampling_config()['serial'] == 'TEST'


def clock_for(monkeypatch):
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(web_server, 'time', SimpleNamespace(
        time=lambda: BASE+clock.value, monotonic=lambda: clock.value))
    return clock


def test_four_hour_wait_status_requests_and_channel_changes_never_poll_usb(tmp_path, monkeypatch):
    clock = clock_for(monkeypatch)
    app = App(tmp_path, 2)
    app.activate_sampling(14400, 'TEST', 22)
    calls = []
    def read():
        calls.append(clock.value)
        if len(calls) == 2:
            app.stop.set()
        return snapshot(stamp=BASE+clock.value)
    app.reader = Mock(read=read)
    waits = []
    class Wake:
        def clear(self): pass
        def wait(self, delay):
            waits.append(delay)
            if len(waits) == 1:
                # Browser HTTP status refreshes read SQLite only.
                for _ in range(30):
                    assert app.status()['waiting_for_query']
                app.store.configure_collection({'channel': 'PAR', 'enabled': True, 'statistics': [],
                                                'start_mode': 'immediate', 'interval': 2}, BASE)
                clock.value += 300  # A spurious wake is not a measurement.
                assert not app.status()['stale']
                assert not app.status()['device_state_fresh']
            else:
                clock.value += delay
    app.wake = Wake()
    app.run()
    assert calls == [0, 14400]
    assert waits == [14400, 14100]
    with app.store.connect() as db:
        rows = db.execute('SELECT quality FROM samples ORDER BY timestamp_utc').fetchall()
        assert len(rows) == 2 and all('interval=14400' in r[0] for r in rows)


def test_failed_queries_retry_at_selected_interval_not_every_two_seconds(tmp_path, monkeypatch):
    clock_for(monkeypatch)
    app = App(tmp_path, 2)
    app.activate_sampling(14400, 'TEST', 22)
    app.reader = Mock()
    app.reader.read.side_effect = OSError('USB disconnected')
    delays = []
    class Wake:
        def clear(self): pass
        def wait(self, delay):
            delays.append(delay)
            app.stop.set()
    app.wake = Wake()
    app.run()
    assert app.reader.read.call_count == 1 and delays == [14400]
    assert not app.status()['usb_connected'] and 'USB disconnected' in app.state['error']


def test_manual_refresh_is_an_explicit_extra_acquisition(tmp_path):
    app = App(tmp_path, 2)
    app.activate_sampling(14400, 'TEST', 22)
    read_once, read_twice = threading.Event(), threading.Event()
    count = []
    def read():
        count.append(1)
        (read_once if len(count) == 1 else read_twice).set()
        return snapshot(stamp=BASE+len(count))
    app.reader = Mock(read=read)
    worker = threading.Thread(target=app.run)
    worker.start()
    try:
        assert read_once.wait(2)
        app.request('refresh')
        assert read_twice.wait(2)
        assert len(count) == 2
    finally:
        app.stop.set()
        app.wake.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize('failure', [False, True])
def test_pi_changes_only_after_verified_native_write(tmp_path, monkeypatch, failure):
    clock_for(monkeypatch)
    app = App(tmp_path, 2)
    app.activate_sampling(10, 'TEST', 21)
    native = {'interval': 14400, 'sample_interval': 0, 'memory_mode': 'stop',
              'start_mode': 'immediate', 'name': 'Test'}
    app.deployment_requested = {'configuration': native, 'serial': 'TEST', 'channels': ['PAR'], 'current_deployment': 21}
    def apply(*args):
        assert app.interval == 10 and app.store.sampling_config()['interval_seconds'] == 10
        if failure:
            raise ValueError('invalid pointer')
        return {'configuration': native, 'serial': 'TEST', 'archive': {'deployment': 22}, 'backup': 'verified.bin'}
    monkeypatch.setattr(web_server, 'apply_deployment', apply)
    app.store.import_archive = Mock()
    def read():
        app.stop.set()
        return snapshot(10 if failure else 14400)
    app.reader = Mock(read=read)
    app.run()
    assert app.interval == app.store.sampling_config()['interval_seconds'] == (10 if failure else 14400)
    assert bool(app.state['deployment'].get('error')) is failure
    assert app.store.import_archive.call_count == (0 if failure else 1)


def test_invalid_pointer_can_preview_but_requires_explicit_raw_backup_confirmation(tmp_path, monkeypatch):
    clock_for(monkeypatch)
    app = App(tmp_path, 2)
    app.activate_sampling(10, 'TEST', 21)
    data = snapshot(10)
    data['status_code'] = 0
    data['archive']['pointer_valid'] = False
    app.state.update(usb_connected=True, snapshot=data, last_success_utc=utc(BASE))
    plan = app.preview_deployment({'interval': 14400, 'sample_interval': 0, 'start_mode': 'immediate',
                                   'memory_mode': 'stop', 'name': 'Test'})
    assert plan['raw_backup_required'] and '兩遍' in plan['warning']
    with pytest.raises(ValueError, match='舊歷史範圍'):
        app.queue_deployment(plan['token'], 'TEST')
    assert app.interval == 10 and app.deployment_requested is None
    app.queue_deployment(plan['token'], 'TEST', True)
    assert app.deployment_requested['raw_recovery_confirmed'] is True and app.interval == 10


def test_statistical_windows_use_only_scheduled_samples(tmp_path):
    store = Store(tmp_path)
    channel(store)
    store.unify_sampling(14400, 'TEST', 22, BASE)
    store.configure_collection({'channel': 'PAR', 'enabled': True, 'statistics': ['mean'],
                                'start_mode': 'immediate', 'aggregate_count': 6}, BASE)
    for index in range(7):
        data = snapshot(stamp=BASE+index*14400)
        data['sensors'][0]['value'] = index
        store.save_snapshot(data)
    with store.connect() as db:
        rows = db.execute('SELECT channel, value, quality FROM samples').fetchall()
    assert len(rows) == 1 and rows[0]['channel'] == 'PAR:stat:mean'
    assert rows[0]['value'] == 2.5 and 'statistics_n=6;expected=6' in rows[0]['quality']
