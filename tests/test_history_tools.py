import csv
import io
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
import web_server
from history_tools import HistoryConflict
from web_server import Store, App, Handler


def populate(store):
    rows = [("dev", "launch", "live", "PAR", f"2024-01-01T00:00:{second:02d}+00:00", i, value, "ok" if value is not None else "missing")
            for i, (second, value) in enumerate(zip((0, 2, 4, 6, 8, 10, 30, 32, 59), (90, 110, 120, 80, 130, None, 140, 150, 160)))]
    with store.connect() as db:
        store.insert_samples(db, rows)
        store.insert_samples(db, [("dev", "launch", "live", "TEMP", "2024-01-01T00:00:02+00:00", 20, 20, "ok")])
    return rows


def selection(**extra):
    return {k: [v] for k, v in {"source": "live", "channel": "PAR", "start": "2024-01-01T08:00:00+08:00",
                               "end": "2024-01-01T08:01:00+08:00", **extra}.items()}


def test_threshold_intervals_split_at_nonmatch_null_and_outage(tmp_path):
    store = Store(tmp_path)
    populate(store)
    result = store.history(selection(value_op="gt", value="100"))
    assert result["total"] == 6 and result["interval_count"] == 4
    events = list(reversed(result["intervals"]))
    assert [r["samples"] for r in events] == [2, 1, 2, 1]
    assert events[0]["start_utc"].endswith("00:00:02+00:00")
    assert events[0]["end_utc"].endswith("00:00:04+00:00")
    assert result["rows"][0]["segment"] == result["rows"][1]["segment"]
    assert result["rows"][1]["segment"] != result["rows"][2]["segment"]
    for op, value, expected in (("gte", "110", 6), ("lt", "100", 2), ("lte", "110", 3)):
        assert store.history(selection(value_op=op, value=value))["total"] == expected
    assert store.history(selection(value_op="between", value="110", value_max="130"))["total"] == 3


def test_archive_intervals_use_deployment_logging_interval(tmp_path):
    store = Store(tmp_path)
    info = {"deployment": 1, "launched_utc": "2024-01-01T00:00:00+00:00", "logging_interval_seconds": 600}
    with store.connect() as db:
        db.execute("INSERT INTO backups VALUES ('test', ?)", (json.dumps({"archive": info}),))
        store.insert_samples(db, [("dev", store.deployment(info), "archive", "PAR", t, 1, 101, "ok")
                                  for t in ("2024-01-01T00:00:00+00:00", "2024-01-01T00:10:00+00:00", "2024-01-01T01:00:00+00:00")])
    result = store.history({"source": ["archive"], "channel": ["PAR"], "value_op": ["gt"], "value": ["100"]})
    assert result["total"] == 3 and result["interval_count"] == 2


@pytest.mark.parametrize("extra", [dict(value_op="gt", value="nan"), dict(value_op="lt", value="inf"),
                                  dict(value_op="between", value="5", value_max="2"), dict(value_op="DROP", value="5"),
                                  dict(channel="all", value_op="gt", value="1"), dict(end="2023-01-01"), dict(start="2024-01-01T00:00:00")])
def test_invalid_queries_fail_closed(tmp_path, extra):
    with pytest.raises(ValueError):
        Store(tmp_path).history(selection(**extra))


def test_recoverable_delete_is_exact_and_survives_reimport_and_restart(tmp_path):
    store = Store(tmp_path)
    rows = populate(store)
    query = selection(value_op="gt", value="100", end="2024-01-01T08:00:04+08:00")
    preview = store.preview_delete(query)
    assert preview["count"] == 2
    batch = store.trash_samples(preview)
    assert store.history(selection())["total"] == 7
    with store.connect() as db:
        store.insert_samples(db, rows)  # Downloading another backup cannot resurrect deleted rows.
    reopened = Store(tmp_path)
    assert reopened.history(selection())["total"] == 7
    assert reopened.history(selection(channel="TEMP"))["total"] == 1
    assert reopened.restore_samples(batch["id"])["restored"] == 2
    assert reopened.history(selection())["total"] == 9
    assert reopened.restore_samples(batch["id"])["already_restored"]
    assert reopened.trash()[0]["restored_utc"]


def test_preview_must_be_bounded_and_cannot_delete_changed_data(tmp_path):
    store = Store(tmp_path)
    populate(store)
    for key in ("start", "end", "source"):
        query = selection()
        del query[key]
        with pytest.raises(ValueError):
            store.preview_delete(query)
    preview = store.preview_delete(selection())
    with store.connect() as db:
        store.insert_samples(db, [("dev", "launch", "live", "PAR", "2024-01-01T00:00:03+00:00", 1, 200, "ok")])
    with pytest.raises(HistoryConflict):
        store.trash_samples(preview)
    assert store.history(selection())["total"] == 10
    assert not store.trash()


