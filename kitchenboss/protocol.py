"""
KitchenBoss sous-vide protocol definitions.

Reverse engineered from the KitchenBoss Android app (com.gerun.simmer 1.4.5).
See PROTOCOL.md for the full description.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class WorkState(IntEnum):
    IDLE = 0        # device idle / "Cook" button shown
    PREHEATING = 1  # heating water to target
    PAUSED = 2      # paused (app sends 2 to pause)
    COOKING = 3     # cooking (app sends 3 to start / resume)
    FINISHED = 4    # finished (app sends 4 to stop)

    @property
    def label(self) -> str:
        return {
            0: "idle",
            1: "preheating",
            2: "paused",
            3: "cooking",
            4: "finished",
        }[int(self)]


class TempUnit(IntEnum):
    FAHRENHEIT = 0
    CELSIUS = 1

    @property
    def symbol(self) -> str:
        return "°F" if self == TempUnit.FAHRENHEIT else "°C"

    @classmethod
    def parse(cls, text: str) -> "TempUnit":
        t = text.strip().lower()
        if t in ("c", "celsius", "°c", "1"):
            return cls.CELSIUS
        if t in ("f", "fahrenheit", "°f", "0"):
            return cls.FAHRENHEIT
        raise ValueError(f"unknown temperature unit {text!r}")


class ErrorCode(IntEnum):
    NONE = 0
    OVERHEAT = 1
    LOW_WATER_LEVEL = 2
    OVERTIMED = 3
    TEMP_SENSOR_FAULT = 4

    @property
    def label(self) -> str:
        return {
            0: "ok",
            1: "OVERHEAT",
            2: "LOW WATER LEVEL",
            3: "OVERTIMED",
            4: "TEMP. SENSOR FAULT",
        }[int(self)]


class DeviceLanguage(IntEnum):
    ITALIAN = 0
    FRENCH = 1
    SPANISH = 2
    GERMAN = 3
    ENGLISH = 4
    JAPANESE = 5

    @classmethod
    def parse(cls, text: str) -> "DeviceLanguage":
        t = text.strip().lower()
        table = {
            "it": cls.ITALIAN, "italian": cls.ITALIAN,
            "fr": cls.FRENCH, "french": cls.FRENCH,
            "es": cls.SPANISH, "spanish": cls.SPANISH,
            "de": cls.GERMAN, "german": cls.GERMAN,
            "en": cls.ENGLISH, "english": cls.ENGLISH,
            "ja": cls.JAPANESE, "jp": cls.JAPANESE, "japanese": cls.JAPANESE,
        }
        if t.isdigit():
            return cls(int(t))
        if t in table:
            return table[t]
        raise ValueError(f"unknown language {text!r}")


# --------------------------------------------------------------------------- #
# MQTT commands and topics
# --------------------------------------------------------------------------- #
CMD_COOK_SET = "cook_set.sub"
CMD_COOK_SET_REPLY = "cook_set.sub_reply"
CMD_COOK_GET = "cook_get.sub"
CMD_COOK_GET_REPLY = "cook_get.sub_reply"
CMD_ATTR_GET = "attr_get.sub"
CMD_ATTR_GET_REPLY = "attr_get.sub_reply"
CMD_COOK_REPORT = "cook_report.pub"          # unsolicited status from the device
CMD_COOK_REPORT_REPLY = "cook_report.pub_reply"
CMD_OTA_UPGRADE_REPLY = "upgrade.sub_reply"

# Recipe id range accepted by the app; anything else is sent as 29 ("custom").
RECIPE_ID_MAX = 28
RECIPE_ID_CUSTOM = 29

MQTT_PORT = 8883
APP_CLIENT_ID_PREFIX = "android_client_snx"


def topic_base(model: str, device_id: str) -> str:
    return f"/cookie/{model}/{device_id}"


def topic_mcu_sub(model: str, device_id: str) -> str:
    """App -> device commands."""
    return topic_base(model, device_id) + "/mcu/sub"


def topic_mcu_sub_reply(model: str, device_id: str) -> str:
    """Device -> app replies to commands."""
    return topic_base(model, device_id) + "/mcu/sub_reply"


def topic_mcu_pub(model: str, device_id: str) -> str:
    """Device -> app unsolicited status reports."""
    return topic_base(model, device_id) + "/mcu/pub"


def topic_wildcard(model: str, device_id: str) -> str:
    return topic_base(model, device_id) + "/#"


def version_number(version: str) -> int:
    """'1.0.0' -> 100 (the app strips dots and parses as int)."""
    digits = "".join(ch for ch in str(version) if ch.isdigit())
    return int(digits) if digits else 1


def new_msg_id() -> int:
    return random.randint(0, 65530)


def build_message(cmd: str, data: Optional[dict] = None, version: int = 1) -> dict:
    return {
        "msgId": new_msg_id(),
        "version": int(version),
        "ts": int(time.time()),
        "body": {"cmd": cmd, "data": data or {}},
    }


def normalize_recipe_id(recipe_id: int | str) -> int:
    try:
        rid = int(recipe_id)
    except (TypeError, ValueError):
        return RECIPE_ID_CUSTOM
    return rid if 0 <= rid <= RECIPE_ID_MAX else RECIPE_ID_CUSTOM


def build_cook_set(
    *,
    set_time_s: int,
    set_temp_tenths: int,
    temp_unit: TempUnit,
    work_state: WorkState,
    recipe_id: int | str = RECIPE_ID_CUSTOM,
    version: int = 1,
) -> dict:
    return build_message(
        CMD_COOK_SET,
        {
            "id": normalize_recipe_id(recipe_id),
            "set_time": int(set_time_s),
            "set_temp": int(set_temp_tenths),
            "temp_unit": int(temp_unit),
            "work_state": int(work_state),
        },
        version,
    )


def build_cook_get(version: int = 1) -> dict:
    return build_message(CMD_COOK_GET, {}, version)


def build_attr_get(version: int = 1) -> dict:
    return build_message(CMD_ATTR_GET, {}, version)


# --------------------------------------------------------------------------- #
# Temperature helpers
# --------------------------------------------------------------------------- #
def c_to_f(c: float) -> float:
    return c * 1.8 + 32.0


def f_to_c(f: float) -> float:
    return (f - 32.0) / 1.8


def encode_temp(degrees: float) -> int:
    """Degrees -> tenths of a degree as used on the wire."""
    return int(round(degrees * 10))


def decode_temp(tenths: int) -> float:
    return tenths / 10.0


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# Status message
# --------------------------------------------------------------------------- #
@dataclass
class CookStatus:
    """Payload of cook_report.pub / cook_get.sub_reply."""
    recipe_id: str = ""
    set_time: int = 0       # seconds
    rem_time: int = 0       # seconds remaining
    total_time: int = 0     # seconds
    set_temp: int = 0       # tenths of degree
    cur_temp: int = 0       # tenths of degree
    temp_unit: TempUnit = TempUnit.CELSIUS
    work_state: WorkState = WorkState.IDLE
    err_code: ErrorCode = ErrorCode.NONE
    wifi_rssi: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_data(cls, d: dict) -> "CookStatus":
        def _int(key: str, default: int = 0) -> int:
            try:
                return int(d.get(key, default))
            except (TypeError, ValueError):
                return default

        def _enum(enum_cls, key, default):
            try:
                return enum_cls(_int(key, int(default)))
            except ValueError:
                return default

        return cls(
            recipe_id=str(d.get("id", "")),
            set_time=_int("set_time"),
            rem_time=_int("rem_time"),
            total_time=_int("total_time"),
            set_temp=_int("set_temp"),
            cur_temp=_int("cur_temp"),
            temp_unit=_enum(TempUnit, "temp_unit", TempUnit.CELSIUS),
            work_state=_enum(WorkState, "work_state", WorkState.IDLE),
            err_code=_enum(ErrorCode, "err_code", ErrorCode.NONE),
            wifi_rssi=_int("wifi_rssi"),
            raw=dict(d),
        )

    @property
    def set_temp_deg(self) -> float:
        return decode_temp(self.set_temp)

    @property
    def cur_temp_deg(self) -> float:
        return decode_temp(self.cur_temp)

    @property
    def unit_symbol(self) -> str:
        return self.temp_unit.symbol

    @property
    def is_running(self) -> bool:
        return self.work_state in (WorkState.PREHEATING, WorkState.COOKING, WorkState.PAUSED)

    def to_dict(self) -> dict:
        return {
            "recipe_id": self.recipe_id,
            "work_state": self.work_state.label,
            "work_state_code": int(self.work_state),
            "error": self.err_code.label,
            "error_code": int(self.err_code),
            "unit": "C" if self.temp_unit == TempUnit.CELSIUS else "F",
            "current_temp": self.cur_temp_deg,
            "target_temp": self.set_temp_deg,
            "set_time_s": self.set_time,
            "remaining_s": self.rem_time,
            "total_time_s": self.total_time,
            "wifi_rssi": self.wifi_rssi,
        }

    def describe(self) -> str:
        u = self.unit_symbol
        lines = [
            f"state      : {self.work_state.label} ({int(self.work_state)})",
            f"error      : {self.err_code.label}",
            f"current    : {self.cur_temp_deg:.1f}{u}",
            f"target     : {self.set_temp_deg:.1f}{u}",
            f"set time   : {fmt_duration(self.set_time)}",
            f"remaining  : {fmt_duration(self.rem_time)}",
            f"total time : {fmt_duration(self.total_time)}",
            f"recipe id  : {self.recipe_id}",
            f"wifi rssi  : {self.wifi_rssi} dBm",
        ]
        return "\n".join(lines)


@dataclass
class ParsedMessage:
    topic: str
    msg_id: Optional[int]
    version: Optional[int]
    ts: Optional[int]
    cmd: str
    data: Any
    code: Optional[int]
    msg: Optional[str]
    raw: dict

    @property
    def status(self) -> Optional[CookStatus]:
        if self.cmd in (CMD_COOK_REPORT, CMD_COOK_GET_REPLY) and isinstance(self.data, dict):
            return CookStatus.from_data(self.data)
        return None


def parse_message(topic: str, payload: bytes | str) -> ParsedMessage:
    text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else payload
    raw = json.loads(text)
    body = raw.get("body") or {}
    if isinstance(body, str):
        body = json.loads(body)
    return ParsedMessage(
        topic=topic,
        msg_id=raw.get("msgId"),
        version=raw.get("version"),
        ts=raw.get("ts"),
        cmd=str(body.get("cmd", "")),
        data=body.get("data"),
        code=body.get("code"),
        msg=body.get("msg"),
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# BLE provisioning constants (JSON over GATT)
# --------------------------------------------------------------------------- #
BLE_SERVICE_UUID = "59462f12-9543-9999-12c8-58b459a2712d"
BLE_WRITE_UUID = "5c3a659e-897e-45e1-b016-007107c96df7"
BLE_NOTIFY_UUID = "5c3a659e-897e-45e1-b016-007107c96df6"
BLE_NAME_FILTER = "KB"       # app only lists devices whose BLE name contains "KB"
BLE_CHUNK_LEN = 64           # cert/key chunks before base64

BLE_CMD_GET_DEVICE_INFO = 0
BLE_CMD_SET_WIFI = 1
BLE_CMD_SET_ITEM = 2         # host / devid / key / crt, chunked, base64
BLE_CMD_START_PROVISION = 3
BLE_CMD_QUERY_RESULT = 4
BLE_CMD_EXIT_PROVISION = 5
BLE_CMD_WIFI_LIST = 6

# Provisioning result states (cmd 4 reply data.state)
BLE_STATE_WIFI_OK = 6
BLE_STATE_CLOUD_OK = 7
BLE_STATE_DONE = 8
BLE_STATE_FAILED = (106, 107, 108)
