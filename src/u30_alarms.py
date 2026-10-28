"""Native U30 alarm and relay protocol, separate from the OSER logger writer."""
import hashlib
import json
import math
import os
import struct
from datetime import datetime, timezone

from hobo_u30 import HoboProtocolError, parse_sensor_header, convert_reading

LAYOUT = struct.Struct(">BHHIBBBHHBB")
ACTIONS = {"activate": 256, "deactivate": 512, "pulse": 16, "close": 2, "open": 4}
ACTION_NAMES = {v:k for k,v in ACTIONS.items()}
CLEAR = {"activate":512, "close":4, "open":2}
MAX_RULES = 32


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def command(client, target, code, payload=b"", missing_ok=False):
    reply = client.transport.transact(client.host_address, target, code, payload, code+1)
    if not reply.payload:
        raise HoboProtocolError("empty alarm/relay acknowledgement")
    status = reply.payload[0]
    if status == 1 and missing_ok:
        return None
    if status != 0:
        raise HoboProtocolError(f"U30 module {target} command {code} returned status {status}")
    return reply.payload[1:]


def relay_state(client):
    result = command(client, 8, 7)
    if len(result) != 1 or result[0] not in (0,1):
        raise HoboProtocolError("invalid relay state")
    return "open" if result[0] == 0 else "closed"


def descriptor(channel):
    return parse_sensor_header(channel["instrument"], bytes.fromhex(channel["header_hex"]))


def decode_rule(raw, sensors, number=0):
    if len(raw) != 18:
        raise HoboProtocolError("alarm definition must contain 18 bytes")
    slot, low, high, serial, sensor_type, enabled, check, action, clear, samples, flag = LAYOUT.unpack(raw)
    channel = next((c for c in sensors if c["serial"] == str(serial) and c["instrument"] == slot), None)
    result = {"number": number, "raw_hex":raw.hex(), "serial":str(serial), "instrument":slot,
              "low_raw":low, "high_raw":high, "enabled":bool(enabled), "sample_count":samples,
              "action":ACTION_NAMES.get(action,"unknown"), "action_code":action,"clear_code":clear,
              "auto_clear":bool(clear), "check_code":check,"sensor_type":sensor_type,"flag":flag,
              "channel":channel["key"] if channel else None, "low":None,"high":None}
    if channel:
        desc = descriptor(channel)
        positive = convert_reading(0, desc)
        other = convert_reading(1, desc)
        if positive is not None and other is not None:
            ascending = other > positive
            if check in (2,3):
                result["low" if ascending else "high"] = convert_reading(low, desc)
            if check in (1,3):
                result["high" if ascending else "low"] = convert_reading(high, desc)
        result.update(label=channel.get("label") or channel["measurement"], units=channel["units"])
    return result


def read_configuration(client, sensors):
    settings = command(client,8,3)
    if len(settings) != 5 or settings[0] not in (0,1):
        raise HoboProtocolError("invalid relay configuration")
    raws = []
    for number in range(1,255):
        raw = command(client,7,11,bytes([number]),missing_ok=True)
        if raw is None:
            break
        if len(raw) != 18:
            raise HoboProtocolError("truncated alarm definition")
        raws.append(raw)
    else:
        raise HoboProtocolError("alarm list has no terminating response")
    result = {"observed_utc":stamp(), "normally":"open" if settings[0] == 0 else "closed",
              "pulse_ms":int.from_bytes(settings[1:],"big")*10,
              "current":relay_state(client), "rules":[decode_rule(raw,sensors,i+1) for i,raw in enumerate(raws)]}
    result["signature"] = signature(result)
    return result