def test_all_channel_delete_respects_source_and_restore_preserves_other_source(tmp_path):
    store = Store(tmp_path)
    populate(store)
    with store.connect() as db:
        store.insert_samples(db, [("dev", "launch", "archive", "PAR", "2024-01-01T00:00:02+00:00", 1, 200, "ok")])
    query = selection()
    del query["channel"]
    plan = store.preview_delete(query)
    assert plan["count"] == 10 and len(plan["channels"]) == 2
    batch = store.trash_samples(plan)
    with store.connect() as db:
        assert dict(db.execute("SELECT source,COUNT(*) FROM samples GROUP BY source")) == {"archive": 1}
    assert store.restore_samples(batch["id"])["restored"] == 10
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 11


def test_preview_freezes_future_end_and_leaves_new_readings_alone(tmp_path):
    from datetime import datetime, timezone, timedelta
    store = Store(tmp_path)
    populate(store)
    plan = store.preview_delete(selection(end="2999-01-01T00:00:00+00:00"))
    assert datetime.fromisoformat(plan["query"]["end"][0]) < datetime.now(timezone.utc)
    future = (datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat(timespec="seconds")
    with store.connect() as db:
        store.insert_samples(db, [("dev", "launch", "live", "PAR", future, 9, 900, "ok")])
    store.trash_samples(plan)
    with store.connect() as db:
        assert db.execute("SELECT value FROM samples WHERE channel='PAR'").fetchall()[0][0] == 900


def test_trash_pagination_keeps_old_batches_recoverable(tmp_path):
    store = Store(tmp_path)
    for day in range(1, 32):
        date = f"2024-01-{day:02d}"
        with store.connect() as db:
            store.insert_samples(db, [("dev", "launch", "live", "PAR", date+"T00:00:00+00:00", 1, 100, "ok")])
        store.trash_samples(store.preview_delete(selection(start=date, end=date)))
    assert store.trash_count() == 31
    assert len(store.trash()) == 30
    assert len(store.trash(30)) == 1
    old = store.trash(30)[0]
    assert old["query"]["start"] == ["2023-12-31T16:00:00+00:00"]  # Taipei midnight.
    assert store.restore_samples(old["id"])["restored"] == 1


def test_deletion_transaction_rolls_back_if_copy_or_delete_fails(tmp_path, monkeypatch):
    store = Store(tmp_path)
    populate(store)
    preview = store.preview_delete(selection())
    class FaultConnection(sqlite3.Connection):
        def execute(self, statement, *args):
            if statement.startswith("DELETE FROM samples"):
                raise sqlite3.OperationalError("simulated disk failure")
            return super().execute(statement, *args)
    def faulty():
        db = sqlite3.connect(store.path, factory=FaultConnection)
        db.row_factory = sqlite3.Row
        return db
    with monkeypatch.context() as context:
        context.setattr(store, "connect", faulty)
        with pytest.raises(sqlite3.OperationalError):
            store.trash_samples(preview)
    assert store.history(selection())["total"] == 9
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deleted_samples").fetchone()[0] == 0


@pytest.fixture
def http_app(tmp_path, monkeypatch):
    app = App(tmp_path, 2)
    populate(app.store)
    def authorize(action="status", data=None):
        return ({"authorized": True}, 200) if data and data.get("setup_key") == "test-only-key" else ({"error": "wrong key"}, 403)
    monkeypatch.setattr(web_server, "network_request", authorize)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(path, body=None, origin=None):
        request = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "Origin": origin or base})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = response.read()
                return response.status, data.decode("utf-8-sig") if path.startswith("/api/export") else json.loads(data)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    yield app, request
    server.shutdown()
    server.server_close()
    thread.join()


