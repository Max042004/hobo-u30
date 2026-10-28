"""Unprivileged bridge to the separately installed network service."""
import json
import socket


def network_request(action="status", data=None):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(4)
            connection.connect("/run/u30-network/control.sock")
            connection.sendall(json.dumps({**(data or {}), "action": action}, ensure_ascii=False).encode() + b"\n")
            with connection.makefile("rb") as stream:
                raw = stream.readline(16385)
            if len(raw) > 16384:
                raise ValueError("response too large")
            result = json.loads(raw)
            return result["data"], result["status"]
    except (OSError, ValueError, AttributeError):
        if action == "status":
            return {"available": False, "error": "此主機尚未啟用 Pi 網路設定服務"}, 200
        return {"error": "Pi 網路設定服務尚未就緒，請稍後再試"}, 503
