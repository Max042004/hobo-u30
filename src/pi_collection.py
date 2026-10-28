"""Durable per-channel Pi schedules and streaming population statistics."""
import json
import math
import time
from datetime import datetime, timezone

STATS = ("min", "max", "mean", "stddev")
NAMES = {"min": "最小值", "max": "最大值", "mean": "平均值", "stddev": "標準差"}


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def seconds(value, name):
    if type(value) is not int or not 1 <= value <= 64800:
        raise ValueError(name + " 必須是 1～64800 的整數秒")
    return value


class PiCollection:
    def init_collection(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS pi_collection (channel TEXT PRIMARY KEY, config TEXT NOT NULL, runtime TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_source_time ON samples(source, timestamp_utc);
        """)

    @staticmethod
    def storage_config(db):
        row = db.execute("SELECT value FROM state WHERE key='pi_storage'").fetchone()
        return json.loads(row[0]) if row else {"max_rows": 0, "mode": "stop", "halted": False}

    def collection_status(self):
        with self.connect() as db:
            entries = {r[0]: {"config": json.loads(r[1]), "runtime": json.loads(r[2])}
                       for r in db.execute("SELECT * FROM pi_collection")}
            storage = self.storage_config(db)
            storage["rows"] = db.execute("SELECT COUNT(*) FROM samples WHERE source='live'").fetchone()[0]
        return {"channels": entries, "storage": storage, "statistics": list(STATS),
                "note": "間隔為排程目標；USB 讀取耗時與斷線可能造成漏樣，不會補造資料。標準差採母體公式。"}

    def configure_collection(self, data, clock=None):
        clock = time.time() if clock is None else clock
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if data.get("target") == "storage":
                count, mode = data.get("max_rows"), data.get("mode")
                if type(count) is not int or not 0 <= count <= 1000000000 or mode not in ("stop", "wrap"):
                    raise ValueError("Pi 容量須為 0～1000000000 筆；0 表示不設筆數上限")
                if mode == "wrap" and data.get("overwrite_confirmed") is not True:
                    raise ValueError("循環模式須確認永久覆寫最舊的 Pi 即時資料")
                value = {"max_rows": count, "mode": mode, "halted": False}
                db.execute("INSERT OR REPLACE INTO state VALUES ('pi_storage', ?)", (json.dumps(value),))
                return value
            key = data.get("channel")
            if not isinstance(key, str):
                raise ValueError("通道無效")
            row = db.execute("SELECT metadata FROM channels WHERE key=?", (key,)).fetchone()
            if not row or json.loads(row[0]).get("kind") == "statistic":
                raise ValueError("請選擇實體感測通道")
            unified = db.execute("SELECT value FROM state WHERE key='unified_sampling'").fetchone()
            interval = seconds(data.get("interval"), "記錄間隔") if not unified else json.loads(unified[0])["interval_seconds"]
            stats = data.get("statistics", [])
            if not isinstance(stats, list) or len(stats) > 4 or any(s not in STATS for s in stats) or len(stats) != len(set(stats)):
                raise ValueError("統計選項無效")
            if unified:
                count = data.get("aggregate_count", 1) if stats else 1
                if type(count) is not int or not 1 <= count <= 64800:
                    raise ValueError("統計彙整筆數必須是 1～64800 的整數")
                sample = interval
                interval *= count
            else:
                sample = seconds(data.get("sample_interval", interval), "取樣間隔") if stats else interval
            if sample > interval or interval % sample:
                raise ValueError("統計取樣間隔須不大於記錄間隔，且能整除記錄間隔")
            mode = data.get("start_mode")
            if mode not in ("immediate", "aligned", "button", "delayed"):
                raise ValueError("啟動方式無效")
            start = clock
            if mode == "aligned":
                # Align to Taipei wall-clock intervals, including intervals >1 hour.
                start = (math.floor((clock + 28800) / interval) + 1) * interval - 28800
            elif mode == "button":
                start = None
            elif mode == "delayed":
                try:
                    date = datetime.fromisoformat(data.get("start_at", "").replace("Z", "+00:00"))
                    if date.utcoffset() is None:
                        raise ValueError()
                    start = date.timestamp()
                except (ValueError, TypeError):
                    raise ValueError("延遲開始須指定含時區的時間")
                if start <= clock:
                    raise ValueError("延遲開始時間必須在未來")
            config = {"enabled": data.get("enabled") is True, "interval": interval, "sample_interval": sample,
                      "statistics": stats, "start_mode": mode, "start_at": utc(start) if start is not None else None,
                      "updated_utc": utc(clock)}
            if unified:
                config["aggregate_count"] = count
            runtime = {"start": start, "next_sample": start, "n": 0}
            db.execute("INSERT OR REPLACE INTO pi_collection VALUES (?, ?, ?)", (key, json.dumps(config), json.dumps(runtime)))
            channel = json.loads(row[0])
            for stat in stats:
                self.channel(db, {**channel, "key": key+":stat:"+stat, "kind": "statistic", "parent": key,
                                  "measurement": channel["measurement"]+" "+NAMES[stat],
                                  "label": (channel.get("label") or channel["measurement"])+" · "+NAMES[stat],
                                  "interval_seconds": interval, "sample_interval_seconds": sample})
        return config

    def start_collection(self, key, clock=None):
        if not isinstance(key, str):
            raise ValueError("通道無效")
        clock = time.time() if clock is None else clock
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config, runtime FROM pi_collection WHERE channel=?", (key,)).fetchone()
            if not row:
                raise ValueError("通道尚未設定")
            config, runtime = map(json.loads, row)
            if config["start_mode"] != "button" or not config["enabled"] or runtime.get("start") is not None:
                raise ValueError("此通道不在等待按鈕啟動狀態")
            runtime.update(start=clock, next_sample=clock)
            db.execute("UPDATE pi_collection SET runtime=? WHERE channel=?", (json.dumps(runtime), key))
        return {"started_utc": utc(clock)}

    def collection_interval(self, default):
        # The global interval is authoritative. Channel statistics must never
        # turn a four-hour acquisition schedule back into rapid USB polling.
        unified = self.sampling_config()
        if unified:
            return unified["interval_seconds"]
        with self.connect() as db:
            configs = [json.loads(r[0]) for r in db.execute("SELECT config FROM pi_collection")]
        return min([default] + [c["sample_interval"] for c in configs if c["enabled"]])

    def collect_snapshot(self, db, data):
        storage = self.storage_config(db)
        if storage["halted"]:
            return
        configs = {r[0]: (json.loads(r[1]), json.loads(r[2])) for r in db.execute("SELECT * FROM pi_collection")}
        unified = db.execute("SELECT value FROM state WHERE key='unified_sampling'").fetchone()
        global_interval = json.loads(unified[0])["interval_seconds"] if unified else None
        when = datetime.fromisoformat(data["observed_utc"].replace("Z", "+00:00")).timestamp()
        prefix = (data["serial"], self.deployment(data["archive"]), "live")
        pending = []
        for channel in data["sensors"]:
            key = channel["key"]
            if key not in configs:
                quality = channel["quality"] + (f";interval={global_interval}" if global_interval else "")
                pending.append((*prefix, key, data["observed_utc"], channel["raw"], channel["value"], quality))
                continue
            config, runtime = configs[key]
            identity = data["serial"]+"/"+self.deployment(data["archive"])
            if runtime.get("identity") != identity:
                runtime.update(n=0, identity=identity)
            start = runtime.get("start")
            if not config["enabled"] or start is None or when < runtime["next_sample"]:
                continue
            window = math.floor((when-start) / config["interval"])
            old_window = runtime.get("window", window)
            if config["statistics"] and old_window != window and runtime["n"]:
                end = start + (old_window+1)*config["interval"]
                values = {"min": runtime["min"], "max": runtime["max"], "mean": runtime["mean"],
                          "stddev": math.sqrt(max(0, runtime["m2"]/runtime["n"]))}
                for stat in config["statistics"]:
                    meta = {**channel, "key": key+":stat:"+stat, "kind": "statistic", "parent": key,
                            "measurement": channel["measurement"]+" "+NAMES[stat],
                            "label": (channel.get("label") or channel["measurement"])+" · "+NAMES[stat],
                            "interval_seconds": config["interval"], "sample_interval_seconds": config["sample_interval"]}
                    self.channel(db, meta)
                    expected = config["interval"]//config["sample_interval"]
                    pending.append((*prefix, meta["key"], utc(end), None, values[stat],
                                    f"statistics_n={runtime['n']};expected={expected};interval={config['interval']}"))
                runtime["n"] = 0
            runtime["window"] = window
            runtime["next_sample"] = start+(math.floor((when-start)/config["sample_interval"])+1)*config["sample_interval"]
            runtime["last_observed_utc"] = data["observed_utc"]
            if not config["statistics"]:
                pending.append((*prefix, key, data["observed_utc"], channel["raw"], channel["value"], channel["quality"]+f";interval={config['interval']}"))
            elif channel["value"] is not None and math.isfinite(channel["value"]) and channel["quality"] == "ok":
                value = channel["value"]
                n = runtime["n"]+1
                mean = runtime.get("mean", 0) if n > 1 else 0
                delta = value-mean
                new_mean = mean+delta/n
                runtime.update(n=n, mean=new_mean, m2=(runtime.get("m2", 0) if n > 1 else 0)+delta*(value-new_mean),
                               min=min(runtime["min"], value) if n > 1 else value,
                               max=max(runtime["max"], value) if n > 1 else value)
            db.execute("UPDATE pi_collection SET runtime=? WHERE channel=?", (json.dumps(runtime), key))
        limit = storage["max_rows"]
        if limit:
            count = db.execute("SELECT COUNT(*) FROM samples WHERE source='live'").fetchone()[0]
            if storage["mode"] == "stop" and count+len(pending) > limit:
                storage["halted"] = True
                db.execute("INSERT OR REPLACE INTO state VALUES ('pi_storage', ?)", (json.dumps(storage),))
                return
        self.insert_samples(db, pending)
        if limit and storage["mode"] == "wrap":
            # Only active Pi live records; archives, raw backups and recycle bin are retained.
            excess = db.execute("SELECT COUNT(*) FROM samples WHERE source='live'").fetchone()[0]-limit
            if excess > 0:
                db.execute("DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples WHERE source='live' ORDER BY timestamp_utc, channel LIMIT ?)", (excess,))
