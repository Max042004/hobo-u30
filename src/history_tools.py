"""Shared history predicates, continuous matching intervals, recoverable deletion."""
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone, timedelta

FIELDS = ("source", "channel", "device", "deployment", "start", "end", "value_op", "value", "value_max")
KEYS = "device, deployment, source, channel, timestamp_utc"
COLUMNS = KEYS + ", raw, value, quality"


class HistoryConflict(ValueError):
    pass


def criteria(query):
    if not isinstance(query, dict):
        raise ValueError("invalid filters")
    result = {}
    for name in FIELDS:
        values = query.get(name, [])
        if values:
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
                raise ValueError("invalid filter: " + name)
            if values[0]:
                result[name] = [values[0]]
    return result


def predicates(query, include_value=True):
    query = criteria(query)
    where, args, dates = [], [], {}
    for name in ("channel", "source", "device", "deployment"):
        value = query.get(name, [""])[0]
        if value and value != "all":
            if name == "source" and value not in ("live", "archive", "import"):
                raise ValueError("invalid source")
            where.append(name + "=?")
            args.append(value)
    for name, operator in (("start", ">="), ("end", "<=")):
        value = query.get(name, [""])[0]
        if value:
            if len(value) == 10:
                value += "T00:00:00+08:00" if name == "start" else "T23:59:59+08:00"
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("時間必須包含時區")
            dates[name] = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
            where.append("timestamp_utc"+operator+"?")
            args.append(dates[name])
    if dates.get("start", "") > dates.get("end", "9999"):
        raise ValueError("結束時間不可早於起始時間")
    expression, values = value_predicate(query)
    if include_value and expression != "1":
        where.append(expression)
        args.extend(values)
    return (" WHERE " + " AND ".join(where)) if where else "", args, dates


def value_predicate(query):
    op = query.get("value_op", [""])[0]
    if not op:
        return "1", []
    if not query.get("channel", [""])[0] or query["channel"][0] == "all":
        raise ValueError("數值條件須選定單一通道，避免混用不同單位")
    operators = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if op not in (*operators, "between"):
        raise ValueError("invalid value operator")
    try:
        low = float(query.get("value", [""])[0])
        high = float(query.get("value_max", [""])[0]) if op == "between" else low
    except (ValueError, TypeError):
        raise ValueError("請輸入有效的數值條件") from None
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("數值條件必須是有限數值")
    if low > high:
        raise ValueError("數值上限不可小於下限")
    if op == "between":
        return "value BETWEEN ? AND ?", [low, high]
    return "value " + operators[op] + " ?", [low]


