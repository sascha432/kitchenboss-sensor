"""
MQTT (AWS IoT Core) client for KitchenBoss sous-vide cookers.

The cloud hands out one X.509 certificate + private key per device.  The
Android app connects to AWS IoT with that same device certificate and talks
to the cooker on /cookie/{model}/{deviceId}/mcu/... topics.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import ssl
import tempfile
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from . import protocol as P
from .api import Device
from .protocol import CookStatus, ParsedMessage, TempUnit, WorkState

log = logging.getLogger("kitchenboss.mqtt")


class MqttError(Exception):
    pass


class CommandRejected(MqttError):
    """The cooker answered with a code other than 200 (e.g. 100 for an out-of-range set_time)."""


class KitchenBossMqtt:
    def __init__(
        self,
        device: Device,
        *,
        client_id: Optional[str] = None,
        mimic_app: bool = False,
        keepalive: int = 60,
        on_status: Optional[Callable[[CookStatus, ParsedMessage], None]] = None,
        on_message: Optional[Callable[[ParsedMessage], None]] = None,
    ):
        if not device.has_mqtt_credentials:
            raise MqttError("device record has no endpoint/certificate/privateKey")
        self.device = device
        self.model = device.model
        self.device_id = device.id
        self.version = P.version_number(device.version)
        self.keepalive = keepalive
        self.on_status = on_status
        self.on_message = on_message

        endpoint = device.endpoint.strip()
        for prefix in ("ssl://", "mqtts://", "tls://"):
            if endpoint.startswith(prefix):
                endpoint = endpoint[len(prefix):]
        host, _, port = endpoint.partition(":")
        self.host = host
        self.port = int(port) if port else P.MQTT_PORT

        if client_id is None:
            # The app uses "android_client_snx" + account; "account" is never
            # stored in 1.4.5, so the app ends up as "android_client_snxtest".
            # Using the same id would kick the phone app off the broker.
            if mimic_app:
                client_id = P.APP_CLIENT_ID_PREFIX + "test"
            else:
                client_id = f"{P.APP_CLIENT_ID_PREFIX}-py-{secrets.token_hex(4)}"
        self.client_id = client_id

        self.topic_cmd = P.topic_mcu_sub(self.model, self.device_id)
        self.topic_all = P.topic_wildcard(self.model, self.device_id)

        self._tmpdir = tempfile.mkdtemp(prefix="kitchenboss-")
        self._ca_path = self._write_pem("root-ca.pem", device.root_ca) if device.root_ca.strip() else None
        self._cert_path = self._write_pem("cert.pem", device.certificate)
        self._key_path = self._write_pem("key.pem", device.private_key)

        self._connected = threading.Event()
        self._conn_rc: Optional[int] = None
        self._lock = threading.Condition()
        self._replies: dict[str, ParsedMessage] = {}
        self.last_status: Optional[CookStatus] = None
        self.last_message: Optional[ParsedMessage] = None

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        try:
            self.client.tls_set(
                ca_certs=self._ca_path,
                certfile=self._cert_path,
                keyfile=self._key_path,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        except (ssl.SSLError, OSError, ValueError) as exc:
            self._cleanup()
            raise MqttError(f"invalid certificate/key for device {device.id}: {exc}") from exc
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

    # ------------------------------------------------------------------ #
    def _write_pem(self, name: str, content: str) -> str:
        path = os.path.join(self._tmpdir, name)
        text = content.strip().replace("\\n", "\n")
        if not text.endswith("\n"):
            text += "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def _cleanup(self) -> None:
        for p in (self._ca_path, self._cert_path, self._key_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # paho callbacks
    # ------------------------------------------------------------------ #
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = int(reason_code.value) if hasattr(reason_code, "value") else int(reason_code)
        self._conn_rc = rc
        if rc == 0:
            log.info("connected to %s:%s as %s", self.host, self.port, self.client_id)
            client.subscribe(self.topic_all, qos=1)
        else:
            log.error("connect failed: %s", reason_code)
        self._connected.set()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        log.debug("subscribed %s -> %s", self.topic_all, reason_codes)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.info("disconnected: %s", reason_code)
        self._connected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            parsed = P.parse_message(msg.topic, msg.payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("unparsable message on %s: %r (%s)", msg.topic, msg.payload[:200], exc)
            return
        log.debug("<- %s %s", msg.topic, msg.payload.decode("utf-8", "replace"))
        self.last_message = parsed
        status = parsed.status
        if status is not None:
            self.last_status = status
        with self._lock:
            self._replies[parsed.cmd] = parsed
            self._lock.notify_all()
        if self.on_message:
            try:
                self.on_message(parsed)
            except Exception:  # noqa: BLE001
                log.exception("on_message callback failed")
        if status is not None and self.on_status:
            try:
                self.on_status(status, parsed)
            except Exception:  # noqa: BLE001
                log.exception("on_status callback failed")

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #
    def connect(self, timeout: float = 15.0) -> "KitchenBossMqtt":
        self._connected.clear()
        self._conn_rc = None
        self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            self.close()
            raise MqttError(f"timeout connecting to {self.host}:{self.port}")
        if self._conn_rc != 0:
            self.close()
            raise MqttError(f"broker refused connection (rc={self._conn_rc})")
        return self

    def close(self) -> None:
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        self._cleanup()

    def __enter__(self) -> "KitchenBossMqtt":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # messaging
    # ------------------------------------------------------------------ #
    def publish(self, message: dict, topic: Optional[str] = None, qos: int = 0) -> None:
        payload = json.dumps(message, separators=(",", ":"))
        log.debug("-> %s %s", topic or self.topic_cmd, payload)
        info = self.client.publish(topic or self.topic_cmd, payload, qos=qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttError(f"publish failed rc={info.rc}")

    def request(self, message: dict, reply_cmd: str, timeout: float = 10.0) -> ParsedMessage:
        with self._lock:
            self._replies.pop(reply_cmd, None)
        self.publish(message)
        deadline = time.monotonic() + timeout
        with self._lock:
            while reply_cmd not in self._replies:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MqttError(f"no {reply_cmd} within {timeout:.0f}s (device offline?)")
                self._lock.wait(remaining)
            return self._replies.pop(reply_cmd)

    def get_status(self, timeout: float = 10.0) -> CookStatus:
        """cook_get.sub -> cook_get.sub_reply"""
        reply = self.request(P.build_cook_get(self.version), P.CMD_COOK_GET_REPLY, timeout)
        status = reply.status
        if status is None:
            raise MqttError(f"unexpected reply: {reply.raw}")
        return status

    def get_attributes(self, timeout: float = 10.0) -> ParsedMessage:
        """attr_get.sub -> attr_get.sub_reply (device attributes; the app never parses this)."""
        return self.request(P.build_attr_get(self.version), P.CMD_ATTR_GET_REPLY, timeout)

    def set_cook(
        self,
        *,
        work_state: WorkState,
        set_temp_tenths: int,
        temp_unit: TempUnit,
        set_time_s: int,
        recipe_id: int | str = P.RECIPE_ID_CUSTOM,
        wait_reply: bool = True,
        timeout: float = 10.0,
    ) -> Optional[ParsedMessage]:
        msg = P.build_cook_set(
            set_time_s=set_time_s,
            set_temp_tenths=set_temp_tenths,
            temp_unit=temp_unit,
            work_state=work_state,
            recipe_id=recipe_id,
            version=self.version,
        )
        if wait_reply:
            reply = self.request(msg, P.CMD_COOK_SET_REPLY, timeout)
            # verified on a G322PX: 200 = applied, 100 (msg still "OK") = ignored, e.g. set_time 99:59:59
            if reply.code is not None and str(reply.code) != "200":
                raise CommandRejected(f"cooker rejected cook_set (code={reply.code} msg={reply.msg})")
            return reply
        self.publish(msg)
        return None

    # convenience ------------------------------------------------------- #
    def start(
        self,
        temperature: float,
        unit: TempUnit,
        duration_s: int,
        recipe_id: int | str = P.RECIPE_ID_CUSTOM,
        timeout: float = 10.0,
    ) -> Optional[ParsedMessage]:
        return self.set_cook(
            work_state=WorkState.COOKING,
            set_temp_tenths=P.encode_temp(temperature),
            temp_unit=unit,
            set_time_s=int(duration_s),
            recipe_id=recipe_id,
            timeout=timeout,
        )

    def _resend_with_state(self, state: WorkState, timeout: float) -> Optional[ParsedMessage]:
        st = self.get_status(timeout)
        return self.set_cook(
            work_state=state,
            set_temp_tenths=st.set_temp,
            temp_unit=st.temp_unit,
            set_time_s=st.set_time,
            recipe_id=st.recipe_id or P.RECIPE_ID_CUSTOM,
            timeout=timeout,
        )

    def pause(self, timeout: float = 10.0) -> Optional[ParsedMessage]:
        return self._resend_with_state(WorkState.PAUSED, timeout)

    def resume(self, timeout: float = 10.0) -> Optional[ParsedMessage]:
        return self._resend_with_state(WorkState.COOKING, timeout)

    def stop(self, timeout: float = 10.0) -> Optional[ParsedMessage]:
        return self._resend_with_state(WorkState.FINISHED, timeout)

    def reset(self, timeout: float = 10.0) -> Optional[ParsedMessage]:
        """What the app sends when the user dismisses the 'cooking finished' screen."""
        st = self.last_status
        unit = st.temp_unit if st else TempUnit.CELSIUS
        temp = 1200 if unit == TempUnit.FAHRENHEIT else 500
        return self.set_cook(
            work_state=WorkState.IDLE, set_temp_tenths=temp, temp_unit=unit,
            set_time_s=6000, recipe_id=0, timeout=timeout,
        )

    def watch(
        self,
        callback: Callable[[CookStatus, ParsedMessage], None],
        poll_every: Optional[float] = None,
    ) -> None:
        """Block until Ctrl-C, invoking callback for every status message.

        If poll_every is given, a cook_get is sent periodically in addition to
        the device's own periodic cook_report.pub messages.
        """
        self.on_status = callback
        try:
            while True:
                if poll_every:
                    try:
                        self.publish(P.build_cook_get(self.version))
                    except MqttError as exc:
                        log.warning("poll failed: %s", exc)
                    time.sleep(poll_every)
                else:
                    time.sleep(1.0)
        except KeyboardInterrupt:
            pass
