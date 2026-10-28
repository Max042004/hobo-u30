"""Explicit U30 deployment writes, derived from HOBOware 3.7.28 OLaunch.

Never called by polling. A complete verified pre-write backup is mandatory.
"""
import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from hobo_u30 import (U30Client, HidU30Transport, HoboProtocolError, encode_oser,
                      decode_oser, LOCAL_SENSOR, HT_OSER, HT_OSER_REPLY)
from pi_collection import seconds
from u30_data import EPOCH, tlvs


def validate_settings(data, clock=None):
    if not isinstance(data, dict):
        raise ValueError("U30 設定格式無效")
    clock = time.time() if clock is None else clock
    interval = seconds(data.get("interval"), "U30 共用記錄間隔")
    mode = data.get("start_mode")
    if mode not in ("immediate", "aligned", "button", "delayed"):
        raise ValueError("U30 啟動方式無效")
    memory = data.get("memory_mode")
    if memory not in ("stop", "wrap"):
        raise ValueError("U30 記憶體模式無效")
    sample = data.get("sample_interval", 0)
    if type(sample) is not int or not 0 <= sample <= 255 or (sample and (sample > interval or interval % sample)):
        raise ValueError("U30 平均取樣間隔須為 0～255 秒且能整除記錄間隔；0 為停用")
    if sample and data.get("averaging_supported_confirmed") is not True:
        raise ValueError("請先確認感測器型號支援 U30 原生平均量測")
    start_at = None
    if mode == "delayed":
        try:
            date = datetime.fromisoformat(data.get("start_at", "").replace("Z", "+00:00"))
            if date.utcoffset() is None or date.timestamp() <= clock+10:
                raise ValueError()
            start_at = date.isoformat(timespec="seconds")
        except (ValueError, TypeError):
            raise ValueError("U30 延遲時間須在未來，且預留完整備份所需時間")
    name = data.get("name", "")
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 40 or any(ord(c) < 32 for c in name):
        raise ValueError("U30 名稱須為 1～40 bytes 的文字")
    return {"interval": interval, "start_mode": mode, "start_at": start_at, "memory_mode": memory,
            "sample_interval": sample, "averaging_supported_confirmed": bool(sample), "name": name}


def launch_patch(header, config, clock):
    """Return bounded B-command patch matching OLaunch's LAUNCH_PARAM_DATA.

    Keep all user notes/scaling/location tags. Firmware F builds the new sensor
    header from attached sensors; never copy the old deployment's channel list.
    """
    from u30_data import memory_layout
    info = memory_layout(header)
    entries = [(value, start, end) for tag, value, start, end in tlvs(header[:413], 4) if tag == 30]
    if len(entries) != 1:
        raise HoboProtocolError("ambiguous launch parameter tag")
    old, start, end = entries[0]
    if len(old) < 34 or not 413 <= info["sensor_start"] <= info["data_start"] <= len(header):
        raise HoboProtocolError("unsupported launch header layout")
    name = config["name"].encode("utf-8")+b"\0"
    if len(name) > len(old)-33:
        raise ValueError("裝置配置的名稱欄位空間不足")
    rtc = int(clock)-EPOCH
    if not 0 <= rtc <= 0xffffffff:
        raise ValueError("RTC outside U30 epoch")
    mode = config["start_mode"]
    delay = 0 if mode == "button" else 1
    if mode == "aligned":
        # Allow the RTC-set settling time before the chosen boundary.
        target = (math.floor((clock+4+28800)/config["interval"])+1)*config["interval"]-28800
        delay = int(target)-int(clock)
    elif mode == "delayed":
        delay = int(datetime.fromisoformat(config["start_at"]).timestamp()-int(clock))
        if delay <= 4:
            raise ValueError("指定時間已過或太接近，請重新設定")
    value = bytearray(len(old))
    value[0:4] = rtc.to_bytes(4, "big")
    value[8:12] = (rtc+delay).to_bytes(4, "big")
    value[12:14] = config["interval"].to_bytes(2, "big")
    value[14] = config["sample_interval"]
    value[15] = int(mode == "button")
    value[16] = int(config["memory_mode"] == "wrap")
    value[17] = old[17]  # Preserve the station's battery/state capability flag.
    value[18:21] = info["sensor_start"].to_bytes(3, "big")
    value[28] = 0  # SI units
    value[29:33] = (480).to_bytes(4, "big")
    value[33:33+len(name)] = name
    return end-len(old), bytes(value), rtc


class U30Writer(U30Client):
    def command(self, command, payload=b""):
        if command not in (0x42, 0x44, 0x46, 0x4c) or self.host_address is None:
            raise ValueError("write operation not permitted")
        reply = self.transport.transact(self.host_address, LOCAL_SENSOR, HT_OSER,
                                        encode_oser(command, payload), HT_OSER_REPLY)
        response = decode_oser(reply.payload)
        if response.command != command ^ 255:
            raise HoboProtocolError("unexpected write acknowledgement")
        return response

    def stop_logging(self):
        self.command(0x44)
        if self._oser(0x4f).payload[:1] != b"\0":
            raise HoboProtocolError("U30 did not confirm stopped state")


@contextmanager
def writer_session():
    client = U30Writer(HidU30Transport())
    client.open()
    try:
        yield client
    finally:
        client.close()