class HistoryTools:
    @staticmethod
    def filters(query):
        where, args, _ = predicates(query)
        return where, args

    @staticmethod
    def insert_samples(db, rows):
        # Tombstones prevent deleted archive rows being reimported from a
        # different raw backup. They are removed by explicit restoration.
        db.executemany("INSERT OR IGNORE INTO samples SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS "
                       "(SELECT 1 FROM deleted_samples WHERE device=? AND deployment=? AND source=? "
                       "AND channel=? AND timestamp_utc=?)", ((*row, *row[:5]) for row in rows))

    def history(self, query):
        base, base_args, _ = predicates(query, include_value=False)
        expression, value_args = value_predicate(criteria(query))
        limit = max(10, min(5000, int(query.get("limit", ["1500"])[0])))
        with self.connect() as db:
            db.execute("BEGIN")
            periods = {}
            for row in db.execute("SELECT metadata FROM backups"):
                info = json.loads(row[0])["archive"]
                periods[self.deployment(info)] = info["logging_interval_seconds"] * 1.5
            gap = ("CASE WHEN source='live' THEN CASE WHEN instr(quality,';interval=')>0 "
                   "THEN MAX(15,CAST(substr(quality,instr(quality,';interval=')+10) AS REAL)*1.5) ELSE 15 END "
                   + "".join("WHEN deployment=? THEN ? " for _ in periods) + "ELSE 0 END")
            gap_args = [value for item in periods.items() for value in item]
            window = "PARTITION BY device,deployment,source,channel ORDER BY timestamp_utc"
            cte = ("WITH base AS (SELECT *, CASE WHEN " + expression + " THEN 1 ELSE 0 END matched, " + gap +
                   " max_gap FROM samples" + base + "), neighbors AS (SELECT *, LAG(matched) OVER (" + window +
                   ") previous_match, LAG(timestamp_utc) OVER (" + window + ") previous_time FROM base), "
                   "groups AS (SELECT *, SUM(CASE WHEN matched=1 AND (COALESCE(previous_match,0)=0 OR "
                   "(julianday(timestamp_utc)-julianday(previous_time))*86400>max_gap+0.001) THEN 1 ELSE 0 END) "
                   "OVER (" + window + " ROWS UNBOUNDED PRECEDING) segment_number FROM neighbors), "
                   "matches AS (SELECT *, device||'|'||deployment||'|'||source||'|'||channel||'|'||segment_number segment "
                   "FROM groups WHERE matched=1) ")
            args = [*value_args, *gap_args, *base_args]
            total = db.execute(cte + "SELECT COUNT(*) FROM matches", args).fetchone()[0]
            stride = max(1, math.ceil(total/limit))
            rows = db.execute(cte + "SELECT * FROM (SELECT timestamp_utc,channel,source,raw,value,quality,segment, "
                              "ROW_NUMBER() OVER (ORDER BY timestamp_utc,channel,source) rn FROM matches) "
                              "WHERE (rn-1)%?=0 OR rn=? ORDER BY rn", [*args, stride, total])
            result = {"total": total, "stride": stride, "rows": [dict(r) for r in rows], "intervals": [], "interval_count": 0}
            if expression != "1":
                intervals = db.execute(cte + "SELECT segment,MIN(timestamp_utc) start_utc,MAX(timestamp_utc) end_utc,"
                                       "COUNT(*) samples,MIN(value) min_value,MAX(value) max_value,COUNT(*) OVER () interval_count "
                                       "FROM matches GROUP BY segment ORDER BY start_utc DESC LIMIT 200", args)
                result["intervals"] = [dict(r) for r in intervals]
                result["interval_count"] = result["intervals"][0]["interval_count"] if result["intervals"] else 0
            return result

    def deletion_query(self, query):
        query = criteria(query)
        if query.get("source", [""])[0] not in ("live", "archive", "import"):
            raise ValueError("刪除前須明確選擇資料來源")
        _, _, dates = predicates(query)
        if not all(name in dates for name in ("start", "end")):
            raise ValueError("刪除前須填寫起始與結束時間")
        cutoff = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(timespec="seconds")
        query["start"] = [dates["start"]]
        query["end"] = [min(dates["end"], cutoff)]
        predicates(query)
        return query

    @staticmethod
    def fingerprint(db, where, args):
        digest, count = hashlib.sha256(), 0
        for row in db.execute("SELECT " + COLUMNS + " FROM samples"+where+" ORDER BY "+KEYS, args):
            digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode()+b"\n")
            count += 1
        return digest.hexdigest(), count

    def preview_delete(self, query):
        query = self.deletion_query(query)
        where, args = self.filters(query)
        with self.connect() as db:
            db.execute("BEGIN")
            signature, count = self.fingerprint(db, where, args)
            channels = [dict(r) for r in db.execute("SELECT channel,COUNT(*) count,MIN(timestamp_utc) first_utc,"
                                                    "MAX(timestamp_utc) last_utc FROM samples"+where+" GROUP BY channel", args)]
        return {"query": query, "signature": signature, "count": count, "channels": channels}

    def trash_samples(self, plan):
        where, args = self.filters(plan["query"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            signature, count = self.fingerprint(db, where, args)
            if signature != plan["signature"] or count != plan["count"]:
                raise HistoryConflict("資料已改變，請重新預覽後再刪除")
            if not count:
                raise ValueError("此範圍沒有可刪除的資料")
            batch = uuid.uuid4().hex
            metadata = {k: plan[k] for k in ("query", "count", "channels")}
            metadata.update(id=batch, deleted_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            db.execute("INSERT INTO deleted_samples SELECT ?,"+COLUMNS+" FROM samples"+where, [batch, *args])
            db.execute("DELETE FROM samples"+where, args)
            db.execute("INSERT INTO sample_deletions VALUES (?, ?, NULL)", (batch, json.dumps(metadata, ensure_ascii=False)))
        return metadata

    def trash(self, offset=0):
        offset = max(0, int(offset))
        with self.connect() as db:
            return [{**json.loads(r["metadata"]), "restored_utc": r["restored_utc"]}
                    for r in db.execute("SELECT * FROM sample_deletions ORDER BY rowid DESC LIMIT 30 OFFSET ?", (offset,))]

    def trash_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM sample_deletions").fetchone()[0]

    def restore_samples(self, batch):
        if not isinstance(batch, str) or len(batch) != 32 or any(c not in "0123456789abcdef" for c in batch):
            raise ValueError("invalid batch")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sample_deletions WHERE id=?", (batch,)).fetchone()
            if not row:
                raise ValueError("找不到這批刪除記錄")
            if row["restored_utc"]:
                return {"restored": 0, "already_restored": True}
            result = db.execute("INSERT OR IGNORE INTO samples SELECT "+COLUMNS+" FROM deleted_samples WHERE batch=?", (batch,))
            count = result.rowcount
            db.execute("DELETE FROM deleted_samples WHERE batch=?", (batch,))
            db.execute("UPDATE sample_deletions SET restored_utc=? WHERE id=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), batch))
        return {"restored": count, "already_restored": False}