def test_http_confirmation_auth_tokens_csv_and_restore(http_app):
    app, request = http_app
    from urllib.parse import urlencode
    query = selection(value_op="gt", value="100")
    status, exported = request("/api/export.csv?"+urlencode(query, doseq=True))
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert status == 200 and len(rows) == 6 and all(float(r["value"]) > 100 for r in rows)
    assert "timestamp_utc" not in rows[0]
    assert rows[0]["timestamp_gmt8"] == "2024-01-01T08:00:02+08:00"
    assert request("/api/data/delete-preview", {"filters": query}, origin="http://evil.invalid")[0] == 403
    status, plan = request("/api/data/delete-preview", {"filters": query})
    assert status == 200 and plan["count"] == 6 and "signature" not in plan
    body = {"token": plan["token"], "confirmed": True, "setup_key": "wrong"}
    assert request("/api/data/trash", body)[0] == 403
    body["setup_key"] = "test-only-key"
    body["confirmed"] = False
    assert request("/api/data/trash", body)[0] == 400
    body["confirmed"] = True
    status, batch = request("/api/data/trash", body)
    assert status == 200 and batch["count"] == 6
    assert request("/api/data/trash", body)[0] == 409  # Tokens are single use.
    assert request("/api/data/restore", {"batch": batch["id"], "setup_key": "test-only-key"})[1]["restored"] == 6
    _, plan = request("/api/data/delete-preview", {"filters": query})
    created, value = app.deletion_previews[plan["token"]]
    app.deletion_previews[plan["token"]] = (time.monotonic()-301, value)
    assert request("/api/data/trash", {**body, "token": plan["token"]})[0] == 409


def test_settings_http_auth_confirmation_and_device_job_exclusion(http_app):
    app, request = http_app
    with app.store.connect() as db:
        app.store.channel(db, {"key": "PAR", "kind": "sensor", "measurement": "PAR", "serial": "TEST"})
    body = {"channel": "PAR", "enabled": True, "interval": 900, "sample_interval": 10,
            "statistics": ["mean"], "start_mode": "button"}
    assert request("/api/collection/settings", body)[0] == 403
    body["setup_key"] = "test-only-key"
    assert request("/api/collection/settings", body, origin="http://evil.invalid")[0] == 403
    assert request("/api/collection/settings", body)[0] == 200
    assert request("/api/collection/start", {"channel": "PAR", "setup_key": "test-only-key"})[0] == 200
    assert request("/api/collection/start", {"channel": "PAR", "setup_key": "test-only-key"})[0] == 400
    config = {"name": "Test", "interval": 600, "sample_interval": 0, "start_mode": "immediate", "memory_mode": "stop"}
    assert request("/api/u30/preview", {"configuration": config})[0] == 409
    app.state.update(usb_connected=True, last_success_utc=web_server.now(),
                     snapshot={"serial": "22005319", "archive": {"deployment": 20}, "sensors": [{"key": "PAR"}]})
    status, plan = request("/api/u30/preview", {"configuration": config})
    assert status == 200 and not app.deployment_requested
    apply = {"token": plan["token"], "serial": "22005319", "confirmed": True, "setup_key": "test-only-key"}
    assert request("/api/u30/apply", {**apply, "confirmed": False})[0] == 400
    assert request("/api/u30/apply", {**apply, "serial": "other"})[0] == 400
    assert request("/api/u30/apply", {**apply, "setup_key": "wrong"})[0] == 403
    assert request("/api/u30/apply", apply)[0] == 202
    assert not app.request("sync")
    assert request("/api/u30/apply", apply)[0] == 409
    assert request("/api/readout", {"mode": "stop", "setup_key": "test-only-key"})[0] == 400
    assert request("/api/readout", {"mode": "continue", "setup_key": "test-only-key"})[0] == 409


def test_raw_recovery_http_keeps_auth_and_requires_the_previewed_acknowledgement(http_app):
    app, request = http_app
    app.state.update(usb_connected=True, last_success_utc=web_server.now(),
                     snapshot={"serial": "22005319", "status_code": 0,
                               "archive": {"deployment": 21, "pointer_valid": False}, "sensors": [{"key": "PAR"}]})
    config = {"name": "Test", "interval": 14400, "sample_interval": 0, "start_mode": "immediate", "memory_mode": "stop"}
    status, plan = request('/api/u30/preview', {'configuration': config})
    assert status == 200 and plan['raw_backup_required']
    body = {'token': plan['token'], 'serial': '22005319', 'confirmed': True, 'raw_recovery_confirmed': True}
    assert request('/api/u30/apply', body)[0] == 403
    body['setup_key'] = 'test-only-key'
    assert request('/api/u30/apply', body, origin='http://evil.invalid')[0] == 403
    assert request('/api/u30/apply', {**body, 'raw_recovery_confirmed': False})[0] == 400
    assert request('/api/u30/apply', body)[0] == 202
    assert app.deployment_requested['raw_recovery_confirmed'] is True
    assert app.interval == 2  # Queueing never changes the interval before USB verification.
