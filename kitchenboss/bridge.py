"""
MQTT bridge: KitchenBoss cloud (AWS IoT) <-> local MQTT broker, with Home Assistant discovery.

    kitchenboss-bridge --config config.yaml        (or: python -m kitchenboss.bridge)

Topics (base = <topic_prefix>/<base_topic>, device = cloud device id):

    <base>/bridge/status                 online | offline (LWT, retained)
    <base>/<device>/availability         online | offline (cooker reachable through the cloud)
    <base>/<device>/state                JSON status (retained)
    <base>/<device>/info                 JSON device metadata (retained)
    <base>/<device>/message              every raw cloud message (not retained, debugging)
    <base>/<device>/alert                monitor alarms raised / cleared (not retained)
    <base>/<device>/set/command          start | stop | pause | resume | reset | refresh
                                         or JSON {"temperature": 56.5, "time": "1h30m", "unit": "C"} = start
    <base>/<device>/set/target_temperature   setpoint in °C
    <base>/<device>/set/cook_time            setpoint in minutes (or "1h30m", "2:30")
    <base>/<device>/set/alarm_temperature    monitor temperature alarm limit in °C, 0 = off

All temperatures on the local broker are °C; °F cookers are converted both ways.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import paho.mqtt.client as mqtt
import requests

from . import __version__
from . import protocol as P
from .api import ApiError, Device, KitchenBossApi
from .bridge_config import ConfigError, find_config, load_config, log_level
from .config import Config
from .monitor import DeviceMonitor, rule_matches
from .mqtt import KitchenBossMqtt, MqttError
from .protocol import CookStatus, ErrorCode, ParsedMessage, TempUnit, WorkState

log = logging.getLogger("kitchenboss.bridge")

TEMP_RANGE_C = (1.0, 100.0)
TIME_RANGE_MIN = (1, 5999)       # 99:59 h
COMMANDS = ("start", "stop", "pause", "resume", "reset", "refresh")
BUTTONS = (
    ("start", "Start", "mdi:play"),
    ("stop", "Stop", "mdi:stop"),
    ("pause", "Pause", "mdi:pause"),
    ("resume", "Resume", "mdi:play-pause"),
    ("refresh", "Refresh", "mdi:refresh"),
)
# combined state: an active error wins over the work state
STATE_OPTIONS = [w.name.lower() for w in WorkState] + [e.name.lower() for e in ErrorCode if e != ErrorCode.NONE]
ORIGIN = {"name": "kitchenboss", "sw_version": __version__}

# exit codes (systemd unit: RestartPreventExitStatus=2 3)
EXIT_CONFIG = 2
EXIT_AUTH = 3


def to_celsius(value: float, unit: TempUnit) -> float:
    return round(P.f_to_c(value) if unit == TempUnit.FAHRENHEIT else value, 1)


def from_celsius(value: float, unit: TempUnit) -> float:
    return round(P.c_to_f(value) if unit == TempUnit.FAHRENHEIT else value, 1)


def check_temp_c(value: float) -> float:
    lo, hi = TEMP_RANGE_C
    if not lo <= value <= hi:
        raise ValueError(f"temperature {value} out of range {lo}..{hi} °C")
    return value


def parse_cook_time(value: Any) -> int:
    """Number = minutes; strings like '1h30m', '2:30', '5400s' are accepted too. Returns seconds."""
    from .cli import parse_duration  # same syntax as the CLI

    if isinstance(value, (int, float)):
        secs = int(round(float(value) * 60))
    else:
        text = str(value).strip()
        try:
            secs = int(round(float(text) * 60))
        except ValueError:
            try:
                secs = parse_duration(text)
            except Exception as exc:  # noqa: BLE001 - argparse.ArgumentTypeError
                raise ValueError(str(exc)) from None
    lo, hi = TIME_RANGE_MIN
    if not lo * 60 <= secs <= hi * 60:
        raise ValueError(f"cook time {secs}s out of range {lo}..{hi} min")
    return secs


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def unit_from_record(device: Device) -> TempUnit:
    try:
        return TempUnit(int(device.temperature_unit))
    except (TypeError, ValueError):
        return TempUnit.CELSIUS


# --------------------------------------------------------------------------- #
# cloud REST session
# --------------------------------------------------------------------------- #
class CloudAccount:
    """Reuses the cached token, refreshes it on 401, logs in with the configured password as last resort."""

    def __init__(self, email: str, password: str, cache_path: Path, timeout: float):
        self.email = email
        self.password = password
        self.cache = Config(cache_path)
        same = self.cache.email.lower() == email.lower()
        self.api = KitchenBossApi(
            token=self.cache.token if same else "",
            refresh_token=self.cache.refresh_token if same else "",
            timeout=max(timeout, 20.0),
        )

    def _login(self) -> None:
        if not self.password:
            raise ApiError("n/a", "no password configured and no valid cached session", "login")
        log.info("logging in to the KitchenBoss cloud as %s", self.email)
        data = self.api.login(self.email, self.password)
        self.cache.set_login(self.email, data)
        self.cache.save()

    def call(self, fn, *a, **kw):
        if not self.api.token:
            self._login()
            return fn(*a, **kw)
        try:
            return fn(*a, **kw)
        except ApiError as exc:
            if str(exc.code) != "401":
                raise
        log.info("access token expired, refreshing")
        try:
            self.api.refresh()
            self.cache.set_tokens(self.api.token, self.api.refresh_token_value)
            self.cache.save()
        except ApiError as exc:
            log.info("token refresh failed (%s), logging in again", exc.msg)
            self._login()
        return fn(*a, **kw)

    def devices(self) -> list[Device]:
        devices = self.call(self.api.devices)
        self.cache.set_devices(devices)
        self.cache.save()
        return devices

    def device_detail(self, device_id: str) -> Device:
        return self.call(self.api.device, device_id)

    def select(self, selectors: list[str]) -> list[Device]:
        devices = self.cache.devices()
        if not selectors:
            return devices
        out: list[Device] = []
        for sel in selectors:
            dev = self.cache.find_device(sel)
            if dev is None:
                log.warning("configured device %r not found on the account (known: %s)", sel,
                            ", ".join(f"{d.name} ({d.id})" for d in devices) or "none")
            elif all(d.id != dev.id for d in out):
                out.append(dev)
        return out


# --------------------------------------------------------------------------- #
# one cooker
# --------------------------------------------------------------------------- #
class DeviceBridge:
    def __init__(self, bridge: "Bridge", device: Device):
        self.bridge = bridge
        self.device = device
        self.node_id = "kitchenboss_" + re.sub(r"[^A-Za-z0-9_-]", "_", device.id)
        self.base = f"{bridge.base_topic}/{device.id}"
        self.lock = threading.RLock()
        self.cloud: Optional[KitchenBossMqtt] = None
        self.cloud_was_connected = False
        self.next_connect = 0.0
        self.backoff = 5.0
        self.last_poll = 0.0
        self.last_seen = 0.0          # monotonic
        self.last_seen_wall = 0.0     # epoch
        self.status: Optional[CookStatus] = None
        self.unit = unit_from_record(device)
        self.available: Optional[bool] = None
        self.monitor: Optional[DeviceMonitor] = None
        cooking = bridge.cfg["cooking"]
        if self.unit == TempUnit.FAHRENHEIT:
            self.setpoint_c = to_celsius(cooking["default_temperature_f"], TempUnit.FAHRENHEIT)
        else:
            self.setpoint_c = cooking["default_temperature_c"]
        self.setpoint_time_s = int(round(cooking["default_time"] * 60))

    @property
    def name(self) -> str:
        return self.device.name or self.device.id

    # cloud connection -------------------------------------------------- #
    def connect_cloud(self) -> None:
        cfg = self.bridge.cfg["cloud"]
        client_id = cfg["client_id"]
        if client_id and len(self.bridge.devices) > 1:
            client_id = f"{client_id}-{self.device.id}"
        try:
            client = KitchenBossMqtt(
                self.device, client_id=client_id, mimic_app=cfg["mimic_app"],
                on_status=self._on_status, on_message=self._on_message,
            )
            log.info("[%s] connecting to cloud %s:%s as %s", self.name, client.host, client.port, client.client_id)
            client.connect(timeout=cfg["timeout"])
        except MqttError as exc:
            log.warning("[%s] cloud connect failed: %s; retry in %.0fs", self.name, exc, self.backoff)
            self.next_connect = time.monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, cfg["reconnect_max_delay"])
            return
        self.backoff = 5.0
        self.cloud = client

    def cloud_connected(self) -> bool:
        return self.cloud is not None and self.cloud.client.is_connected()

    def require_cloud(self) -> KitchenBossMqtt:
        if not self.cloud_connected():
            raise MqttError("not connected to the cloud")
        return self.cloud  # type: ignore[return-value]

    def poll(self) -> None:
        self.last_poll = time.monotonic()
        if not self.cloud_connected():
            return
        try:
            self.cloud.publish(P.build_cook_get(self.cloud.version))  # type: ignore[union-attr]
        except MqttError as exc:
            log.warning("[%s] cook_get failed: %s", self.name, exc)

    def tick(self, now: float) -> None:
        if self.cloud is None:
            if now >= self.next_connect:
                self.connect_cloud()
            if self.cloud is None:
                self._set_available(False)
                return
        connected = self.cloud_connected()
        if connected and (not self.cloud_was_connected or now - self.last_poll >= self.bridge.update_interval):
            self.poll()
        if self.cloud_was_connected and not connected:
            log.warning("[%s] lost cloud connection, paho reconnects", self.name)
        self.cloud_was_connected = connected
        online = connected and self.last_seen > 0 and now - self.last_seen < self.bridge.offline_after
        self._set_available(online)
        if self.monitor is not None:
            self.monitor.evaluate(now)

    def _set_available(self, online: bool) -> None:
        if online != self.available:
            self.available = online
            log.info("[%s] %s", self.name, "online" if online else "offline")
            self.bridge.publish_availability(self)

    def close(self) -> None:
        if self.cloud is not None:
            self.cloud.close()
            self.cloud = None

    # cloud callbacks (paho thread of the cloud client) ----------------- #
    def _on_message(self, msg: ParsedMessage) -> None:
        with self.lock:
            self.last_seen = time.monotonic()
            self.last_seen_wall = time.time()
        self.bridge.publish(f"{self.base}/message", msg.raw, retain=False, qos=0)

    def _on_status(self, st: CookStatus, msg: ParsedMessage) -> None:
        with self.lock:
            self.status = st
            self.unit = st.temp_unit
            if st.is_running:
                # follow the program the cooker actually runs
                self.setpoint_c = to_celsius(st.set_temp_deg, st.temp_unit)
                self.setpoint_time_s = st.set_time
        was_available = self.available
        self._set_available(self.cloud_connected())
        if was_available:
            self.bridge.publish_state(self)  # _set_available already published it otherwise

    # state ---------------------------------------------------------------- #
    def state_payload(self) -> Optional[dict]:
        with self.lock:
            st = self.status
            if st is None:
                return None
            if st.err_code != ErrorCode.NONE:
                state = st.err_code.name.lower()
            else:
                state = st.work_state.name.lower()
            finish_at = None
            if st.work_state == WorkState.COOKING and st.rem_time > 0:
                end = datetime.now(timezone.utc) + timedelta(seconds=st.rem_time)
                end = end.replace(second=0, microsecond=0) + timedelta(minutes=1 if end.second >= 30 else 0)
                finish_at = end.isoformat(timespec="seconds")
            return {
                "state": state,
                "work_state": st.work_state.name.lower(),
                "error": st.err_code.name.lower(),
                "problem": st.err_code != ErrorCode.NONE,
                "active": st.work_state in (WorkState.PREHEATING, WorkState.COOKING),
                "current_temp": to_celsius(st.cur_temp_deg, st.temp_unit),
                "target_temp": to_celsius(st.set_temp_deg, st.temp_unit),
                "device_unit": "F" if st.temp_unit == TempUnit.FAHRENHEIT else "C",
                "set_time": st.set_time,
                "total_time": st.total_time,
                "remaining": st.rem_time,
                "finish_at": finish_at,
                "wifi_rssi": st.wifi_rssi,
                "setpoint_temp": round(self.setpoint_c, 1),
                "setpoint_time": round(self.setpoint_time_s / 60),
                "last_update": iso(self.last_seen_wall or time.time()),
                "alarm": bool(self.monitor and self.monitor.alarm),
                "alarm_reason": self.monitor.reason if self.monitor else None,
                "alarm_temp": (self.monitor.limit or 0) if self.monitor else None,
            }

    def info_payload(self) -> dict:
        d = self.device
        return {
            "id": d.id, "name": d.name, "model": d.model, "mac": d.mac_address, "serial": d.serial_num,
            "firmware_mcu": d.firmware_mcu, "firmware_wifi": d.firmware_wifi, "bridge_version": __version__,
        }

    # commands (bridge worker thread) ----------------------------------- #
    def handle(self, what: str, payload: str) -> None:
        payload = payload.strip()
        if what == "target_temperature":
            self.set_temperature(float(payload))
        elif what == "cook_time":
            self.set_cook_time(parse_cook_time(payload))
        elif what == "alarm_temperature":
            self.set_alarm_temperature(float(payload))
        elif what == "command":
            if payload.startswith("{"):
                self.start_from_json(json.loads(payload))
            else:
                self.command(payload.lower())
        else:
            raise ValueError(f"unknown command topic .../set/{what}")

    def command(self, cmd: str) -> None:
        if cmd == "refresh":
            self.poll()
            return
        if cmd not in COMMANDS:
            raise ValueError(f"unknown command {cmd!r}, expected one of {', '.join(COMMANDS)}")
        c = self.require_cloud()
        timeout = self.bridge.cfg["cloud"]["timeout"]
        if cmd == "start":
            with self.lock:
                temp_c, secs, unit = self.setpoint_c, self.setpoint_time_s, self.unit
            self._start(c, temp_c, secs, unit, P.RECIPE_ID_CUSTOM)
        else:
            log.info("[%s] %s", self.name, cmd)
            getattr(c, cmd)(timeout=timeout)
        self.poll()

    def _start(self, c: KitchenBossMqtt, temp_c: float, secs: int, unit: TempUnit, recipe) -> None:
        temp = from_celsius(temp_c, unit)
        log.info("[%s] start %.1f%s for %s", self.name, temp, unit.symbol, P.fmt_duration(secs))
        c.start(temp, unit, secs, recipe_id=recipe, timeout=self.bridge.cfg["cloud"]["timeout"])

    def start_from_json(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("JSON command must be an object")
        with self.lock:
            unit = self.unit
            temp_c, secs = self.setpoint_c, self.setpoint_time_s
        if "temperature" in data:
            given_unit = TempUnit.parse(str(data.get("unit", "C")))
            temp_c = check_temp_c(to_celsius(float(data["temperature"]), given_unit))
        if "time" in data:
            secs = parse_cook_time(data["time"])
        c = self.require_cloud()
        self._start(c, temp_c, secs, unit, data.get("recipe", P.RECIPE_ID_CUSTOM))
        self.poll()

    def set_temperature(self, temp_c: float) -> None:
        check_temp_c(temp_c)
        with self.lock:
            self.setpoint_c = temp_c
            st = self.status
        log.info("[%s] setpoint %.1f°C", self.name, temp_c)
        if st is not None and st.is_running and self.bridge.cfg["cooking"]["apply_while_running"]:
            self._apply_running(st, P.encode_temp(from_celsius(temp_c, st.temp_unit)), st.set_time)
        self.bridge.publish_state(self)

    def set_cook_time(self, secs: int) -> None:
        with self.lock:
            self.setpoint_time_s = secs
            st = self.status
        log.info("[%s] setpoint time %s", self.name, P.fmt_duration(secs))
        if st is not None and st.is_running and self.bridge.cfg["cooking"]["apply_while_running"]:
            self._apply_running(st, st.set_temp, secs)
        self.bridge.publish_state(self)

    def set_alarm_temperature(self, temp_c: float) -> None:
        """Temperature alarm limit in °C; 0 = off. Remembered in the account cache."""
        mon = self.monitor
        if mon is None:
            raise ValueError("no monitor configured for this cooker (monitor.devices)")
        limit = None if temp_c <= 0 else check_temp_c(temp_c)
        mon.set_limit(limit)
        cache = self.bridge.account.cache
        cache.set_alarm_temperature(self.device.id, limit, mon.configured_limit)
        cache.save()
        self.bridge.publish_state(self)

    def extend_timer(self) -> None:
        """monitor restart=true: set the timer back to monitor.restart_hours while cooking.

        The cooker restarts the countdown from the new set_time (verified on a G322PX).
        """
        mon = self.monitor
        st = self.status
        if mon is None or st is None or st.work_state != WorkState.COOKING or st.rem_time >= mon.extend_below:
            return
        c = self.require_cloud()
        log.info("[%s] remaining %s, setting timer back to %s", self.name,
                 P.fmt_duration(st.rem_time), P.fmt_duration(mon.restart_time))
        c.set_cook(
            work_state=WorkState.COOKING, set_temp_tenths=st.set_temp, temp_unit=st.temp_unit,
            set_time_s=mon.restart_time, recipe_id=st.recipe_id or P.RECIPE_ID_CUSTOM,
            timeout=self.bridge.cfg["cloud"]["timeout"],
        )
        self.poll()

    def _apply_running(self, st: CookStatus, temp_tenths: int, set_time_s: int) -> None:
        c = self.require_cloud()
        state = WorkState.PAUSED if st.work_state == WorkState.PAUSED else WorkState.COOKING
        c.set_cook(
            work_state=state, set_temp_tenths=temp_tenths, temp_unit=st.temp_unit, set_time_s=set_time_s,
            recipe_id=st.recipe_id or P.RECIPE_ID_CUSTOM, timeout=self.bridge.cfg["cloud"]["timeout"],
        )
        self.poll()


# --------------------------------------------------------------------------- #
# bridge
# --------------------------------------------------------------------------- #
class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        m = cfg["mqtt"]
        self.base_topic = "/".join(s for s in (m["topic_prefix"], m["base_topic"]) if s)
        self.bridge_status_topic = f"{self.base_topic}/bridge/status"
        self.qos = m["qos"]
        self.retain = m["retain"]
        self.discovery = m["auto_discovery"]
        self.disc_prefix = m["auto_discovery_prefix"]
        self.update_interval = m["update_interval"]
        self.offline_after = m["offline_after"] or max(3 * self.update_interval, 180.0)
        self.devices: dict[str, DeviceBridge] = {}
        self.devices_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        acc = cfg["account"]
        self.account = CloudAccount(acc["email"], acc["password"], acc["cache_file"], cfg["cloud"]["timeout"])
        self.next_device_refresh = 0.0
        self.local = self._make_local_client()

    # local broker ------------------------------------------------------- #
    def _make_local_client(self) -> mqtt.Client:
        m = self.cfg["mqtt"]
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=m["client_id"] or "",
                        clean_session=True, protocol=mqtt.MQTTv311)
        if m["username"]:
            c.username_pw_set(str(m["username"]), None if m["password"] is None else str(m["password"]))
        if m["tls"]:
            import ssl

            c.tls_set(
                ca_certs=str(m["tls_ca_certs"]) if m["tls_ca_certs"] else None,
                certfile=str(m["tls_certfile"]) if m["tls_certfile"] else None,
                keyfile=str(m["tls_keyfile"]) if m["tls_keyfile"] else None,
                cert_reqs=ssl.CERT_NONE if m["tls_insecure"] else ssl.CERT_REQUIRED,
            )
            if m["tls_insecure"]:
                c.tls_insecure_set(True)
        c.will_set(self.bridge_status_topic, "offline", qos=1, retain=True)
        c.reconnect_delay_set(1, 60)
        c.on_connect = self._on_local_connect
        c.on_connect_fail = self._on_local_connect_fail
        c.on_disconnect = self._on_local_disconnect
        c.on_message = self._on_local_message
        return c

    def _on_local_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("local broker refused connection: %s", reason_code)
            return
        m = self.cfg["mqtt"]
        log.info("connected to local broker %s:%s", m["host"], m["port"])
        client.publish(self.bridge_status_topic, "online", qos=1, retain=True)
        client.subscribe(f"{self.base_topic}/+/set/+", qos=self.qos)
        if self.discovery:
            client.subscribe(f"{self.disc_prefix}/status", qos=self.qos)
        self.jobs.put(("announce",))

    def _on_local_connect_fail(self, client, userdata):
        m = self.cfg["mqtt"]
        log.error("cannot connect to local broker %s:%s (not running / wrong mqtt.host or port?); retrying",
                  m["host"], m["port"])

    def _on_local_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if not self.stop_event.is_set():
            log.warning("disconnected from local broker: %s", reason_code)

    def _on_local_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
        if self.discovery and msg.topic == f"{self.disc_prefix}/status":
            if payload.strip().lower() == "online":
                log.info("Home Assistant came online, re-announcing")
                self.jobs.put(("announce",))
            return
        parts = msg.topic[len(self.base_topic) + 1:].split("/")
        if len(parts) != 3 or parts[1] != "set":
            return
        if msg.retain:
            log.warning("ignoring retained command on %s (clear it with an empty retained message)", msg.topic)
            return
        with self.devices_lock:
            dev = self.devices.get(parts[0])
        if dev is None:
            log.warning("command for unknown device %s", parts[0])
            return
        log.debug("command %s %r", msg.topic, payload)
        self.jobs.put(("command", dev, parts[2], payload))

    def publish(self, topic: str, payload: Any, retain: Optional[bool] = None, qos: Optional[int] = None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        log.debug("local -> %s %s", topic, payload)
        return self.local.publish(topic, payload, qos=self.qos if qos is None else qos,
                                  retain=self.retain if retain is None else retain)

    def publish_availability(self, dev: DeviceBridge) -> None:
        self.publish(f"{dev.base}/availability", "online" if dev.available else "offline", retain=True)
        if dev.available:
            self.publish_state(dev)

    def publish_state(self, dev: DeviceBridge) -> None:
        payload = dev.state_payload()
        if payload is not None:
            self.publish(f"{dev.base}/state", payload)

    def on_alarm_change(self, dev: DeviceBridge, key: str, active: bool, text: str) -> None:
        self.publish(f"{dev.base}/alert", {"alarm": key, "active": active, "message": text,
                                           "time": iso(time.time())}, retain=False)
        self.publish_state(dev)

    def announce(self, dev: DeviceBridge) -> None:
        if self.discovery:
            self.publish_discovery(dev)
        self.publish(f"{dev.base}/info", dev.info_payload(), retain=True)
        self.publish(f"{dev.base}/availability", "online" if dev.available else "offline", retain=True)
        self.publish_state(dev)

    # Home Assistant discovery ------------------------------------------- #
    def publish_discovery(self, dev: DeviceBridge) -> None:
        for component, key, config in self._entities(dev):
            self.publish(f"{self.disc_prefix}/{component}/{dev.node_id}/{key}/config", config, retain=True)

    def _entities(self, dev: DeviceBridge) -> list[tuple[str, str, dict]]:
        d = dev.device
        state = f"{dev.base}/state"
        cmd = f"{dev.base}/set"
        device_block = {
            "identifiers": [dev.node_id],
            "name": d.name or f"KitchenBoss {d.model}",
            "manufacturer": "KitchenBoss",
            "model": d.model,
            "sw_version": d.firmware_mcu,
            "serial_number": d.serial_num,
        }
        if d.mac_address:
            device_block["connections"] = [["mac", d.mac_address.lower()]]
        common = {
            "device": {k: v for k, v in device_block.items() if v},
            "origin": ORIGIN,
            "availability": [{"topic": self.bridge_status_topic}, {"topic": f"{dev.base}/availability"}],
            "availability_mode": "all",
            "qos": self.qos,
        }

        def ent(component: str, key: str, name: str, **extra) -> tuple[str, str, dict]:
            c = dict(common, unique_id=f"{dev.node_id}_{key}", name=name)
            if component in ("sensor", "binary_sensor", "number"):
                c["state_topic"] = state
            c.update(extra)
            return component, key, c

        temp = {"device_class": "temperature", "unit_of_measurement": "°C", "suggested_display_precision": 1}
        lo, hi = TEMP_RANGE_C
        tmin, tmax = TIME_RANGE_MIN
        hidden = {"entity_category": "diagnostic", "enabled_by_default": False}
        return [
            ent("sensor", "state", "State", value_template="{{ value_json.state }}",
                device_class="enum", options=STATE_OPTIONS, icon="mdi:pot-steam"),
            ent("sensor", "current_temperature", "Water temperature",
                value_template="{{ value_json.current_temp }}", state_class="measurement", **temp),
            ent("sensor", "target_temperature", "Target temperature",
                value_template="{{ value_json.target_temp }}", **temp),
            ent("sensor", "set_time", "Set time", value_template="{{ value_json.set_time }}",
                device_class="duration", unit_of_measurement="s", icon="mdi:timer-cog-outline"),
            ent("sensor", "total_time", "Total time", value_template="{{ value_json.total_time }}",
                device_class="duration", unit_of_measurement="s", icon="mdi:timer-outline"),
            ent("sensor", "remaining_time", "Remaining time", value_template="{{ value_json.remaining }}",
                device_class="duration", unit_of_measurement="s", icon="mdi:timer-sand", **hidden),
            ent("sensor", "finish_time", "Finish time", value_template="{{ value_json.finish_at }}",
                device_class="timestamp"),
            ent("sensor", "wifi_rssi", "Wi-Fi signal", value_template="{{ value_json.wifi_rssi }}",
                device_class="signal_strength", unit_of_measurement="dBm", state_class="measurement", **hidden),
            ent("sensor", "last_update", "Last update", value_template="{{ value_json.last_update }}",
                device_class="timestamp", **hidden),
            ent("binary_sensor", "heating", "Heating",
                value_template="{{ 'ON' if value_json.active else 'OFF' }}", device_class="running"),
            ent("binary_sensor", "problem", "Problem",
                value_template="{{ 'ON' if value_json.problem else 'OFF' }}", device_class="problem"),
            ent("number", "setpoint_temperature", "Cook temperature",
                value_template="{{ value_json.setpoint_temp }}", command_topic=f"{cmd}/target_temperature",
                min=lo, max=hi, step=0.1, mode="box", icon="mdi:thermometer-water",
                device_class="temperature", unit_of_measurement="°C"),
            ent("number", "setpoint_time", "Cook duration",
                value_template="{{ value_json.setpoint_time }}", command_topic=f"{cmd}/cook_time",
                min=tmin, max=tmax, step=1, mode="box", icon="mdi:timer-edit-outline",
                device_class="duration", unit_of_measurement="min"),
        ] + ([
            ent("binary_sensor", "alarm", "Alarm",
                value_template="{{ 'ON' if value_json.alarm else 'OFF' }}", device_class="problem",
                json_attributes_topic=state,
                json_attributes_template="{{ {'reason': value_json.alarm_reason} | tojson }}",
                icon="mdi:alarm-light-outline"),
            ent("number", "alarm_temperature", "Alarm temperature",
                value_template="{{ value_json.alarm_temp }}", command_topic=f"{cmd}/alarm_temperature",
                min=0, max=hi, step=0.1, mode="box", icon="mdi:thermometer-alert",
                device_class="temperature", unit_of_measurement="°C"),
        ] if dev.monitor is not None else []) + [
            ent("button", key, label, command_topic=f"{cmd}/command", payload_press=key, icon=icon,
                **(hidden if key == "refresh" else {}))
            for key, label, icon in BUTTONS
        ]

    # devices -------------------------------------------------------------- #
    def _fetch_devices(self) -> list[Device]:
        devices = self.account.devices()
        log.info("account has %d device(s): %s", len(devices),
                 ", ".join(f"{d.name} ({d.model}/{d.id})" for d in devices) or "none")
        return devices

    def _load_initial_devices(self) -> bool:
        delay = 10.0
        while not self.stop_event.is_set():
            try:
                self._fetch_devices()
                return True
            except ApiError as exc:
                transient = str(exc.code) in ("500", "502", "503", "504")
                if not transient:
                    if self.account.cache.devices():
                        log.error("cloud login failed (%s); using cached device records", exc)
                        return True
                    log.critical("cloud login failed: %s", exc)
                    return False
                log.error("cloud API error: %s; retry in %.0fs", exc, delay)
            except requests.RequestException as exc:
                if self.account.cache.devices():
                    log.error("cloud unreachable (%s); using cached device records", exc)
                    return True
                log.error("cloud unreachable: %s; retry in %.0fs", exc, delay)
            self.stop_event.wait(delay)
            delay = min(delay * 2, self.cfg["cloud"]["reconnect_max_delay"])
        return False

    def _sync_devices(self) -> None:
        """Add newly configured devices; refresh metadata of known ones."""
        for d in self.account.select(self.cfg["devices"]):
            if not d.has_mqtt_credentials:
                try:
                    d = self.account.device_detail(d.id)
                except (ApiError, requests.RequestException) as exc:
                    log.warning("[%s] cannot fetch device detail: %s", d.name, exc)
                    continue
                if not d.has_mqtt_credentials:
                    log.warning("[%s] cloud record has no MQTT certificate, skipped", d.name)
                    continue
            with self.devices_lock:
                existing = self.devices.get(d.id)
                if existing is None:
                    self.devices[d.id] = dev = DeviceBridge(self, d)
                    log.info("[%s] bridging %s/%s as %s", dev.name, d.model, d.id, dev.base)
                    self._attach_monitor(dev)
                else:
                    old = existing.device
                    existing.device = d
                    dev = existing
                    if (old.name, old.firmware_mcu, old.firmware_wifi) == (d.name, d.firmware_mcu, d.firmware_wifi):
                        continue
            if self.local.is_connected():
                self.announce(dev)

    def _attach_monitor(self, dev: DeviceBridge) -> None:
        for rule in self.cfg["monitor"]["devices"]:
            if rule_matches(rule, dev):
                dev.monitor = DeviceMonitor(dev, rule, self.cfg["monitor"])
                dev.monitor.limit = self.account.cache.alarm_temperature(dev.device.id, rule["temperature"])
                log.info("[%s] monitoring: %s", dev.name, dev.monitor.describe())
                return

    def _refresh_devices(self) -> None:
        try:
            self._fetch_devices()
        except (ApiError, requests.RequestException) as exc:
            log.warning("device list refresh failed: %s", exc)
            return
        self._sync_devices()

    # main loop ------------------------------------------------------------ #
    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job[0] == "stop":
                return
            try:
                if job[0] == "announce":
                    with self.devices_lock:
                        devs = list(self.devices.values())
                    for dev in devs:
                        self.announce(dev)
                elif job[0] == "command":
                    _, dev, what, payload = job
                    dev.handle(what, payload)
                elif job[0] == "refresh_devices":
                    self._refresh_devices()
                elif job[0] == "extend":
                    job[1].extend_timer()
            except (ValueError, MqttError) as exc:
                log.error("%s failed: %s", job[0], exc)
            except Exception:  # noqa: BLE001
                log.exception("%s failed", job[0])

    def run(self) -> int:
        if not self._load_initial_devices():
            return 0 if self.stop_event.is_set() else EXIT_AUTH
        self._sync_devices()
        if not self.devices:
            log.warning("no devices to bridge; will look again every device_refresh_interval")
        for rule in self.cfg["monitor"]["devices"]:
            if not any(rule_matches(rule, dev) for dev in self.devices.values()):
                log.warning("monitor: device %r is not bridged (check the name and the devices list)", rule["name"])

        m = self.cfg["mqtt"]
        log.info("connecting to local broker %s:%s (base topic %s)", m["host"], m["port"], self.base_topic)
        self.local.connect_async(m["host"], m["port"], keepalive=m["keepalive"])
        self.local.loop_start()
        worker = threading.Thread(target=self._worker, name="kb-worker", daemon=True)
        worker.start()

        refresh = self.cfg["account"]["device_refresh_interval"]
        self.next_device_refresh = time.monotonic() + refresh
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                with self.devices_lock:
                    devs = list(self.devices.values())
                for dev in devs:
                    if self.stop_event.is_set():
                        break
                    dev.tick(now)
                if refresh and now >= self.next_device_refresh:
                    self.next_device_refresh = now + refresh
                    self.jobs.put(("refresh_devices",))
                self.stop_event.wait(1.0)
        except KeyboardInterrupt:
            pass
        self._shutdown(worker)
        return 0

    def _shutdown(self, worker: threading.Thread) -> None:
        log.info("shutting down")
        self.stop_event.set()
        self.jobs.put(("stop",))
        worker.join(timeout=5)
        for dev in list(self.devices.values()):
            dev.close()
            self.publish(f"{dev.base}/availability", "offline", retain=True)
        info = self.publish(self.bridge_status_topic, "offline", retain=True, qos=1)
        try:
            info.wait_for_publish(timeout=3)
        except (RuntimeError, ValueError):
            pass
        self.local.disconnect()
        self.local.loop_stop()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def setup_logging(cfg: dict, level_override: Optional[str], console: bool) -> None:
    level = log_level(level_override or cfg["level"])
    fmt = logging.Formatter(cfg["format"])
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    if console:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
    if cfg["file"]:
        Path(cfg["file"]).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            cfg["file"], maxBytes=cfg["max_bytes"], backupCount=cfg["backup_count"], encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    logging.getLogger("urllib3").setLevel(max(level, logging.INFO))


def daemonize(pid_file: Optional[str]) -> None:
    if os.name != "posix":
        raise SystemExit("--daemon needs a POSIX system; run in the foreground or as a service")
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    os.umask(0o027)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if pid_file:
        Path(pid_file).write_text(f"{os.getpid()}\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        prog="kitchenboss-bridge",
        description="Bridge KitchenBoss cookers to a local MQTT broker with Home Assistant discovery. "
                    "Runs in the foreground and logs to the console unless --daemon is given.",
    )
    ap.add_argument("-c", "--config", help="YAML config (default: first of $KITCHENBOSS_BRIDGE_CONFIG, "
                                            "./config.yaml, ~/.kitchenboss/config.yaml, /etc/kitchenboss/config.yaml)")
    ap.add_argument("-l", "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], type=str.upper,
                    help="override logging.level")
    ap.add_argument("--daemon", action="store_true", help="detach into the background (POSIX; logs only to logging.file)")
    ap.add_argument("--pid-file", help="with --daemon: write the PID here")
    ap.add_argument("--check", action="store_true", help="validate the config and exit")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    try:
        path = Path(args.config).expanduser().resolve() if args.config else find_config().resolve()
        cfg, warnings = load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.check:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"{path}: OK")
        return 0
    if args.daemon:
        if not cfg["logging"]["file"]:
            print("warning: --daemon without logging.file - log output is discarded", file=sys.stderr)
        daemonize(args.pid_file)

    setup_logging(cfg["logging"], args.log_level, console=not args.daemon)
    for w in warnings:
        log.warning(w)
    log.info("kitchenboss-bridge %s starting, config %s", __version__, path)

    bridge = Bridge(cfg)

    def _stop(signum, frame):
        log.info("signal %s received", signum)
        bridge.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _stop)
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
