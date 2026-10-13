"""
REST client for the KitchenBoss cloud (https://simmer.kitchenbossglobal.com/).

Reverse engineered from the KitchenBoss Android app (com.gerun.simmer 1.4.5).
The backend looks like a yudao / ruoyi-vue-pro "app-api" and wraps every
response as {"code": 200, "data": ..., "msg": "..."}.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .protocol import DeviceLanguage, TempUnit

BASE_URL = "https://simmer.kitchenbossglobal.com/"
TENANT_ID = "1"
PUBLIC_IP_URL = "https://checkip.amazonaws.com/"

# API paths (ContentApi.java)
P_LOGIN = "app-api/member/auth/login"
P_REGISTER = "app-api/member/auth/register"
P_SEND_CODE = "app-api/member/auth/send-sms-code"
P_RESET_PASSWORD = "app-api/member/auth/reset-password"
P_REFRESH_TOKEN = "app-api/member/auth/refresh-token"
P_USER_INFO = "app-api/member/user/get"
P_DEVICE_LIST = "app-api/device/device-app-info/list"
P_DEVICE_GET = "app-api/device/device-app-info/get"
P_DEVICE_BIND = "app-api/device/device-app-info/create"
P_DEVICE_UPDATE = "app-api/device/device-app-info/updateConfig"
P_DEVICE_VERSION = "app-api/device/device-app-info/getVersion"
P_DEVICE_MODELS = "app-api/device/device-app-info/getDeviceModel"
P_DEVICE_UPGRADE_MCU = "app-api/device/device-app-info/upgradeMcu"
P_DEVICE_UPGRADE_WIFI = "app-api/device/device-app-info/upgradeWifi"
P_DEVICE_UNBIND = "app-api/device/info-member/unDevice"
P_DEVICE_SORT = "app-api/device/info-member/updateSort"
P_SIGN_OUT = "app-api/device/info-member/sign-out"
P_APP_VERSION = "app-api/content/app-version/get"


class ApiError(Exception):
    def __init__(self, code: Any, msg: str, path: str = "", payload: Any = None):
        super().__init__(f"{path}: code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.path = path
        self.payload = payload


@dataclass
class Device:
    """Subset of deviceInfoDO that the tool needs."""
    id: str
    name: str
    model: str
    mac_address: str
    endpoint: str
    certificate: str
    private_key: str
    root_ca: str
    thing_id: str
    version: str
    firmware_mcu: str
    firmware_wifi: str
    is_online: Optional[str]
    language: Optional[str]
    temperature_unit: Optional[str]
    silent_sign: Optional[str]
    serial_num: Optional[str]
    raw: dict

    @classmethod
    def from_info(cls, info: dict) -> "Device":
        return cls(
            id=str(info.get("id", "")),
            name=str(info.get("name", "")),
            model=str(info.get("model", "")),
            mac_address=str(info.get("macAddress", "")),
            endpoint=str(info.get("endpoint", "") or ""),
            certificate=str(info.get("certificate", "") or ""),
            private_key=str(info.get("privateKey", "") or ""),
            root_ca=str(info.get("rootCA", "") or ""),
            thing_id=str(info.get("thingId", "") or ""),
            version=str(info.get("version", "") or "1"),
            firmware_mcu=str(info.get("versionNumberMcu") or info.get("versionNumber") or ""),
            firmware_wifi=str(info.get("versionNumberWifi") or ""),
            is_online=info.get("isOnline"),
            language=info.get("language"),
            temperature_unit=info.get("temperatureUnit"),
            silent_sign=info.get("silentSign"),
            serial_num=info.get("serialNum"),
            raw=info,
        )

    @classmethod
    def from_list_entry(cls, entry: dict) -> "Device":
        info = entry.get("deviceInfoDO") or entry
        return cls.from_info(info)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("raw", None)
        return d

    @property
    def has_mqtt_credentials(self) -> bool:
        return bool(self.endpoint and self.certificate and self.private_key)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def public_ip(timeout: float = 3.0) -> str:
    try:
        r = requests.get(PUBLIC_IP_URL, timeout=timeout)
        return r.text.strip() or "0.0.0.0"
    except Exception:
        return "0.0.0.0"


class KitchenBossApi:
    def __init__(
        self,
        token: str = "",
        refresh_token: str = "",
        language: str = "en_US",
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        mimic_app_headers: bool = False,
    ):
        self.token = token or ""
        self.refresh_token_value = refresh_token or ""
        self.language = language
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.mimic_app_headers = mimic_app_headers
        self.session = requests.Session()
        self.last_login: dict = {}

    # ------------------------------------------------------------------ #
    # low level
    # ------------------------------------------------------------------ #
    def _headers(self, content_type: Optional[str] = None) -> dict:
        h = {
            "Authorization": self.token,     # raw token, no "Bearer"
            "Accept-Language": self.language,
            "Tenant-Id": TENANT_ID,
            "User-Agent": "okhttp/4.9.0",
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    @staticmethod
    def _unwrap(resp: requests.Response, path: str) -> Any:
        try:
            payload = resp.json()
        except ValueError:
            raise ApiError(resp.status_code, resp.text[:200], path)
        if str(payload.get("code")) != "200":
            raise ApiError(payload.get("code"), payload.get("msg", ""), path, payload)
        return payload.get("data")

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        r = self.session.get(
            self.base_url + path, params=params, timeout=self.timeout,
            headers=self._headers("application/x-www-form-urlencoded"),
        )
        return self._unwrap(r, path)

    def _delete(self, path: str, params: Optional[dict] = None) -> Any:
        r = self.session.delete(
            self.base_url + path, params=params, timeout=self.timeout,
            headers=self._headers("application/x-www-form-urlencoded"),
        )
        return self._unwrap(r, path)

    def _send_json(self, method: str, path: str, body: dict) -> Any:
        # The app declares "multipart/form-data" while sending a JSON body
        # (OkHttp quirk).  Verified against the live server: that header
        # yields code 500, plain application/json works - so always use JSON.
        r = self.session.request(
            method, self.base_url + path, json=body, timeout=self.timeout,
            headers=self._headers("application/json;charset=UTF-8"),
        )
        return self._unwrap(r, path)

    def _post(self, path: str, body: dict) -> Any:
        return self._send_json("POST", path, body)

    def _put(self, path: str, body: dict) -> Any:
        return self._send_json("PUT", path, body)

    # ------------------------------------------------------------------ #
    # auth
    # ------------------------------------------------------------------ #
    def login(self, email: str, password: str, login_ip: Optional[str] = None) -> dict:
        """Password is sent as lowercase hex MD5 (as the app does)."""
        body = {
            "mailbox": email.strip(),
            "password": md5_hex(password.strip()),
            "loginIp": login_ip or public_ip(),
        }
        data = self._post(P_LOGIN, body)
        self.token = data.get("accessToken", "")
        self.refresh_token_value = data.get("refreshToken", "")
        self.last_login = data
        return data

    def refresh(self, refresh_token: Optional[str] = None) -> dict:
        """The app posts a form body `refreshToken=...` (OkHttp3UtilsNet.doPostRefreshToken)."""
        rt = refresh_token or self.refresh_token_value
        if not rt:
            raise ApiError("n/a", "no refresh token", P_REFRESH_TOKEN)
        r = self.session.post(
            self.base_url + P_REFRESH_TOKEN, data={"refreshToken": rt},
            params={"refreshToken": rt}, timeout=self.timeout, headers=self._headers(),
        )
        data = self._unwrap(r, P_REFRESH_TOKEN)
        if isinstance(data, dict):
            self.token = data.get("accessToken", self.token)
            self.refresh_token_value = data.get("refreshToken", rt)
        return data

    def user_info(self) -> dict:
        return self._get(P_USER_INFO)

    def sign_out(self) -> Any:
        return self._post(P_SIGN_OUT, {})

    # ------------------------------------------------------------------ #
    # devices
    # ------------------------------------------------------------------ #
    def devices(self) -> list[Device]:
        data = self._get(P_DEVICE_LIST) or []
        return [Device.from_list_entry(e) for e in data]

    def device(self, device_id: str) -> Device:
        data = self._get(P_DEVICE_GET, {"id": device_id})
        return Device.from_info(data)

    def device_raw(self, device_id: str) -> dict:
        return self._get(P_DEVICE_GET, {"id": device_id})

    def bind_device(self, model: str, mac_address: str, area: str) -> Device:
        """Called after the device accepted Wi-Fi credentials over BLE.

        Returns the new device record including endpoint / certificate /
        privateKey / rootCA that must be pushed to the device over BLE.
        """
        body = {"model": model, "macAddress": mac_address, "area": area, "status": 0, "notes": ""}
        return Device.from_info(self._post(P_DEVICE_BIND, body))

    def unbind_device(self, unbinding_id: str) -> Any:
        """`unbinding_id` is `unBindingId` from the device detail, not the device id."""
        return self._delete(P_DEVICE_UNBIND, {"id": unbinding_id})

    def update_device(
        self,
        device_id: str,
        *,
        name: Optional[str] = None,
        silent: Optional[bool] = None,
        temp_unit: Optional[TempUnit] = None,
        language: Optional[DeviceLanguage] = None,
    ) -> Any:
        body: dict[str, Any] = {"id": device_id}
        if name:
            body["name"] = name
        if silent is not None:
            body["silentSign"] = "1" if silent else "0"
        if temp_unit is not None:
            body["temperatureUnit"] = str(int(temp_unit))
        if language is not None:
            body["language"] = str(int(language))
        return self._post(P_DEVICE_UPDATE, body)

    def firmware_version(self, device_id: str) -> Any:
        return self._get(P_DEVICE_VERSION, {"deviceId": device_id})

    def device_models(self) -> Any:
        return self._get(P_DEVICE_MODELS)

    def upgrade_mcu(self, device_id: str) -> Any:
        return self._get(P_DEVICE_UPGRADE_MCU, {"deviceId": device_id})

    def upgrade_wifi(self, device_id: str) -> Any:
        return self._get(P_DEVICE_UPGRADE_WIFI, {"deviceId": device_id})

    def app_version(self) -> Any:
        return self._get(P_APP_VERSION)