def signature(configuration):
    value = {"normally":configuration["normally"],"pulse_ms":configuration["pulse_ms"],
             "rules":[r["raw_hex"] for r in configuration["rules"]]}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def closest_raw(value, channel):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError("警報門檻必須是有限數值")
    desc = descriptor(channel)
    if not desc.sample_bits or not 1 <= desc.sample_bits <= 16:
        raise ValueError("此通道尚未支援警報門檻換算")
    maximum = (1 << desc.sample_bits)-3  # Never use EOF/missing sentinels.
    a,b = convert_reading(0,desc),convert_reading(maximum,desc)
    if a is None or b is None or a == b or not all(math.isfinite(x) for x in (a,b)):
        raise ValueError("此通道尚未支援可逆的工程值換算")
    if value < min(desc.lower,desc.upper) or value > max(desc.lower,desc.upper):
        raise ValueError("警報門檻超出此感測器量測範圍")
    ascending = b > a
    lo,hi = 0,maximum
    while lo < hi:
        mid = (lo+hi)//2
        actual = convert_reading(mid,desc)
        if (actual < value) == ascending:
            lo = mid+1
        else:
            hi = mid
    candidates = {lo,max(0,lo-1)}
    # HOBOware selects the higher raw bin on an exact halfway tie.
    raw = min(candidates,key=lambda n:(abs(convert_reading(n,desc)-value),-n))
    return raw,convert_reading(raw,desc),ascending


def build_rule(data, sensors):
    if not isinstance(data,dict):
        raise ValueError("警報設定格式無效")
    channel = next((c for c in sensors if c["key"] == data.get("channel")),None)
    if not channel:
        raise ValueError("警報通道已不存在，請重新讀取")
    count,action = data.get("sample_count"),data.get("action")
    if type(count) is not int or not 1 <= count <= 255 or action not in ACTIONS:
        raise ValueError("連續樣本數須為1～255，且須選擇支援的動作")
    low,high = data.get("low"),data.get("high")
    if any(x is not None and (isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x)) for x in (low,high)):
        raise ValueError("警報門檻必須是有限數值")
    if low is None and high is None:
        raise ValueError("至少須設定高警報或低警報")
    if low is not None and high is not None and low >= high:
        raise ValueError("低警報須小於高警報")
    raw_low=raw_high=0
    actual_low=actual_high=None
    ascending=True
    if low is not None:
        raw_low,actual_low,ascending=closest_raw(low,channel)
    if high is not None:
        raw_high,actual_high,ascending=closest_raw(high,channel)
    if low is not None and high is not None and raw_low == raw_high:
        raise ValueError("高低門檻換算後相同，請擴大間距")
    check = 3 if low is not None and high is not None else 2 if low is not None else 1
    if not ascending:
        raw_low,raw_high = raw_high,raw_low
        check = {1:2,2:1,3:3}[check]
    auto = data.get("auto_clear") is True
    if auto and action not in CLEAR:
        raise ValueError("此動作不提供條件恢復後反向解除")
    raw = LAYOUT.pack(channel["instrument"],raw_low,raw_high,int(channel["serial"]),0,
                      int(data.get("enabled",True) is True),check,ACTIONS[action],CLEAR[action] if auto else 0,count,0)
    result=decode_rule(raw,sensors)
    result.update(requested_low=low,requested_high=high)
    return result


def preview_configuration(data, snapshot):
    if not isinstance(data,dict) or data.get("normally") not in ("open","closed"):
        raise ValueError("請選擇常開或常閉")
    pulse=data.get("pulse_ms")
    if type(pulse) is not int or not 10 <= pulse <= 42949672950 or pulse%10:
        raise ValueError("脈衝寬度須為10毫秒的整數倍，範圍10～42949672950毫秒")
    rules=data.get("rules")
    if not isinstance(rules,list) or len(rules)>MAX_RULES:
        raise ValueError("此介面每次最多設定32條警報")
    baseline=snapshot.get("alarms")
    if not baseline or baseline.get("error"):
        raise ValueError("尚未完整讀取 U30 原有警報設定")
    preserved=[r["raw_hex"] for r in baseline["rules"]]
    built=[]
    for rule in rules:
        if not isinstance(rule,dict):
            raise ValueError("警報格式無效")
        if "preserve_hex" in rule:
            raw=rule["preserve_hex"]
            if raw not in preserved:
                raise ValueError("保留的警報已變更，請重新載入")
            preserved.remove(raw)
            built.append(decode_rule(bytes.fromhex(raw),snapshot["sensors"]))
        else:
            built.append(build_rule(rule,snapshot["sensors"]))
    return {"normally":data["normally"],"pulse_ms":pulse,"rules":built,"serial":snapshot["serial"],
            "deployment":snapshot["archive"]["deployment"],"baseline_signature":baseline["signature"],
            "sensors":{c["key"]:c["header_hex"] for c in snapshot["sensors"]},"created_utc":stamp()}


