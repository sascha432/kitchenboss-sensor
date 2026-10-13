"""
Cook monitor for the MQTT bridge.

- restart: keeps a cook running indefinitely by setting the timer back to monitor.restart_hours
  (default 24 h) whenever less than restart_hours - 1 h remain. The cooker restarts its countdown
  from the new set_time (verified on a G322PX; 99:59:59 is rejected with reply code 100), but only
  when set_time changes; an unchanged value is acknowledged and ignored, so the bridge alternates
  between restart_hours and restart_hours + 1 s.
- paused longer than monitor.pause_warning minutes -> alarm (never resumed automatically; the
  user may be working on the cooker).
- otherwise: alarm once the water temperature drops below the device's temperature limit. The
  alarm is armed only after the limit was reached (no alarm while preheating) and clears at
  limit + hysteresis.
- cooker offline, device error, timer extension not taking effect -> alarm.

Alarms are logged and published to MQTT (<base>/<device>/alert, Home Assistant "Alarm" sensor).
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

from . import protocol as P
from .protocol import ErrorCode, TempUnit, WorkState

if TYPE_CHECKING:
    from .bridge import DeviceBridge

log = logging.getLogger("kitchenboss.monitor")

EXTEND_MARGIN = 3600                        # extend once remaining < restart time - 1 h
EXTEND_RETRY = 300                          # seconds between extension attempts
EXTEND_MAX_ATTEMPTS = 3                     # then alarm "timer extension failed"


def rule_matches(rule: dict, dev: "DeviceBridge") -> bool:
    s = rule["name"].lower()
    d = dev.device
    return s in (d.name.lower(), d.id.lower(), d.mac_address.lower())


class DeviceMonitor:
    def __init__(self, dev: "DeviceBridge", rule: dict, cfg: dict):
        self.dev = dev
        self.restart: bool = rule["restart"]
        self.configured_limit: Optional[float] = rule["temperature"]
        self.limit = self.configured_limit
        self.pause_after = cfg["pause_warning"] * 60
        self.hysteresis = cfg["hysteresis"]
        self.lock = threading.RLock()
        self.alarms: dict[str, str] = {}
        self.armed = False
        self.pause_since: Optional[float] = None
        self.last_extend = 0.0
        self.extend_attempts = 0
        self.restart_time = int(cfg["restart_hours"] * 3600)

    @property
    def alarm(self) -> bool:
        return bool(self.alarms)

    @property
    def reason(self) -> Optional[str]:
        with self.lock:
            return "; ".join(self.alarms.values()) or None

    def describe(self) -> str:
        parts = []
        if self.restart:
            parts.append(f"keep the timer at {P.fmt_duration(self.restart_time)}")
        if self.limit is not None:
            parts.append(f"temperature limit {self.limit:.1f}°C")
        parts.append(f"pause warning after {self.pause_after / 60:.0f} min")
        return ", ".join(parts)

    def set_limit(self, limit: Optional[float]) -> None:
        """Change the temperature alarm limit (None = off); re-armed once the water reaches it."""
        with self.lock:
            self.limit = limit
            self.armed = False
        log.info("[%s] temperature limit %s", self.dev.name, f"{limit:.1f}°C" if limit is not None else "off")
        self.clear_alarm("temperature", "temperature limit changed")

    @property
    def extend_below(self) -> int:
        return max(self.restart_time - EXTEND_MARGIN, self.restart_time // 2)

    # alarms ---------------------------------------------------------------- #
    def raise_alarm(self, key: str, text: str) -> None:
        with self.lock:
            if key in self.alarms:
                return
            self.alarms[key] = text
        log.warning("[%s] ALERT: %s", self.dev.name, text)
        self.dev.bridge.on_alarm_change(self.dev, key, True, text)

    def clear_alarm(self, key: str, text: str) -> None:
        with self.lock:
            if self.alarms.pop(key, None) is None:
                return
        log.warning("[%s] alert cleared: %s", self.dev.name, text)
        self.dev.bridge.on_alarm_change(self.dev, key, False, text)

    # checks (bridge supervisor thread, once per second) ---------------------- #
    def evaluate(self, now: float) -> None:
        dev = self.dev
        st = dev.status
        if st is None:
            return  # never had data; nothing to compare against
        with self.lock:
            if not dev.available:
                self.pause_since = None
                self.raise_alarm("offline", f"cooker offline (no data for {dev.bridge.offline_after / 60:.0f} min)")
                return
            self.clear_alarm("offline", "cooker back online")

            if st.err_code != ErrorCode.NONE:
                self.raise_alarm("error", f"device error {st.err_code.label}")
            else:
                self.clear_alarm("error", "device error cleared")

            if st.work_state == WorkState.PAUSED:
                self.armed = False  # re-arm after the user is done and the water is hot again
                if self.pause_since is None:
                    self.pause_since = now
                    log.info("[%s] paused, warning in %.0f min if still paused", dev.name, self.pause_after / 60)
                elif now - self.pause_since >= self.pause_after:
                    self.raise_alarm("paused", f"paused for {(now - self.pause_since) / 60:.0f} min")
                return
            self.pause_since = None
            self.clear_alarm("paused", f"no longer paused (now {st.work_state.label})")

            if self.limit is not None:
                cur = st.cur_temp_deg
                if st.temp_unit == TempUnit.FAHRENHEIT:
                    cur = P.f_to_c(cur)
                if cur >= self.limit and not self.armed:
                    self.armed = True
                    log.info("[%s] temperature %.1f°C reached limit %.1f°C, alarm armed", dev.name, cur, self.limit)
                if "temperature" in self.alarms:
                    if cur >= self.limit + self.hysteresis:
                        self.clear_alarm("temperature", f"water temperature back to {cur:.1f}°C")
                elif self.armed and cur < self.limit:
                    self.raise_alarm("temperature", f"water temperature {cur:.1f}°C below limit {self.limit:.1f}°C "
                                                    f"(state {st.work_state.label})")

            if self.restart and st.work_state == WorkState.COOKING:
                if st.rem_time >= self.extend_below:
                    if self.extend_attempts:
                        log.info("[%s] timer extended, remaining %s", dev.name, P.fmt_duration(st.rem_time))
                    self.extend_attempts = 0
                    self.clear_alarm("extend", "timer extension works again")
                elif now - self.last_extend >= EXTEND_RETRY:
                    self.last_extend = now
                    if self.extend_attempts >= EXTEND_MAX_ATTEMPTS:
                        self.raise_alarm("extend", f"cannot extend the timer "
                                                   f"(remaining {P.fmt_duration(st.rem_time)})")
                    self.extend_attempts += 1
                    dev.bridge.jobs.put(("extend", dev))
