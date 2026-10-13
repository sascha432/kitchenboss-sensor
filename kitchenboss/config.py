"""Local configuration / credential cache for the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .api import Device

DEFAULT_PATH = Path(os.environ.get("KITCHENBOSS_CONFIG", Path.home() / ".kitchenboss" / "config.json"))


class Config:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}
        else:
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    # auth ----------------------------------------------------------------- #
    @property
    def token(self) -> str:
        return self.data.get("token", "")

    @property
    def refresh_token(self) -> str:
        return self.data.get("refresh_token", "")

    @property
    def email(self) -> str:
        return self.data.get("email", "")

    def set_login(self, email: str, login: dict) -> None:
        self.data["email"] = email
        self.data["token"] = login.get("accessToken", "")
        self.data["refresh_token"] = login.get("refreshToken", "")
        self.data["expires_time"] = login.get("expiresTime")
        self.data["user_id"] = login.get("userId")

    def set_tokens(self, token: str, refresh_token: str) -> None:
        self.data["token"] = token
        if refresh_token:
            self.data["refresh_token"] = refresh_token

    # devices -------------------------------------------------------------- #
    def set_devices(self, devices: list[Device]) -> None:
        self.data["devices"] = [d.raw for d in devices]

    def devices(self) -> list[Device]:
        return [Device.from_info(d) for d in self.data.get("devices", [])]

    @property
    def default_device(self) -> str:
        return self.data.get("default_device", "")

    @default_device.setter
    def default_device(self, device_id: str) -> None:
        self.data["default_device"] = device_id

    # bridge: alarm temperature set in Home Assistant, per device id ------- #
    def alarm_temperature(self, device_id: str, configured: Optional[float]) -> Optional[float]:
        """The stored limit (None = off), or `configured` if none was stored or the config changed since."""
        entry = self.data.get("alarm_temperature", {}).get(device_id)
        if not isinstance(entry, dict) or entry.get("config") != configured:
            return configured
        return entry.get("value")

    def set_alarm_temperature(self, device_id: str, value: Optional[float], configured: Optional[float]) -> None:
        self.data.setdefault("alarm_temperature", {})[device_id] = {"value": value, "config": configured}

    def find_device(self, selector: Optional[str]) -> Optional[Device]:
        """Match by id, exact name, case-insensitive name prefix, or MAC."""
        devs = self.devices()
        if not devs:
            return None
        if not selector:
            selector = self.default_device
            if not selector:
                return devs[0] if len(devs) == 1 else None
        s = selector.strip().lower()
        for d in devs:
            if d.id.lower() == s or d.mac_address.lower() == s:
                return d
        for d in devs:
            if d.name.lower() == s:
                return d
        for d in devs:
            if d.name.lower().startswith(s):
                return d
        return None