def audit_write(path,value):
    temporary=path.with_suffix(".tmp")
    with temporary.open("w",encoding="utf-8") as target:
        json.dump(value,target,ensure_ascii=False,indent=2)
        target.flush();os.fsync(target.fileno())
    temporary.replace(path)


def apply_configuration(directory, plan):
    from station import session,read_header,descriptor_channel
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-alarms.json")
    with session() as client:
        header,info=read_header(client)
        if info["serial"]!=plan["serial"] or info["deployment"]!=plan["deployment"]:
            raise HoboProtocolError("U30 或部署已變更，未寫入警報")
        sensors=[descriptor_channel(client.descriptor(slot),info) for slot in client.attached_instruments()]
        if {c["key"]:c["header_hex"] for c in sensors}!=plan["sensors"]:
            raise HoboProtocolError("感測器描述已變更，未寫入警報")
        baseline=read_configuration(client,sensors)
        if baseline["signature"]!=plan["baseline_signature"]:
            raise HoboProtocolError("U30 原有警報已變更，請重新預覽")
        audit={"created_utc":stamp(),"plan":plan,"before":baseline,"phase":"backed_up"}
        audit_write(path,audit)
        try:
            audit["phase"]="replacing";audit_write(path,audit)
            # HOBOware rebuilds this module's list; it does not erase logged data.
            command(client,7,3)
            # Establish the final output configuration before adding enabled
            # rules, so a rule cannot pulse using the previous pulse width.
            command(client,8,1,bytes([0 if plan["normally"]=="open" else 1]))
            command(client,8,9,(plan["pulse_ms"]//10).to_bytes(4,"big"))
            for number,rule in enumerate(plan["rules"],1):
                assigned=command(client,7,1,bytes.fromhex(rule["raw_hex"]))
                if not assigned or assigned[0]!=number:
                    raise HoboProtocolError("unexpected assigned alarm number")
            after=read_configuration(client,sensors)
            expected={**plan,"rules":plan["rules"]}
            if after["signature"]!=signature(expected):
                raise HoboProtocolError("警報或繼電器設定讀回不符")
            audit.update(phase="verified",after=after)
            audit_write(path,audit)
            return audit
        except Exception as exc:
            audit.update(phase="failed",error=str(exc));audit_write(path,audit)
            raise HoboProtocolError(str(exc)+"；設定可能部分完成，請重新讀取核對，不會自動重送") from exc


def manual_action(directory, action, expected_serial):
    from station import session,read_header
    if action not in ("open","closed"):
        raise ValueError("只支援明確斷開或閉合")
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-relay.json")
    with session() as client:
        _,info=read_header(client)
        if info["serial"]!=expected_serial:
            raise HoboProtocolError("U30 序號已變更")
        audit={"created_utc":stamp(),"serial":expected_serial,"action":action,"before":relay_state(client),"phase":"requested"}
        audit_write(path,audit)
        try:
            command(client,8,5,bytes([0 if action=="open" else 1]))
            after=relay_state(client)
            if after!=action:
                raise HoboProtocolError("接點狀態未符合要求；現有警報也可能改變接點")
            audit.update(phase="verified",after=after)
            audit_write(path,audit)
            return audit
        except Exception as exc:
            audit.update(phase="failed",error=str(exc));audit_write(path,audit)
            raise
