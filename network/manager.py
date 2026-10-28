"""Small, root-owned NetworkManager controller for the U30 setup portal.

Only this process changes Wi-Fi. The unprivileged dashboard talks to a
group-restricted Unix socket; network changes also require the setup key.
"""
import hashlib
import configparser
import hmac
import io
import json
import logging
import os
from pathlib import Path
import queue
import re
import socketserver
import subprocess
import threading
import time
import uuid

LOG = logging.getLogger("u30-network")
CONFIG = Path("/etc/u30-network/config.json")
SOCKET = "/run/u30-network/control.sock"
PROFILES = Path("/etc/NetworkManager/system-connections")


def command(*args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "LC_ALL": "C"})


def valid_wifi(ssid, password):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("Wi-Fi 名稱須為 1～32 bytes，且不可含控制字元")
    if not isinstance(password, str) or not (re.fullmatch(r"[ -~]{8,63}", password) or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise ValueError("Wi-Fi 密碼須為 8～63 個英數或符號，或 64 位十六進位金鑰")
    return ssid, password


def wifi_profile(identifier, ssid, password):
    # Use a byte array for arbitrary SSIDs and a derived PSK so keyfile string
    # escapes cannot alter user credentials. Never put a password in argv.
    valid_wifi(ssid, password)
    psk = password if len(password) == 64 else hashlib.pbkdf2_hmac(
        "sha1", password.encode("ascii"), ssid.encode("utf-8"), 4096, 32).hex()
    encoded = ";".join(str(b) for b in ssid.encode("utf-8")) + ";"
    return (f"[connection]\nid=u30-home-{identifier[:8]}\nuuid={identifier}\n"
            "type=wifi\ninterface-name=wlan0\nautoconnect=false\nautoconnect-priority=100\n"
            f"[wifi]\nmode=infrastructure\nssid={encoded}\n"
            f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={psk}\n"
            "[ipv4]\nmethod=auto\n[ipv6]\nmethod=auto\n")


def sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def private_write(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    sync_directory(path.parent)


class Manager:
    def __init__(self, config, config_path=CONFIG):
        self.config = config
        self.config_path = config_path
        self.lock = threading.Lock()
        self.pending = queue.Queue(maxsize=1)
        self.busy = False
        self.state = {"available": True, "mode": "starting", "interfaces": [],
                      "hostname_url": f"http://{config['hostname']}.local:8080/",
                      "setup_ssid": config["setup_ssid"], "setup_url": "http://10.42.0.1:8080/",
                      "setup_active": False, "job": None}

    @property
    def password_journal(self):
        return self.config_path.parent / 'password-change.json'

    def recover_password_change(self):
        """An interrupted two-file commit rolls back before accepting requests."""
        journal = self.password_journal
        if not journal.exists():
            return
        previous = json.loads(journal.read_text(encoding='utf-8'))
        if previous.get('phase') == 'committed':
            # Both files were fsynced and NM readback passed before this marker.
            self.config = json.loads(self.config_path.read_text(encoding='utf-8'))
            journal.unlink()
            sync_directory(journal.parent)
            return
        profile_path = PROFILES / 'u30-setup.nmconnection'
        private_write(profile_path, previous['profile'])
        if command('/usr/bin/nmcli', 'connection', 'load', str(profile_path)).returncode:
            raise RuntimeError('密碼設定復原失敗；保留原始復原資料')
        private_write(self.config_path, previous['config'])
        self.config = json.loads(previous['config'])
        journal.unlink()
        sync_directory(journal.parent)

    def submit_password(self, data):
        if not self.authorized(data):
            return {'error': '目前 Pi 設定密碼不正確'}, 403
        password = data.get('new_password')
        if not isinstance(password, str) or not re.fullmatch(r'[ -~]{8,63}', password):
            return {'error': '新密碼須為 8～63 個英文字母、數字或符號'}, 400
        if password != data.get('confirm_password'):
            return {'error': '兩次輸入的新密碼不一致'}, 400
        if hmac.compare_digest(password.encode(), self.config['setup_key'].encode()):
            return {'error': '新密碼不可與目前密碼相同'}, 400
        if data.get('confirmed') is not True:
            return {'error': '請確認同步變更 Pi 設定與熱點密碼'}, 400
        with self.lock:
            if self.busy:
                return {'error': '正在更新網路或密碼，請稍後再試'}, 409
            self.busy = True
            self.state['job'] = {'kind': 'password', 'state': 'changing_password',
                                 'message': '密碼更新中；使用熱點時，請稍後以新密碼重新連線。'}
        self.pending.put(('password', password))
        return {'accepted': True}, 202

    def change_password(self, password):
        """Commit a verified NM profile and portal key; never put secrets in argv."""
        committed = False
        profile_path = PROFILES / 'u30-setup.nmconnection'
        try:
            if self.password_journal.exists():
                raise RuntimeError('有尚未復原的密碼變更，請先重新啟動網路設定服務')
            original_config = self.config_path.read_text(encoding='utf-8')
            original_profile = profile_path.read_text(encoding='utf-8')
            profile = configparser.ConfigParser(interpolation=None)
            profile.read_string(original_profile)
            if (profile.get('connection', 'uuid') != self.config['ap_uuid'] or
                    profile.get('wifi', 'mode') != 'ap' or
                    profile.get('wifi-security', 'key-mgmt') != 'wpa-psk'):
                raise RuntimeError('設定熱點配置無法確認，未修改密碼')
            ssid = profile.get('wifi', 'ssid')
            if re.fullmatch(r'(?:[0-9]{1,3};)+', ssid):
                ssid = bytes(int(value) for value in ssid.split(';') if value).decode('utf-8')
            if ssid != self.config['setup_ssid']:
                raise RuntimeError('設定熱點名稱無法確認，未修改密碼')
            psk = hashlib.pbkdf2_hmac('sha1', password.encode('ascii'),
                                     self.config['setup_ssid'].encode('utf-8'), 4096, 32).hex()
            profile['wifi-security']['psk'] = psk
            contents = io.StringIO()
            profile.write(contents)
            updated = {**self.config, 'setup_key': password}
            recovery = {'phase': 'prepared', 'config': original_config, 'profile': original_profile}
            private_write(self.password_journal, json.dumps(recovery, ensure_ascii=False))
            private_write(profile_path, contents.getvalue())
            if command('/usr/bin/nmcli', 'connection', 'load', str(profile_path)).returncode:
                raise RuntimeError('無法載入熱點密碼，已還原原設定')
            # This output is captured only for comparison, never logged/returned.
            checked = command('/usr/bin/nmcli', '--show-secrets', '-g',
                              '802-11-wireless-security.psk', 'connection', 'show', 'uuid', self.config['ap_uuid'])
            if checked.returncode or not hmac.compare_digest(checked.stdout.strip(), psk):
                raise RuntimeError('熱點密碼核對失敗，已還原原設定')
            private_write(self.config_path, json.dumps(updated, ensure_ascii=False))
            private_write(self.password_journal, json.dumps({**recovery, 'phase': 'committed'}, ensure_ascii=False))
            with self.lock:
                self.config = updated
            committed = True
        except Exception:
            # Restore both files. Keep the journal if rollback itself fails so
            # startup recovery can finish it before serving authorization.
            try:
                self.recover_password_change()
            except Exception:
                LOG.warning('Password rollback deferred to network service restart')
            self.update(job={'kind': 'password', 'state': 'failed',
                             'message': '密碼更新未完成，未確認變更；請使用原密碼，若仍無法登入請聯絡管理者。'})
        if not committed:
            return
        try:
            self.password_journal.unlink()
            sync_directory(self.password_journal.parent)
        except OSError:
            LOG.warning('Committed password journal will be cleaned up on restart')
        self.update(job={'kind': 'password', 'state': 'success',
                         'message': '密碼已更新。Pi 設定與熱點請使用新密碼；原交付卡不會自動更新。'})
        try:
            wifi = self.device('wlan0')
            if wifi['connected'] and wifi['uuid'] == self.config['ap_uuid']:
                command('/usr/bin/nmcli', '--wait', '10', 'connection', 'down', 'uuid', self.config['ap_uuid'])
                self.ensure_ap()
        except Exception:
            # The new key is already committed. A temporary AP restart failure
            # must not falsely tell the user that the old password still works.
            LOG.warning('Password committed; hotspot recovery will retry')
            self.update(job={'kind': 'password', 'state': 'success',
                             'message': '密碼已更新，熱點正在恢復連線；請稍後以新密碼重新連線。'})

    def status(self):
        with self.lock:
            return {**json.loads(json.dumps(self.state)), 'busy': self.busy}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def submit(self, data):
        if not self.authorized(data):
            return {"error": "設定密碼不正確（請輸入 Pi 設定熱點的密碼）"}, 403
        try:
            ssid, password = valid_wifi(data.get("ssid"), data.get("password"))
        except ValueError as exc:
            return {"error": str(exc)}, 400
        with self.lock:
            if self.busy:
                return {"error": "正在切換網路，請稍後再試"}, 409
            self.busy = True
            self.state["job"] = {"state": "connecting", "ssid": ssid,
                                 "message": "正在套用 Wi-Fi；設定熱點將暫時中斷。失敗時會自動恢復。"}
        self.pending.put(('connect', ssid, password))
        return {"accepted": True}, 202

    def authorized(self, data):
        key = data.get("setup_key", "")
        return isinstance(key, str) and hmac.compare_digest(key.encode(), self.config["setup_key"].encode())

    def device(self, name):
        result = command("/usr/bin/nmcli", "-g", "GENERAL.STATE,GENERAL.CON-UUID,GENERAL.CONNECTION", "device", "show", name)
        lines = result.stdout.splitlines()
        connected = bool(lines and lines[0].startswith("100"))
        return {"connected": connected, "uuid": lines[1] if len(lines) > 1 else "",
                "connection": lines[2] if len(lines) > 2 else ""}

    def refresh(self):
        eth, wifi = self.device("eth0"), self.device("wlan0")
        ap = wifi["connected"] and wifi["uuid"] == self.config["ap_uuid"]
        addresses = json.loads(command("/usr/sbin/ip", "-j", "-4", "address", "show").stdout)
        interfaces = []
        for item in addresses:
            if item["ifname"] not in ("eth0", "wlan0"):
                continue
            for address in item.get("addr_info", []):
                if address["scope"] == "global":
                    interfaces.append({"name": item["ifname"], "ip": address["local"],
                                       "url": f"http://{address['local']}:8080/",
                                       "kind": "setup" if item["ifname"] == "wlan0" and ap else "ethernet" if item["ifname"] == "eth0" else "wifi"})
        self.update(mode="setup" if ap else "wifi" if wifi["connected"] else "ethernet" if eth["connected"] else "offline",
                    interfaces=interfaces, setup_active=ap,
                    wifi_connection=wifi["connection"] if wifi["connected"] and not ap else None,
                    updated_at=time.time())
        return eth, wifi, ap

    def dhcp(self, start):
        result = command("/usr/bin/systemctl", "start" if start else "stop", "u30-setup-dhcp.service")
        if result.returncode:
            raise RuntimeError("設定熱點 DHCP 服務未啟動" if start else "無法停止設定熱點 DHCP")

    def ensure_ap(self):
        wifi = self.device("wlan0")
        if not (wifi["connected"] and wifi["uuid"] == self.config["ap_uuid"]):
            self.dhcp(False)
            result = command("/usr/bin/nmcli", "--wait", "20", "connection", "up", "uuid", self.config["ap_uuid"], timeout=25)
            if result.returncode:
                raise RuntimeError("無法啟動設定熱點")
        self.dhcp(True)
        self.refresh()

    def activate(self, identifier):
        current = self.status()
        self.update(mode="connecting", setup_active=False,
                    interfaces=[i for i in current["interfaces"] if i["name"] != "wlan0"])
        self.dhcp(False)
        result = command("/usr/bin/nmcli", "--wait", "25", "connection", "up", "uuid", identifier, timeout=32)
        wifi = self.device("wlan0")
        return not result.returncode and wifi["connected"] and wifi["uuid"] == identifier

    def saved_wifi(self):
        result = command("/usr/bin/nmcli", "-t", "-f", "UUID,TYPE", "connection", "show")
        identifiers = [line.split(":")[0] for line in result.stdout.splitlines()
                       if line.endswith(":802-11-wireless") and not line.startswith(self.config["ap_uuid"] + ":")]
        preferred = self.config.get("home_uuid")
        if preferred in identifiers:
            identifiers.remove(preferred)
            identifiers.insert(0, preferred)
        # Uncommitted trial profiles have autoconnect=false; never retry those
        # after a power interruption. Existing user profiles remain intact.
        return [item for item in identifiers if command("/usr/bin/nmcli", "-g", "connection.autoconnect", "connection", "show", "uuid", item).stdout.strip() == "yes"]

    def connect(self, ssid, password):
        identifier = str(uuid.uuid4())
        path = PROFILES / f"u30-home-{identifier}.nmconnection"
        success = False
        try:
            private_write(path, wifi_profile(identifier, ssid, password))
            loaded = command("/usr/bin/nmcli", "connection", "load", str(path))
            if loaded.returncode or not self.activate(identifier):
                raise RuntimeError("Wi-Fi 連線失敗，已恢復設定熱點。請確認名稱、密碼與訊號後再試。")
            # Commit only after NetworkManager obtained an address. Retain all
            # pre-existing profiles; remove only our previously successful one.
            modified = command("/usr/bin/nmcli", "connection", "modify", "uuid", identifier, "connection.autoconnect", "yes")
            if modified.returncode:
                raise RuntimeError("Wi-Fi 設定無法保存，請重試。")
            previous = self.config.get("home_uuid")
            self.config["home_uuid"] = identifier
            self.config["home_ssid"] = ssid
            private_write(self.config_path, json.dumps(self.config, ensure_ascii=False))
            success = True
            if previous and previous != identifier and (PROFILES / f"u30-home-{previous}.nmconnection").exists():
                command("/usr/bin/nmcli", "connection", "delete", "uuid", previous)
            self.refresh()
            wifi_ips = [i["ip"] for i in self.status()["interfaces"] if i["kind"] == "wifi"]
            self.update(job={"state": "success", "ssid": ssid, "ip": wifi_ips,
                             "message": "Wi-Fi 已連線並保存。手機切回家用網路後，可使用固定網址開啟感測站。"})
        except Exception as exc:
            # Do not log subprocess output, which can contain connection details.
            LOG.warning("Wi-Fi configuration did not complete (%s)", type(exc).__name__)
            self.update(job={"state": "failed", "ssid": ssid,
                             "message": str(exc) if isinstance(exc, RuntimeError) else "Wi-Fi 連線失敗，正在恢復設定熱點。"})
        finally:
            if not success:
                command("/usr/bin/nmcli", "connection", "delete", "uuid", identifier)
                path.unlink(missing_ok=True)
            if not success or self.device("eth0")["connected"]:
                self.ensure_ap()
            else:
                self.refresh()

    def run(self):
        previous_eth = None
        attempted = False
        disconnected_since = None
        while True:
            try:
                eth, wifi, ap = self.refresh()
                try:
                    pending = self.pending.get_nowait()
                except queue.Empty:
                    pass
                else:
                    # Return the HTTP response before disconnecting its AP.
                    time.sleep(2)
                    try:
                        if pending[0] == 'password':
                            self.change_password(pending[1])
                        else:
                            self.connect(pending[1], pending[2])
                    finally:
                        with self.lock:
                            self.busy = False
                    attempted = True
                    previous_eth = eth["connected"]
                    continue
                if eth["connected"]:
                    self.ensure_ap()
                    attempted = False
                    disconnected_since = None
                else:
                    if previous_eth:
                        attempted = False
                    if wifi["connected"] and not ap:
                        self.dhcp(False)
                        disconnected_since = None
                    elif not attempted:
                        attempted = True
                        self.update(mode="connecting")
                        if not any(self.activate(item) for item in self.saved_wifi()):
                            self.ensure_ap()
                        self.refresh()
                    elif ap:
                        self.dhcp(True)
                    else:
                        disconnected_since = disconnected_since or time.monotonic()
                        if time.monotonic() - disconnected_since >= 15:
                            self.ensure_ap()
                previous_eth = eth["connected"]
            except Exception as exc:
                LOG.warning("Network recovery retry (%s)", type(exc).__name__)
                self.update(error="網路設定服務正在重試；請稍候。")
            else:
                self.update(error=None)
            time.sleep(3)


class Request(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(4097)
            if len(raw) > 4096 or not raw.endswith(b"\n"):
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            action = data.pop("action", "status")
            if action == "status":
                result, status = self.server.manager.status(), 200
            elif action == "connect":
                result, status = self.server.manager.submit(data)
            elif action == 'password':
                result, status = self.server.manager.submit_password(data)
            elif action == "authorize_data":
                ok = self.server.manager.authorized(data)
                result, status = ({"authorized": True}, 200) if ok else ({"error": "Pi 設定密碼不正確"}, 403)
            else:
                result, status = {"error": "unknown action"}, 400
        except (ValueError, TimeoutError):
            result, status = {"error": "invalid request"}, 400
        self.wfile.write(json.dumps({"status": status, "data": result}, ensure_ascii=False).encode() + b"\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manager = Manager(json.loads(CONFIG.read_text()))
    manager.recover_password_change()
    Path(SOCKET).unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(SOCKET, Request) as server:
        server.daemon_threads = True
        server.manager = manager
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=manager.run, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
