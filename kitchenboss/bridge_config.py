"""YAML configuration for the MQTT bridge (kitchenboss-bridge)."""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import DEFAULT_PATH

DEFAULTS: dict[str, Any] = {
    "account": {
        "email": "",
        "password": "",
        # token + device cache (holds the device certificates); null = KITCHENBOSS_CONFIG or ~/.kitchenboss/config.json
        "cache_file": None,
        # re-read the device list from the cloud every N seconds (0 = only at start)
        "device_refresh_interval": 3600,
    },
    # names, ids or MAC addresses of the cookers to bridge; empty = every device on the account
    "devices": [],
    "cloud": {
        "client_id": None,          # AWS IoT client id (default: random, app-like prefix)
        "mimic_app": False,         # use the app's client id; kicks the phone app off the broker
        "timeout": 10.0,            # seconds for connect / command replies
        "reconnect_max_delay": 300,  # max backoff between failed cloud connects
    },
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
        "client_id": "kitchenboss-bridge",
        "keepalive": 60,
        "qos": 1,
        "retain": True,
        "tls": False,
        "tls_ca_certs": None,
        "tls_certfile": None,
        "tls_keyfile": None,
        "tls_insecure": False,
        "update_interval": 60,      # seconds between cook_get polls
        "offline_after": None,      # seconds without a report before a cooker is unavailable (default 3x interval, min 180)
        "topic_prefix": "home",
        "base_topic": "kitchenboss",
        "auto_discovery": True,
        "auto_discovery_prefix": "homeassistant",
    },
    "cooking": {
        "default_temperature_c": 56.0,  # setpoint used by "start" until changed
        "default_temperature_f": 133.0,
        "default_time": 90,             # minutes
        "apply_while_running": True,    # setpoint changes during a cook are sent to the cooker immediately
    },
    "monitor": {
        "pause_warning": 15,        # minutes paused before an alert
        "restart_hours": 24,        # restart=true: timer is set back to this (max 99, 99:59:59 is rejected)
        "hysteresis": 0.5,          # °C above the limit before a temperature alarm clears
        # - name: <device name, id or MAC>
        #   restart: true        keep the timer at restart_hours while cooking
        #   temperature: 60.0    alarm when the water drops below this (°C)
        "devices": [],
    },
    "logging": {
        "level": "DEBUG",
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        "file": None,
        "max_bytes": 5_000_000,
        "backup_count": 3,
    },
}

# Secrets may come from the environment (e.g. a systemd EnvironmentFile).
ENV_OVERRIDES = {
    "KITCHENBOSS_EMAIL": ("account", "email"),
    "KITCHENBOSS_PASSWORD": ("account", "password"),
    "KITCHENBOSS_MQTT_USERNAME": ("mqtt", "username"),
    "KITCHENBOSS_MQTT_PASSWORD": ("mqtt", "password"),
}
MONITOR_DEVICE_KEYS = {"name": None, "restart": False, "temperature": None}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    pass


def search_paths() -> list[Path]:
    paths = []
    env = os.environ.get("KITCHENBOSS_BRIDGE_CONFIG")
    if env:
        paths.append(Path(env))
    paths += [
        Path("config.yaml"),
        Path.home() / ".kitchenboss" / "config.yaml",
        Path("/etc/kitchenboss/config.yaml"),
    ]
    return paths


def find_config() -> Path:
    candidates = search_paths()
    for p in candidates:
        if p.is_file():
            return p
    raise ConfigError("no config file found, tried: " + ", ".join(str(p) for p in candidates)
                      + " (copy config.example.yaml, or pass --config)")


def _merge(base: dict, override: dict, prefix: str, warnings: list[str]) -> None:
    for key, value in override.items():
        name = f"{prefix}{key}"
        if key not in base:
            warnings.append(f"unknown config option {name!r} ignored")
            continue
        if isinstance(base[key], dict):
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ConfigError(f"{name}: expected a section, got {value!r}")
            _merge(base[key], value, name + ".", warnings)
        else:
            base[key] = value


