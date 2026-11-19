"""LAN HTTP dashboard, durable SQLite collection, and raw U30 backups."""
import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import signal
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from station import StationReader, backup, now
from network_portal import network_request
from history_tools import HistoryTools, HistoryConflict
from pi_collection import PiCollection
from unified_sampling import UnifiedSampling
from plot_workbench import PlotWorkbench
from u30_settings import validate_settings, apply_deployment
from native_control import NativeControl
from u30_data import parse_memory_header, iter_archive

LOG = logging.getLogger("u30")
ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / 'manual' / 'HOBO_U30_Raspberry_Pi_Manual.pdf'


class Store(PlotWorkbench, HistoryTools, PiCollection, UnifiedSampling):
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "station.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS channels (key TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    device TEXT, deployment TEXT, source TEXT, channel TEXT, timestamp_utc TEXT,
                    raw INTEGER, value REAL, quality TEXT,
                    PRIMARY KEY(device, deployment, source, channel, timestamp_utc));
                CREATE INDEX IF NOT EXISTS samples_time ON samples(channel, source, timestamp_utc);
                CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sample_deletions (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, restored_utc TEXT);
                CREATE TABLE IF NOT EXISTS deleted_samples (
                    batch TEXT NOT NULL, device TEXT, deployment TEXT, source TEXT, channel TEXT, timestamp_utc TEXT,
                    raw INTEGER, value REAL, quality TEXT,
                    PRIMARY KEY(device, deployment, source, channel, timestamp_utc));
                CREATE INDEX IF NOT EXISTS deleted_samples_batch ON deleted_samples(batch);
            """)
            self.init_collection(db)
            self.init_workbench(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def deployment(info):
        if info.get('metadata_valid') is False:
            return 'live-unverified'
        return f"{info['deployment']}:{info['launched_utc']}"

    @staticmethod
    def channel(db, channel):
        db.execute("INSERT INTO channels VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET metadata=excluded.metadata WHERE channels.metadata<>excluded.metadata",
                   (channel["key"], json.dumps(channel, ensure_ascii=False)))

    def save_snapshot(self, data):
        info = data["archive"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for channel in info["channels"]:
                self.channel(db, channel)
            for channel in data["sensors"]:
                self.channel(db, channel)
            self.collect_snapshot(db, data)
            self.observe_snapshot(db, data)
            db.execute("INSERT INTO state VALUES ('snapshot', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(data, ensure_ascii=False),))

    def load_snapshot(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()
            return json.loads(row[0]) if row else None

    def import_archive(self, path):
        image = Path(path).read_bytes()
        digest = hashlib.sha256(image).hexdigest()
        with self.connect() as db:
            found = db.execute("SELECT metadata FROM backups WHERE id=?", (digest,)).fetchone()
        info = parse_memory_header(image)
        manifest_path = Path(path).with_suffix(".json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        if found:
            previous = json.loads(found[0])
            if (previous.get("decoded") or previous.get("recovery_verified") or not manifest.get("recovery_verified")):
                return previous
        stable = manifest.get("consistent_snapshot", manifest.get("stable_header", manifest.get("stable_first_block", False)))
        bounds = manifest.get("snapshot_bounds")
        if stable and bounds is not None:
            end, bit = bounds.get("data_end"), bounds.get("data_end_bit")
            if (manifest.get("sha256") != digest or type(end) is not int or type(bit) is not int or
                    not info["data_start"] <= end <= info["memory_bytes"] or not 0 <= bit <= 7 or
                    (end == info["memory_bytes"] and bit)):
                raise ValueError("invalid verified backup bounds")
            info.update(data_end=end, data_end_bit=bit)
        result = {"id": digest, "file": Path(path).name, "bytes": len(image), "sha256": digest,
                  "created_utc": manifest.get("created_utc", manifest.get("collected_utc", now())),
                  "stable_header": manifest.get("stable_header", stable), "consistent_snapshot": stable,
                  "serial": info["serial"], "rows": 0,
                  "channels": len(info["channels"]), "first_utc": None, "last_utc": None,
                  "decoded": False, "archive": info}
        raw_verified = bool(manifest.get("raw_recovery") and manifest.get("recovery_verified") and
                            manifest.get("memory_verified_twice") and manifest.get("complete_image") and
                            manifest.get("sha256") == digest and manifest.get("bytes") == len(image) == 524288 and
                            manifest.get("stable_header") and manifest.get("status_before_hex") == "00" and
                            manifest.get("status_after_hex") == "00")
        result.update(raw_recovery=bool(manifest.get("raw_recovery")),
                      recovery_verified=raw_verified,
                      memory_verified_twice=bool(manifest.get("memory_verified_twice")))
        # A running logger can update memory during readout. Keep its image,
        # but do not silently label an unverified snapshot as decoded history.
        if not stable:
            result["error"] = ("記憶體指標無法確認；完整原始備份已保留，未推測歷史資料範圍。"
                               + ("全部記憶體已讀取兩遍並逐位元組核對一致。" if result["recovery_verified"] else
                                  "原始映像尚未通過停止狀態與兩遍核對，不能用於重新部署。")) if result["raw_recovery"] else "Header changed or consistency evidence is missing; raw backup retained."
        elif info["wrap_count"]:
            result["error"] = "Circular memory decoding is not yet validated; raw backup retained."
        else:
            with self.connect() as db:
                for channel in info["channels"]:
                    self.channel(db, channel)
                batch = []
                for row in iter_archive(image, info):
                    if result["first_utc"] is None:
                        result["first_utc"] = row["timestamp_utc"]
                    result["last_utc"] = row["timestamp_utc"]
                    result["rows"] += 1
                    batch.append((info["serial"], self.deployment(info), "archive", row["channel_key"],
                                  row["timestamp_utc"], row["raw"], row["value"], row["quality"]))
                    if len(batch) >= 2000:
                        self.insert_samples(db, batch)
                        batch.clear()
                self.insert_samples(db, batch)
                result["decoded"] = True
                db.execute("INSERT INTO backups VALUES (?, ?)", (digest, json.dumps(result, ensure_ascii=False)))
                return result
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO backups VALUES (?, ?)", (digest, json.dumps(result, ensure_ascii=False)))
        return result

    def backups(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT metadata FROM backups ORDER BY rowid DESC")]

    def channels(self):
        with self.connect() as db:
            stats = {r["channel"]: dict(r) for r in db.execute("""
                SELECT channel, COUNT(*) AS rows, MIN(timestamp_utc) AS first_utc,
                       MAX(timestamp_utc) AS last_utc FROM samples GROUP BY channel""")}
            return [{**json.loads(r["metadata"]), "history": stats.get(r["key"], {"rows": 0})}
                    for r in db.execute("SELECT * FROM channels ORDER BY key")]

    @staticmethod
    def filters(query):
        return HistoryTools.filters(query)

    def history(self, query):
        return super().history(query)


class App(NativeControl):
    def __init__(self, data, interval):
        self.store = Store(data)
        setting = self.store.sampling_config()
        old = self.store.load_snapshot()
        if setting is None and old:
            archive = old.get("archive", {})
            native = archive.get("logging_interval_seconds")
            if archive.get("metadata_valid") is not False and type(native) is int and 1 <= native <= 64800 and archive.get("sampling_interval_seconds", 0) == 0:
                setting = self.store.unify_sampling(native, old["serial"], archive.get("deployment"))
        if old:
            old.pop("alarms", None)
            old.pop("relay_response_hex", None)
        self.interval = setting["interval_seconds"] if setting else interval
        self.next_query = time.monotonic()
        self.next_query_utc = time.time()
        self.polling_revision = 0
        self.refresh_requested = False
        self.reader = StationReader()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.sync_requested = False
        self.sync_stop = False
        self.deployment_requested = None
        self.deployment_previews = {}
        self.deletion_previews = {}
        self.state = {"usb_connected": False, "error": None, "last_success_utc": None,
                      "snapshot": old, "interval_seconds": self.interval, "query_running": False,
                      "sync": {"running": False, "bytes": 0, "total": 524288}}
        self.state["deployment"] = {"running": False}
        self.init_native_control()

    def status(self):
        with self.lock:
            state = json.loads(json.dumps(self.state))
            due = self.next_query
            state["next_query_utc"] = datetime.fromtimestamp(self.next_query_utc, timezone.utc).isoformat(timespec="seconds")
            interval = self.interval
        last = state["last_success_utc"]
        age = time.time()-datetime.fromisoformat(last).timestamp() if last else math.inf
        state["stale"] = age > max(30, interval*3)
        state["device_state_fresh"] = bool(state["usb_connected"] and age <= 30)
        state["waiting_for_query"] = bool(state["usb_connected"] and not state["query_running"] and time.monotonic() < due)
        setting = self.store.sampling_config()
        archive = (state.get("snapshot") or {}).get("archive", {})
        state["sampling"] = {"interval_seconds": interval, "configured": setting is not None,
                             "matches_u30": bool(setting and archive.get("metadata_valid") is not False and
                                 (state.get("snapshot") or {}).get("serial") == setting["serial"] and
                                 archive.get("logging_interval_seconds") == interval and archive.get("sampling_interval_seconds", 0) == 0),
                             "updated_utc": setting.get("updated_utc") if setting else None}
        state["server_utc"] = now()
        state["pi_collection"] = self.store.collection_status()
        state["native_control"] = self.native_status()
        return state

    def request(self, action):
        with self.lock:
            if action == "refresh":
                self.refresh_requested = True
            if action == "sync":
                if self.device_busy():
                    return False
                self.sync_requested = True
                self.sync_stop = False
        self.wake.set()
        return True

    def preview_deployment(self, data):
        config = validate_settings(data)
        if config["sample_interval"]:
            raise ValueError("全站採集只使用一個頻率；請停用 U30 額外平均取樣")
        state = self.status()
        if not state["device_state_fresh"] or not state["snapshot"]:
            raise HistoryConflict("請先按右上角「重新讀取」取得 U30 最新狀態，再預覽全站設定")
        snapshot = state["snapshot"]
        if snapshot["archive"].get("metadata_valid") is False:
            raise HistoryConflict("U30 記憶體部署資訊尚未確認；即時採集可繼續，暫不允許重新部署")
        if not snapshot.get("sensors"):
            raise ValueError("U30 未偵測到感測器")
        token = secrets.token_urlsafe(24)
        plan = {"configuration": config, "serial": snapshot["serial"], "channels": [c["key"] for c in snapshot["sensors"]],
                "current_deployment": snapshot["archive"]["deployment"], "created_utc": now(),
                "requires_stop_first": snapshot.get("status_code") in (1, 3, 5),
                "raw_backup_required": snapshot["archive"].get("pointer_valid") is False,
                "warning": "將統一 U30 記錄、Pi 查詢及取樣頻率，並重設未完成的 Pi 統計視窗。重新部署會清除 U30 內原部署；套用前會下載並驗證完整備份，U30 核對成功後才更新 Pi。Pi 既有資料保留。"}
        if plan["raw_backup_required"]:
            plan["warning"] += " 記憶體指標異常時，先讀取完整 512 KB 兩遍並逐位元組核對，約需一般備份兩倍時間。原始資料保留，但無法確認舊資料的有效範圍或保證可還原成歷史讀值；須另外確認後才重新部署。"
        with self.lock:
            self.deployment_previews = {k:v for k,v in self.deployment_previews.items() if time.monotonic()-v[0] < 600}
            if len(self.deployment_previews) >= 32:
                self.deployment_previews.pop(next(iter(self.deployment_previews)))
            self.deployment_previews[token] = (time.monotonic(), plan)
        return {**plan, "token": token}

    def queue_deployment(self, token, serial, raw_recovery_confirmed=False):
        if not isinstance(token, str):
            raise ValueError("invalid preview token")
        with self.lock:
            entry = self.deployment_previews.get(token)
            if not entry or time.monotonic()-entry[0] > 600:
                raise HistoryConflict("部署預覽已過期")
            plan = entry[1]
            if serial != plan["serial"]:
                raise ValueError("請輸入預覽中的完整 U30 序號確認重新部署")
            snapshot = self.state.get("snapshot")
            last = self.state.get("last_success_utc")
            if not self.state["usb_connected"] or not last or time.time()-datetime.fromisoformat(last).timestamp() > 30:
                raise HistoryConflict("請先按重新讀取，確認 U30 目前狀態後再套用")
            if snapshot and snapshot["archive"].get("metadata_valid") is False:
                raise HistoryConflict("U30 記憶體部署資訊尚未確認，請重新取得狀態")
            if plan.get("raw_backup_required") and raw_recovery_confirmed is not True:
                raise ValueError("請確認舊歷史範圍無法判定，並同意完整原始備份核對後重新部署")
            if snapshot and snapshot["archive"].get("pointer_valid") is False and not plan.get("raw_backup_required"):
                raise HistoryConflict("記憶體指標狀態已變更；請重新預覽原始備份流程")
            if snapshot and snapshot.get("status_code") in (1, 3, 5):
                raise HistoryConflict("U30 仍在記錄或等待啟動，請先選擇停止記錄再下載，完成後重新預覽部署")
            if (not snapshot or snapshot["serial"] != serial or snapshot["archive"]["deployment"] != plan["current_deployment"] or
                    sorted(c["key"] for c in snapshot["sensors"]) != sorted(plan["channels"])):
                raise HistoryConflict("U30 或感測器已變更，請重新預覽")
            if self.device_busy():
                raise HistoryConflict("裝置已有工作執行中")
            self.deployment_requested = {**plan, "raw_recovery_confirmed": bool(plan.get("raw_backup_required") and raw_recovery_confirmed is True)}
            del self.deployment_previews[token]
        self.wake.set()
        return {"accepted": True}

    def device_busy(self):
        # Call with self.lock held.
        return (self.native_requested or self.deployment_requested or self.sync_requested or self.state["sync"]["running"] or
                self.state["deployment"]["running"])



    def preview_delete(self, query):
        plan = self.store.preview_delete(query)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.deletion_previews = {k: v for k, v in self.deletion_previews.items() if time.monotonic()-v[0] < 300}
            if len(self.deletion_previews) >= 64:
                self.deletion_previews.pop(next(iter(self.deletion_previews)))
            self.deletion_previews[token] = (time.monotonic(), plan)
        return {k: v for k, v in plan.items() if k != "signature"} | {"token": token}

    def commit_delete(self, token):
        if not isinstance(token, str):
            raise ValueError("invalid preview token")
        with self.lock:
            entry = self.deletion_previews.get(token)
            if not entry or time.monotonic()-entry[0] > 300:
                raise HistoryConflict("預覽已過期，請重新預覽")
            if self.state["sync"]["running"]:
                raise HistoryConflict("U30 備份下載中，請完成後再刪除")
        result = self.store.trash_samples(entry[1])
        with self.lock:
            self.deletion_previews.pop(token, None)
        return result

    def progress(self, done, total):
        if self.stop.is_set():
            raise InterruptedError("server is stopping; partial backup retained")
        with self.lock:
            self.state["sync"].update(bytes=done, total=total)

    def activate_sampling(self, interval, serial, deployment):
        # Called only after native readback succeeds (or to adopt a verified
        # existing native interval). Persist Pi schedules in one transaction.
        with self.lock:
            setting = self.store.unify_sampling(interval, serial, deployment)
            self.interval = interval
            self.polling_revision += 1
            self.state["interval_seconds"] = interval
        return setting

    def poll_once(self):
        started, epoch = time.monotonic(), time.time()
        with self.lock:
            revision = self.polling_revision
            self.state.update(query_running=True, last_attempt_utc=datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds"))
        try:
            data = self.reader.read()
            if self.store.sampling_config() is None:
                archive = data["archive"]
                native = archive.get("logging_interval_seconds")
                if archive.get("metadata_valid") is not False and type(native) is int and 1 <= native <= 64800 and archive.get("sampling_interval_seconds", 0) == 0:
                    self.activate_sampling(native, data["serial"], archive.get("deployment"))
                    revision = self.polling_revision
            self.store.save_snapshot(data)
            self.store.observe_connection(True)
            with self.lock:
                self.state.update(usb_connected=True, error=None, last_success_utc=data["observed_utc"], snapshot=data)
            try:
                self.observe_native(data)
            except Exception as exc:
                # Optional enrollment must never turn a successful measurement
                # into a USB failure or stop future Pi collection.
                LOG.exception('Could not schedule native sensor enrollment')
                with self.lock:
                    self.native_policy = {**self.native_policy, 'blocked': True, 'error': str(exc)}
        except Exception as exc:
            self.reader.invalidate()
            try:
                self.store.observe_connection(False)
            except Exception:
                LOG.exception('Could not save connection observation')
            with self.lock:
                previous = self.state.get("error")
                self.state.update(usb_connected=False, error=str(exc))
            if previous != str(exc):
                LOG.warning("U30 query: %s", exc)
        finally:
            elapsed = time.monotonic()-started
            with self.lock:
                self.state.update(query_running=False, actual_cycle_seconds=round(elapsed, 3))
                # Even failures wait for the same acquisition interval. No
                # rapid retry loop that defeats the selected power policy.
                if revision == self.polling_revision:
                    delay = max(.1, self.interval-elapsed)
                    self.next_query = time.monotonic()+delay
                    self.next_query_utc = time.time()+delay

    def run(self):
        for path in sorted((self.store.directory/"backups").glob("*.bin")):
            try:
                result = self.store.import_archive(path)
                LOG.info("Archive %s: %s rows", path.name, result["rows"])
            except Exception:
                LOG.exception("Could not import %s", path)
        update_lock = os.environ.get('U30_UPDATE_LOCK')
        while not self.stop.is_set():
            if update_lock:
                import fcntl
                with open(update_lock, 'rb') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.stop.wait(1)
                        continue
                    should_query = self.run_device_cycle()
            else:
                should_query = self.run_device_cycle()
            if not should_query:
                self.wake.wait(max(0, self.next_query - time.monotonic()))

    def run_device_cycle(self):
        with self.lock:
            self.wake.clear()
            sync = self.sync_requested
            stop_readout = self.sync_stop
            self.sync_requested = False
            deployment = self.deployment_requested
            self.deployment_requested = None
            native = self.native_requested
            self.native_requested = None
            refresh = self.refresh_requested
            self.refresh_requested = False
            should_query = bool(refresh or sync or deployment or native or time.monotonic() >= self.next_query)
            if native:
                self.state['deployment']['running'] = True
            if deployment:
                self.state["deployment"]["running"]=True
                self.state["sync"]["running"]=True
            if sync:
                self.state["sync"]["running"]=True
        if not should_query:
            return False
        if deployment:
            with self.lock:
                self.state["deployment"] = {"running": True, "phase": "完整備份與設定驗證中"}
                self.state["sync"] = {"running": True, "bytes": 0, "total": 524288}
            try:
                result = apply_deployment(self.store.directory/"backups", deployment["configuration"], deployment["serial"], self.progress,
                                          deployment["channels"], deployment["current_deployment"], deployment.get("raw_recovery_confirmed", False))
                self.activate_sampling(result["configuration"]["interval"], result["serial"], result["archive"]["deployment"])
                self.native_success()
                self.store.import_archive(self.store.directory/"backups"/result["backup"])
                with self.lock:
                    self.state["deployment"].update(result=result, phase="設定已寫入並核對")
            except Exception as exc:
                LOG.exception("U30 deployment failed")
                with self.lock:
                    self.state["deployment"].update(error=str(exc), phase="未完成；請檢查裝置狀態與備份")
            finally:
                self.reader.invalidate()
                with self.lock:
                    self.state["deployment"]["running"] = False
                    self.state["sync"]["running"] = False
        if native:
            self.execute_native(native)
        if sync:
            with self.lock:
                self.state["sync"] = {"running": True, "bytes": 0, "total": 524288, "error": None}
            try:
                path, manifest = backup(self.store.directory/"backups", self.progress, stop_logging=stop_readout)
                result = self.store.import_archive(path)
                with self.lock:
                    self.state["sync"]["result"] = result
                LOG.info("Backup finished: %s", path.name)
            except Exception as exc:
                LOG.exception("Backup failed")
                with self.lock:
                    self.state["sync"]["error"] = str(exc)
            finally:
                self.reader.invalidate()
                with self.lock:
                    self.state["sync"]["running"] = False
        self.poll_once()
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "U30Station/0.1"

    @property
    def app(self):
        return self.server.app

    def send_bytes(self, body, mime, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/app.js", "/studio.js", "/i18n.js", "/navigation.js", "/native-control.js", "/password.js", "/software-update.js", "/workspace.css", "/style.css"):
                name, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
                              "/navigation.js": ("navigation.js", "text/javascript; charset=utf-8"),
                              "/native-control.js": ("native-control.js", "text/javascript; charset=utf-8"),
                              "/password.js": ("password.js", "text/javascript; charset=utf-8"),
                              "/software-update.js": ("software-update.js", "text/javascript; charset=utf-8"),
                              "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
                              "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
                              "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
                return self.send_bytes((ROOT/"web"/name).read_bytes(), mime)
            if path == '/manual/HOBO_U30_Raspberry_Pi_Manual.pdf':
                try:
                    manual = MANUAL.read_bytes()
                except FileNotFoundError:
                    return self.json({'error': '使用手冊目前無法下載。'}, 404)
                return self.send_bytes(manual, 'application/pdf', filename=MANUAL.name)
            if path == "/health":
                return self.json({"ok": True, "usb_connected": self.app.status()["usb_connected"]})
            if path == "/api/status":
                return self.json(self.app.status())
            if path == "/api/collection":
                return self.json(self.app.store.collection_status())
            if path == "/api/network":
                result, status = network_request()
                return self.json(result, status)
            if path == "/api/trash":
                offset = max(0, int(query.get("offset", ["0"])[0]))
                return self.json({"batches": self.app.store.trash(offset), "offset": offset, "total": self.app.store.trash_count()})
            if path == "/api/channels":
                return self.json(self.app.store.channels())
            if path == '/api/studio/catalog':
                return self.json(self.app.store.catalog())
            if path == "/api/backups":
                return self.json(self.app.store.backups())
            if path == "/api/history":
                return self.json(self.app.store.history(query))
            if path == "/api/export.csv":
                return self.export(query)
            if path.startswith("/api/backups/") and path.endswith(".bin"):
                digest = path.split("/")[-1][:-4]
                found = next((b for b in self.app.store.backups() if b["id"] == digest), None)
                if found:
                    target = self.app.store.directory/"backups"/Path(found["file"]).name
                    return self.send_bytes(target.read_bytes(), "application/octet-stream", filename=target.name)
            return self.json({"error": "not found"}, 404)
        except (ValueError, OverflowError) as exc:
            self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("HTTP request failed")
            self.json({"error": "server error"}, 500)

    def reject_post(self, message, status):
        # Drain small rejected bodies before closing. Closing with unread bytes
        # can reset the TCP connection and hide the 403/404 from some clients.
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if 0 < length <= 4096 and not self.headers.get('Transfer-Encoding'):
                self.connection.settimeout(1)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        self.close_connection = True
        return self.json({'error': message}, status)

    def do_POST(self):
        # No cross-origin device actions. There is no CORS allowance.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.reject_post('origin mismatch', 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.reject_post('cross-site request denied', 403)
        if urlparse(self.path).path in {"/api/alarms", "/api/alarms/reload", "/api/alarms/preview",
                                            "/api/alarms/apply", "/api/relay/action"}:
            return self.reject_post('not found', 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            maximum=17*1024*1024 if self.path=='/api/studio/import' else 131072 if self.path.startswith('/api/studio/') else 4096
            if not 0 <= length <= maximum or self.headers.get("Transfer-Encoding"):
                raise ValueError()
        except ValueError:
            return self.json({"error": "invalid request length"}, 400)
        if self.path in ("/api/network/connect", "/api/network/password", "/api/network/software-update", "/api/data/delete-preview", "/api/data/trash", "/api/data/restore",
                         "/api/collection/settings", "/api/collection/start", "/api/u30/preview", "/api/u30/apply", "/api/readout", "/api/u30/control",
                         '/api/studio/query', '/api/studio/export', '/api/studio/import'):
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.json({"error": "JSON body required"}, 400)
            self.connection.settimeout(5)
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, OSError):
                return self.json({"error": "invalid JSON body"}, 400)
            if self.path == "/api/network/connect":
                result, status = network_request("connect", data)
                return self.json(result, status)
            if self.path == '/api/network/password':
                result, status = network_request('password', data)
                return self.json(result, status)
            if self.path == '/api/network/software-update':
                result, status = network_request('software_update', {'setup_key': data.get('setup_key')})
                return self.json(result, status)
            try:
                if self.path == "/api/data/delete-preview":
                    return self.json(self.app.preview_delete(data.get("filters")))
                if self.path == "/api/u30/preview":
                    return self.json(self.app.preview_deployment(data.get("configuration", {})))
                if self.path == '/api/studio/query':
                    return self.json(self.app.store.workbench_query(data))
                if self.path == '/api/studio/export':
                    format = data.get('format', 'csv')
                    with self.app.store.workbench_export(data, format) as output:
                        output.seek(0, 2)
                        size = output.tell()
                        output.seek(0)
                        self.send_response(200)
                        self.send_header('Content-Type', {'csv':'text/csv; charset=utf-8','txt':'text/tab-separated-values; charset=utf-8','xls':'application/vnd.ms-excel'}[format])
                        self.send_header('Content-Length', str(size))
                        self.send_header('Content-Disposition', 'attachment; filename="u30-chart-data.'+format+'"')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while chunk := output.read(65536):
                            self.wfile.write(chunk)
                    return
                authorization, status = network_request("authorize_data", {"setup_key": data.get("setup_key", "")})
                if status != 200 or not authorization.get("authorized"):
                    return self.json({"error": authorization.get("error", "Pi 設定密碼不正確")}, status if status != 200 else 403)
                if self.path == '/api/studio/import':
                    return self.json(self.app.store.import_table(data))
                if self.path == '/api/u30/control':
                    if data.get('action') != 'policy' and data.get('confirmed') is not True:
                        raise ValueError('請確認 U30 記錄操作；恢復時會先備份再重新部署')
                    return self.json(self.app.queue_native(data.get('action'), data.get('serial'), data.get('enabled'),
                                                          data.get('expected_deployment'), data.get('expected_channels')), 202)
                if self.path == "/api/data/trash":
                    if data.get("confirmed") is not True:
                        raise ValueError("請先確認刪除範圍與筆數")
                    return self.json(self.app.commit_delete(data.get("token")))
                if self.path == "/api/collection/settings":
                    result = self.app.store.configure_collection(data)
                    self.app.wake.set()
                    return self.json(result)
                if self.path == "/api/collection/start":
                    result = self.app.store.start_collection(data.get("channel"))
                    self.app.wake.set()
                    return self.json(result)
                if self.path == "/api/u30/apply":
                    if data.get("confirmed") is not True:
                        raise ValueError("請先確認 U30 重新部署會清除舊部署")
                    return self.json(self.app.queue_deployment(data.get("token"), data.get("serial"), data.get("raw_recovery_confirmed") is True), 202)
                if self.path == "/api/readout":
                    if data.get("mode") not in ("stop", "continue"):
                        raise ValueError("讀取模式無效")
                    if data["mode"] == "stop" and data.get("confirmed") is not True:
                        raise ValueError("停止讀取會終止 U30 目前部署，須重新部署才能再次記錄")
                    with self.app.lock:
                        if self.app.device_busy():
                            raise HistoryConflict("裝置已有工作執行中")
                        self.app.sync_requested = True
                        self.app.sync_stop = data["mode"] == "stop"
                    self.app.wake.set()
                    return self.json({"accepted": True}, 202)
                return self.json(self.app.store.restore_samples(data.get("batch")))
            except HistoryConflict as exc:
                return self.json({"error": str(exc)}, 409)
            except (ValueError, OverflowError) as exc:
                return self.json({"error": str(exc)}, 400)
            except Exception:
                LOG.exception("Data action failed")
                return self.json({"error": "資料操作失敗，未完成的交易已回復，請稍後再試"}, 500)
        if length > 0:
            return self.json({"error": "request body is not supported"}, 400)
        if self.path not in ("/api/refresh", "/api/sync"):
            return self.json({"error": "not found"}, 404)
        action = "sync" if self.path.endswith("sync") else "refresh"
        accepted = self.app.request(action)
        return self.json({"accepted": accepted}, 202 if accepted else 409)

    def export(self, query):
        where, args = self.app.store.filters(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="u30-readings.csv"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["timestamp_gmt8", "device", "deployment", "source", "channel", "label", "measurement", "units", "raw", "value", "quality"])
        self.wfile.write(b"\xef\xbb\xbf"+buffer.getvalue().encode())
        with self.app.store.connect() as db:
            channels = {r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM channels")}
            for row in db.execute("SELECT * FROM samples"+where+" ORDER BY timestamp_utc, channel", args):
                channel = channels.get(row["channel"], {})
                def safe(value):
                    s = str(value)
                    return "'"+s if s.startswith(("=", "+", "-", "@", "\t", "\r")) else s
                buffer.seek(0)
                buffer.truncate()
                local_time = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
                writer.writerow([local_time, row["device"], safe(row["deployment"]), row["source"],
                                 safe(row["channel"]), safe(channel.get("label", "")),
                                 safe(channel.get("measurement", "")), safe(channel.get("units", "")),
                                 row["raw"], row["value"], row["quality"]])
                self.wfile.write(buffer.getvalue().encode())

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--data", type=Path, default=ROOT/"data")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("interval must be at least 1 second")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = App(args.data, args.interval)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.app = app
    thread = threading.Thread(target=app.run, name="u30-collector", daemon=True)
    thread.start()
    LOG.info("U30 dashboard listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        app.wake.set()
        server.server_close()
        thread.join(timeout=12)


if __name__ == "__main__":
    main()
