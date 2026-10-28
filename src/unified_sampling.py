"""One acquisition interval for USB queries, Pi samples and native U30 logging."""
import json
import math
import time

from pi_collection import seconds, utc


class UnifiedSampling:
    def sampling_config(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='unified_sampling'").fetchone()
        return json.loads(row[0]) if row else None

    def unify_sampling(self, interval, serial, deployment, clock=None):
        interval = seconds(interval, "採集間隔")
        clock = time.time() if clock is None else clock
        setting = {"interval_seconds": interval, "serial": serial,
                   "deployment": deployment, "updated_utc": utc(clock)}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT channel, config, runtime FROM pi_collection").fetchall():
                config, runtime = json.loads(row[1]), json.loads(row[2])
                # Preserve enabled channels and the number of samples per
                # statistical window, never a second acquisition interval.
                count = max(1, config["interval"] // config["sample_interval"]) if config["statistics"] else 1
                config.update(interval=interval*count, sample_interval=interval,
                              aggregate_count=count, updated_utc=utc(clock))
                start = runtime.get("start")
                if start is not None:
                    start = max(start, clock)
                    if config["start_mode"] == "aligned":
                        start = (math.floor((clock+28800)/(interval*count))+1)*(interval*count)-28800
                runtime = {"start": start, "next_sample": start, "n": 0}
                config["start_at"] = utc(start) if start is not None else None
                db.execute("UPDATE pi_collection SET config=?, runtime=? WHERE channel=?",
                           (json.dumps(config), json.dumps(runtime), row[0]))
            db.execute("INSERT OR REPLACE INTO state VALUES ('unified_sampling', ?)", (json.dumps(setting),))
        return setting