def _num(cfg: dict, section: str, key: str, kind=float, minimum: Optional[float] = None,
         maximum: Optional[float] = None, optional: bool = False):
    value = cfg[section][key]
    if value is None and optional:
        return
    try:
        value = kind(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}.{key}: expected a number, got {value!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{section}.{key}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{section}.{key}: must be <= {maximum}")
    cfg[section][key] = value


def _path(value) -> Optional[Path]:
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser().resolve()


def _validate(cfg: dict) -> None:
    acc = cfg["account"]
    acc["email"] = str(acc["email"] or "").strip()
    acc["password"] = "" if acc["password"] is None else str(acc["password"])
    if not acc["email"]:
        raise ConfigError("account.email is required")
    acc["cache_file"] = _path(acc["cache_file"]) or DEFAULT_PATH
    _num(cfg, "account", "device_refresh_interval", int, 0)

    devices = cfg["devices"]
    if devices is None:
        devices = []
    elif isinstance(devices, (str, int)):
        devices = [devices]
    if not isinstance(devices, list):
        raise ConfigError("devices: expected a list of names, ids or MAC addresses")
    cfg["devices"] = [str(d).strip() for d in devices if str(d).strip()]

    _num(cfg, "cloud", "timeout", float, 1)
    _num(cfg, "cloud", "reconnect_max_delay", float, 5)

    m = cfg["mqtt"]
    m["host"] = str(m["host"] or "").strip()
    if not m["host"]:
        raise ConfigError("mqtt.host is required")
    _num(cfg, "mqtt", "port", int, 1, 65535)
    _num(cfg, "mqtt", "keepalive", int, 5)
    _num(cfg, "mqtt", "qos", int, 0, 2)
    _num(cfg, "mqtt", "update_interval", float, 5)
    _num(cfg, "mqtt", "offline_after", float, m["update_interval"], optional=True)
    for key in ("topic_prefix", "base_topic", "auto_discovery_prefix"):
        m[key] = str(m[key] or "").strip().strip("/")
    if not m["base_topic"]:
        raise ConfigError("mqtt.base_topic must not be empty")
    if not m["auto_discovery_prefix"]:
        m["auto_discovery_prefix"] = "homeassistant"
    for key in ("tls_ca_certs", "tls_certfile", "tls_keyfile"):
        m[key] = _path(m[key])
    for key in ("retain", "tls", "tls_insecure", "auto_discovery"):
        m[key] = bool(m[key])
    cfg["cloud"]["mimic_app"] = bool(cfg["cloud"]["mimic_app"])
    cfg["cooking"]["apply_while_running"] = bool(cfg["cooking"]["apply_while_running"])

    _num(cfg, "cooking", "default_temperature_c", float, 1, 100)
    _num(cfg, "cooking", "default_temperature_f", float, 33, 212)
    _num(cfg, "cooking", "default_time", float, 1, 5999)

    lg = cfg["logging"]
    lg["level"] = str(lg["level"] or "DEBUG").upper()
    if lg["level"] not in LOG_LEVELS:
        raise ConfigError(f"logging.level: expected one of {', '.join(LOG_LEVELS)}")
    lg["format"] = str(lg["format"] or DEFAULTS["logging"]["format"])
    lg["file"] = _path(lg["file"])
    _num(cfg, "logging", "max_bytes", int, 0)
    _num(cfg, "logging", "backup_count", int, 0)


def _validate_monitor(mon: dict, warnings: list[str]) -> None:
    for key, minimum in (("pause_warning", 1), ("hysteresis", 0), ("restart_hours", 2)):
        try:
            mon[key] = float(mon[key])
        except (TypeError, ValueError):
            raise ConfigError(f"monitor.{key}: expected a number") from None
        if mon[key] < minimum:
            raise ConfigError(f"monitor.{key}: must be >= {minimum}")
    if mon["restart_hours"] > 99:
        raise ConfigError("monitor.restart_hours: the cooker accepts at most 99 h")

    devices = mon["devices"] or []
    if not isinstance(devices, list):
        raise ConfigError("monitor.devices: expected a list")
    rules = []
    for i, entry in enumerate(devices):
        name = f"monitor.devices[{i}]"
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise ConfigError(f"{name}: expected a mapping with name/restart/temperature")
        for k in entry:
            if k not in MONITOR_DEVICE_KEYS:
                warnings.append(f"unknown config option {name}.{k!s} ignored")
        rule = {k: entry.get(k, d) for k, d in MONITOR_DEVICE_KEYS.items()}
        rule["name"] = str(rule["name"] or "").strip()
        if not rule["name"]:
            raise ConfigError(f"{name}.name is required")
        rule["restart"] = bool(rule["restart"])
        if rule["temperature"] is not None:
            try:
                rule["temperature"] = float(rule["temperature"])
            except (TypeError, ValueError):
                raise ConfigError(f"{name}.temperature: expected a number (°C)") from None
            if not 1 <= rule["temperature"] <= 100:
                raise ConfigError(f"{name}.temperature: must be 1..100 °C")
        rules.append(rule)
    mon["devices"] = rules


def load_config(path: Path) -> tuple[dict, list[str]]:
    """Returns (config, warnings). Warnings are logged once logging is set up."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"{path}: file not found")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    cfg = copy.deepcopy(DEFAULTS)
    warnings: list[str] = []
    _merge(cfg, data, "", warnings)
    for env, keys in ENV_OVERRIDES.items():
        if os.environ.get(env):
            target = cfg
            for k in keys[:-1]:
                target = target[k]
            target[keys[-1]] = os.environ[env]
    _validate(cfg)
    _validate_monitor(cfg["monitor"], warnings)
    if cfg["mqtt"]["password"] and not cfg["mqtt"]["username"]:
        warnings.append("mqtt.password is set but mqtt.username is empty; connecting without credentials")
    if not cfg["account"]["password"]:
        warnings.append("account.password is empty; only a cached session can be used")
    return cfg, warnings


def log_level(name: str) -> int:
    return getattr(logging, name.upper(), logging.DEBUG)
