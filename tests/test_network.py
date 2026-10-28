import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"network"))
import manager


def controller(tmp_path):
    config = {"hostname": "u30-test", "setup_ssid": "U30-test",
              "setup_key": "test-setup-key", "ap_uuid": "ap-id", "home_uuid": "old-home"}
    return manager.Manager(config, tmp_path/"config.json")


def test_setup_key_required_and_never_in_status(tmp_path):
    service = controller(tmp_path)
    result, status = service.submit({"ssid": "Home", "password": "password", "setup_key": "wrong"})
    assert status == 403 and service.pending.empty() and not service.busy
    assert "test-setup-key" not in json.dumps(service.status())
    for ssid, password in (("", "password"), ("a"*33, "password"), ("bad\nssid", "password"), ("Home", "short")):
        result, status = service.submit({"ssid": ssid, "password": password, "setup_key": "test-setup-key"})
        assert status == 400 and service.pending.empty()
    assert service.submit({"ssid": "家裡 Wi-Fi", "password": "password", "setup_key": "test-setup-key"})[1] == 202
    assert service.submit({"ssid": "Other", "password": "password", "setup_key": "test-setup-key"})[1] == 409


def test_profile_preserves_ssid_bytes_and_uses_known_wpa_key():
    profile = manager.wifi_profile("test", "IEEE", "password")
    assert "psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e" in profile
    assert "ssid=73;69;69;69;" in profile
    assert "password" not in profile
    tricky = "家裡;\\Wi-Fi"
    value = manager.wifi_profile("test", tricky, "safe password").split("ssid=")[1].splitlines()[0]
    assert bytes(int(n) for n in value.split(";") if n).decode() == tricky


def test_failed_wifi_restores_ap_and_preserves_previous_profile(tmp_path):
    service = controller(tmp_path)
    old = tmp_path/"u30-home-old-home.nmconnection"
    old.write_text("previous working credentials")
    service.activate = Mock(return_value=False)
    service.ensure_ap = Mock()
    with patch.object(manager, "PROFILES", tmp_path), patch.object(manager, "command", return_value=SimpleNamespace(returncode=0)) as run:
        service.connect("MissingHome", "password")
    assert service.status()["job"]["state"] == "failed"
    assert service.config["home_uuid"] == "old-home"
    assert old.read_text() == "previous working credentials"
    assert list(tmp_path.glob("*.nmconnection")) == [old]
    service.ensure_ap.assert_called_once()
    assert all("password" not in str(call) for call in run.call_args_list)


def test_successful_wifi_is_committed_before_old_profile_removed(tmp_path):
    service = controller(tmp_path)
    old = tmp_path/"u30-home-old-home.nmconnection"
    old.write_text("previous credentials")
    service.activate = Mock(return_value=True)
    service.device = Mock(return_value={"connected": False})
    service.refresh = Mock()
    service.ensure_ap = Mock()
    with patch.object(manager, "PROFILES", tmp_path), patch.object(manager, "command", return_value=SimpleNamespace(returncode=0)) as run:
        service.connect("Home", "password")
    saved = json.loads((tmp_path/"config.json").read_text())
    assert saved["home_uuid"] != "old-home" and saved["home_ssid"] == "Home"
    assert service.status()["job"]["state"] == "success"
    assert any(call.args[-1] == "old-home" for call in run.call_args_list)
    service.ensure_ap.assert_not_called()