def apply_deployment(directory, config, expected_serial, progress, expected_channels=None, expected_deployment=None,
                     allow_raw_recovery=False):
    from station import backup, read_header, read_memory, descriptor_channel, now
    # This always reads a new complete image immediately before changing U30.
    path, manifest = backup(directory, progress)
    image = path.read_bytes()
    if len(image) != 524288 or manifest["sha256"] != hashlib.sha256(image).hexdigest():
        raise HoboProtocolError("pre-write backup failed verification")
    if manifest["serial"] != expected_serial:
        raise HoboProtocolError("U30 serial changed; deployment cancelled")
    if not manifest["stable_header"]:
        raise HoboProtocolError("備份期間 U30 記憶體有變更；請先選擇停止並下載，再重新預覽部署")
    if manifest.get("status_before_hex", "00") != "00" or manifest.get("status_after_hex", "00") != "00":
        raise HoboProtocolError("U30 仍在記錄或等待啟動；請先選擇停止並下載，再重新預覽部署")
    if manifest.get("raw_recovery"):
        if allow_raw_recovery is not True:
            raise HoboProtocolError("完整原始備份已保留；指標異常，請重新預覽並確認使用原始備份重新部署")
        if not (manifest.get("complete_image") and manifest.get("recovery_verified") and manifest.get("memory_verified_twice")):
            raise HoboProtocolError("原始記憶體備份未通過兩次逐位元組核對；未重新部署 U30")
    audit_path = path.with_suffix(".deployment.json")
    audit = {"created_utc": now(), "configuration": config, "backup": path.name,
             "sha256": manifest["sha256"], "serial": expected_serial, "phase": "backed_up",
             "raw_recovery": bool(manifest.get("raw_recovery")),
             "historical_bounds_verified": bool(manifest.get("consistent_snapshot"))}
    def record(phase, **extra):
        audit.update(phase=phase, **extra)
        temporary = audit_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as target:
            target.write(json.dumps(audit, ensure_ascii=False, indent=2))
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(audit_path)
    record("backed_up")
    try:
        with writer_session() as client:
            header, info = read_header(client)
            if info["serial"] != expected_serial:
                raise HoboProtocolError("device changed after backup")
            if header != image[:len(header)]:
                raise HoboProtocolError("U30 header changed after backup; deployment cancelled")
            if expected_deployment is not None and info["deployment"] != expected_deployment:
                raise HoboProtocolError("U30 deployment changed after preview")
            descriptors = [client.descriptor(slot) for slot in client.attached_instruments()]
            if expected_channels is not None and sorted(descriptor_channel(d, info)["key"] for d in descriptors) != sorted(expected_channels):
                raise HoboProtocolError("attached sensors changed after preview")
            if config["interval"] < 2 and any("TRMS" in d.model for d in descriptors):
                raise ValueError("FlexSmart TRMS 需要至少 2 秒記錄間隔")
            # Validate everything before the first stop/write command.
            config = validate_settings(config)
            offset, value, rtc = launch_patch(header, config, time.time())
            record("stopping")
            client.stop_logging()
            record("writing", launch_offset=offset, launch_hex=value.hex())
            client.command(0x4c, rtc.to_bytes(4, "big"))
            time.sleep(2)
            for pos in range(0, len(value), 10):
                chunk = value[pos:pos+10]
                client.command(0x42, (offset+pos).to_bytes(3, "big")+bytes([len(chunk)])+chunk)
                if read_memory(client, offset+pos, len(chunk)) != chunk:
                    raise HoboProtocolError("launch parameter read-back mismatch; F was not sent")
            record("launching")
            client.command(0x46, b"\0")
            time.sleep(1)
            status = client._oser(0x4f).payload
            after_header, after = read_header(client)
            expected_status = 3 if config["start_mode"] == "button" else 5 if config["start_mode"] in ("aligned", "delayed") else 1
            if not status or status[0] not in ({1, 5} if expected_status == 5 else {expected_status}):
                raise HoboProtocolError("U30 launch status verification failed: "+status.hex())
            if (after["logging_interval_seconds"] != config["interval"] or
                    after["wrap_enabled"] != (config["memory_mode"] == "wrap") or
                    after["sampling_interval_seconds"] != config["sample_interval"]):
                raise HoboProtocolError("U30 configuration read-back mismatch")
            if expected_channels is not None and sorted(c['key'] for c in after['channels']) != sorted(expected_channels):
                raise HoboProtocolError('U30 launched channel list does not match the verified sensor list')
            try:
                # U30's OLoggerUF769.postLaunchProcessing also synchronizes
                # the system manager clock, separately from local OSER RTC.
                manager_rtc = int(time.time())-EPOCH
                client.transport.transact(client.host_address, 0, 9, manager_rtc.to_bytes(4, "big"), 10)
            except Exception as exc:
                audit["system_clock_error"] = str(exc)
            record("verified", status=status.hex(), archive=after)
            return audit
    except Exception as exc:
        failed_after = audit["phase"]
        record("failed", failed_after_phase=failed_after, error=str(exc))
        # Never automatically launch again or replay partially completed writes.
        if failed_after in ("stopping", "writing", "launching"):
            raise HoboProtocolError(f"{exc}（作業階段 {failed_after}；U30 狀態可能已變更，請核對後再操作）") from exc
        raise
