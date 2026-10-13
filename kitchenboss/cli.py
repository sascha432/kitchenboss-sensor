"""Command line interface: python -m kitchenboss ..."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

from . import protocol as P
from .api import ApiError, Device, KitchenBossApi
from .config import DEFAULT_PATH, Config
from .mqtt import KitchenBossMqtt, MqttError
from .protocol import CookStatus, DeviceLanguage, TempUnit, WorkState

log = logging.getLogger("kitchenboss")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_duration(text: str) -> int:
    """'1h30m' | '90m' | '5400s' | '2:30' (h:mm) | '90' (minutes) -> seconds."""
    t = text.strip().lower()
    if re.fullmatch(r"\d+", t):
        return int(t) * 60
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s
    total = 0
    pos = 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([hms])", t):
        if m.start() != pos:
            break
        pos = m.end()
        val = float(m.group(1))
        total += int(val * {"h": 3600, "m": 60, "s": 1}[m.group(2)])
    if pos != len(t) or total == 0:
        raise argparse.ArgumentTypeError(f"cannot parse duration {text!r} (use e.g. 1h30m, 90m, 2:30)")
    return total


def parse_unit(text: str) -> TempUnit:
    try:
        return TempUnit.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def parse_language(text: str) -> DeviceLanguage:
    try:
        return DeviceLanguage.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def parse_onoff(text: str) -> bool:
    t = text.strip().lower()
    if t in ("on", "1", "true", "yes"):
        return True
    if t in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def jprint(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


class CliError(Exception):
    pass


class Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = Config(Path(args.config))
        self._api: Optional[KitchenBossApi] = None

    # api ------------------------------------------------------------------ #
    @property
    def api(self) -> KitchenBossApi:
        if self._api is None:
            self._api = KitchenBossApi(
                token=self.cfg.token,
                refresh_token=self.cfg.refresh_token,
                mimic_app_headers=self.args.mimic_app,
            )
        return self._api

    def api_call(self, fn, *a, **kw):
        """Run an API call, refreshing the token once on 401."""
        if not self.cfg.token:
            raise CliError("not logged in - run: kitchenboss login <email>")
        try:
            return fn(*a, **kw)
        except ApiError as exc:
            if str(exc.code) != "401":
                raise
            log.info("token expired, refreshing")
            try:
                self.api.refresh()
            except ApiError as exc2:
                raise CliError(f"session expired and refresh failed ({exc2.msg}); run: kitchenboss login") from exc2
            self.cfg.set_tokens(self.api.token, self.api.refresh_token_value)
            self.cfg.save()
            return fn(*a, **kw)

    def refresh_devices(self) -> list[Device]:
        devices = self.api_call(self.api.devices)
        self.cfg.set_devices(devices)
        self.cfg.save()
        return devices

    def device(self) -> Device:
        selector = getattr(self.args, "device", None)
        dev = self.cfg.find_device(selector)
        if dev is None and self.cfg.token:
            self.refresh_devices()
            dev = self.cfg.find_device(selector)
        if dev is None:
            devs = self.cfg.devices()
            if not devs:
                if not self.cfg.token:
                    raise CliError("not logged in - run: kitchenboss login <email>")
                raise CliError("no devices bound to this account - set the cooker up with the app or: kitchenboss ble provision")
            if selector:
                raise CliError(f"device {selector!r} not found; known: " + ", ".join(f"{d.name} ({d.id})" for d in devs))
            raise CliError("several devices - pick one with -d NAME or: kitchenboss use NAME")
        if not dev.has_mqtt_credentials:
            # list entries normally carry certs; fall back to detail endpoint
            dev = self.api_call(self.api.device, dev.id)
        return dev

    # mqtt ----------------------------------------------------------------- #
    def mqtt(self, device: Optional[Device] = None) -> KitchenBossMqtt:
        dev = device or self.device()
        client = KitchenBossMqtt(dev, client_id=self.args.client_id, mimic_app=self.args.mimic_app)
        print(f"connecting to {client.host} as {client.client_id} for {dev.name} ({dev.model}/{dev.id}) ...", file=sys.stderr)
        return client.connect(timeout=self.args.timeout)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_login(ctx: Context) -> int:
    email = ctx.args.email or ctx.cfg.email or input("email: ")
    password = ctx.args.password or getpass.getpass("password: ")
    api = KitchenBossApi(mimic_app_headers=ctx.args.mimic_app)
    data = api.login(email, password)
    ctx.cfg.set_login(email, data)
    ctx.cfg.save()
    ctx._api = api
    print(f"logged in as {data.get('nickname') or email} (userId {data.get('userId')})")
    devices = ctx.refresh_devices()
    print(f"{len(devices)} device(s) cached in {ctx.cfg.path}")
    for d in devices:
        print(f"  - {d.name}  model={d.model}  id={d.id}  online={d.is_online}")
    return 0


def cmd_devices(ctx: Context) -> int:
    devices = ctx.refresh_devices() if (ctx.args.refresh or not ctx.cfg.devices()) else ctx.cfg.devices()
    if ctx.args.json:
        jprint([d.raw if ctx.args.raw else d.to_dict() for d in devices])
        return 0
    if not devices:
        print("no devices bound to this account")
        return 0
    for d in devices:
        default = "*" if d.id == ctx.cfg.default_device else " "
        print(f"{default} {d.name:<20} model={d.model:<10} id={d.id:<24} mac={d.mac_address:<18} "
              f"online={d.is_online} fw(mcu/wifi)={d.firmware_mcu}/{d.firmware_wifi} endpoint={d.endpoint}")
    return 0


def cmd_use(ctx: Context) -> int:
    dev = ctx.cfg.find_device(ctx.args.device) if ctx.cfg.devices() else None
    if dev is None:
        ctx.refresh_devices()
        dev = ctx.cfg.find_device(ctx.args.device)
    if dev is None:
        raise CliError(f"device {ctx.args.device!r} not found")
    ctx.cfg.default_device = dev.id
    ctx.cfg.save()
    print(f"default device: {dev.name} ({dev.id})")
    return 0


def print_status(st: CookStatus, as_json: bool, raw: bool = False) -> None:
    if as_json:
        jprint(st.raw if raw else st.to_dict())
    else:
        print(st.describe())


def cmd_status(ctx: Context) -> int:
    with ctx.mqtt() as m:
        st = m.get_status(timeout=ctx.args.timeout)
    print_status(st, ctx.args.json, ctx.args.raw)
    return 0


def cmd_attrs(ctx: Context) -> int:
    with ctx.mqtt() as m:
        reply = m.get_attributes(timeout=ctx.args.timeout)
    jprint(reply.raw)
    return 0


def cmd_watch(ctx: Context) -> int:
    as_json = ctx.args.json

    def on_status(st: CookStatus, msg) -> None:
        stamp = time.strftime("%H:%M:%S")
        if as_json:
            d = st.to_dict()
            d["time"] = stamp
            d["cmd"] = msg.cmd
            print(json.dumps(d), flush=True)
        else:
            print(f"[{stamp}] {msg.cmd:<20} {st.work_state.label:<10} cur={st.cur_temp_deg:.1f}{st.unit_symbol} "
                  f"target={st.set_temp_deg:.1f}{st.unit_symbol} remaining={P.fmt_duration(st.rem_time)} "
                  f"err={st.err_code.label} rssi={st.wifi_rssi}", flush=True)

    def on_message(msg) -> None:
        if msg.status is None and ctx.args.all:
            print(f"[{time.strftime('%H:%M:%S')}] {msg.topic} {json.dumps(msg.raw)}", flush=True)

    with ctx.mqtt() as m:
        m.on_message = on_message
        try:
            on_status(m.get_status(timeout=ctx.args.timeout), m.last_message)
        except MqttError as exc:
            print(f"(initial cook_get failed: {exc}; waiting for reports)", file=sys.stderr)
        print("watching - Ctrl-C to stop", file=sys.stderr)
        m.watch(on_status, poll_every=ctx.args.poll)
    return 0


def cmd_start(ctx: Context) -> int:
    unit: TempUnit = ctx.args.unit
    temp = ctx.args.temperature
    if unit == TempUnit.CELSIUS and not (0 < temp <= 100):
        raise CliError("temperature out of range for °C")
    if unit == TempUnit.FAHRENHEIT and not (32 < temp <= 212):
        raise CliError("temperature out of range for °F")
    with ctx.mqtt() as m:
        reply = m.start(temp, unit, ctx.args.time, recipe_id=ctx.args.recipe, timeout=ctx.args.timeout)
        print(f"sent start: {temp:.1f}{unit.symbol} for {P.fmt_duration(ctx.args.time)}")
        if reply is not None:
            print("reply:", json.dumps(reply.raw))
        if ctx.args.wait:
            time.sleep(2)
            print_status(m.get_status(timeout=ctx.args.timeout), ctx.args.json)
    return 0


def _simple_state(ctx: Context, method: str, label: str) -> int:
    with ctx.mqtt() as m:
        reply = getattr(m, method)(timeout=ctx.args.timeout)
        print(f"sent {label}")
        if reply is not None:
            print("reply:", json.dumps(reply.raw))
        if ctx.args.wait:
            time.sleep(2)
            print_status(m.get_status(timeout=ctx.args.timeout), ctx.args.json)
    return 0


def cmd_pause(ctx: Context) -> int:
    return _simple_state(ctx, "pause", "pause (work_state=2)")


def cmd_resume(ctx: Context) -> int:
    return _simple_state(ctx, "resume", "resume (work_state=3)")


def cmd_stop(ctx: Context) -> int:
    return _simple_state(ctx, "stop", "stop (work_state=4)")


def cmd_reset(ctx: Context) -> int:
    return _simple_state(ctx, "reset", "reset (work_state=0)")


def cmd_set(ctx: Context) -> int:
    """Raw cook_set with every field explicit."""
    with ctx.mqtt() as m:
        reply = m.set_cook(
            work_state=WorkState(ctx.args.state),
            set_temp_tenths=P.encode_temp(ctx.args.temperature),
            temp_unit=ctx.args.unit,
            set_time_s=ctx.args.time,
            recipe_id=ctx.args.recipe,
            wait_reply=not ctx.args.no_wait,
            timeout=ctx.args.timeout,
        )
        if reply is not None:
            jprint(reply.raw)
    return 0


def cmd_send(ctx: Context) -> int:
    data = json.loads(ctx.args.data) if ctx.args.data else {}
    with ctx.mqtt() as m:
        msg = P.build_message(ctx.args.cmd, data, m.version)
        reply_cmd = ctx.args.reply or (ctx.args.cmd + "_reply")
        print("->", json.dumps(msg))
        try:
            reply = m.request(msg, reply_cmd, timeout=ctx.args.timeout)
            print("<-", json.dumps(reply.raw))
        except MqttError as exc:
            print(f"no reply: {exc}")
            return 1
    return 0


def cmd_config(ctx: Context) -> int:
    dev = ctx.device()
    if not any(v is not None for v in (ctx.args.name, ctx.args.unit, ctx.args.silent, ctx.args.language)):
        d = ctx.api_call(ctx.api.device_raw, dev.id)
        jprint({k: d.get(k) for k in ("id", "name", "model", "temperatureUnit", "silentSign", "language",
                                       "versionNumberMcu", "versionNumberWifi", "lastOnlineTime", "status",
                                       "macAddress", "serialNum", "area", "unBindingId")})
        return 0
    res = ctx.api_call(
        ctx.api.update_device, dev.id,
        name=ctx.args.name, silent=ctx.args.silent, temp_unit=ctx.args.unit, language=ctx.args.language,
    )
    print("updated:", json.dumps(res, default=str))
    ctx.refresh_devices()
    return 0


def cmd_firmware(ctx: Context) -> int:
    dev = ctx.device()
    jprint(ctx.api_call(ctx.api.firmware_version, dev.id))
    return 0


def cmd_user(ctx: Context) -> int:
    jprint(ctx.api_call(ctx.api.user_info))
    return 0


def cmd_export_certs(ctx: Context) -> int:
    dev = ctx.device()
    out = Path(ctx.args.dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "cert.pem": dev.certificate,
        "key.pem": dev.private_key,
        "root-ca.pem": dev.root_ca,
    }
    for name, content in files.items():
        if content.strip():
            (out / name).write_text(content.strip().replace("\\n", "\n") + "\n", encoding="utf-8")
    (out / "mqtt.json").write_text(json.dumps({
        "endpoint": dev.endpoint, "port": P.MQTT_PORT, "model": dev.model, "device_id": dev.id,
        "topic_cmd": P.topic_mcu_sub(dev.model, dev.id),
        "topic_reply": P.topic_mcu_sub_reply(dev.model, dev.id),
        "topic_report": P.topic_mcu_pub(dev.model, dev.id),
        "version": P.version_number(dev.version),
    }, indent=2), encoding="utf-8")
    print(f"written to {out}/ (cert.pem, key.pem, root-ca.pem, mqtt.json)")
    print("example:")
    print(f"  mosquitto_sub -h {dev.endpoint} -p 8883 --cafile root-ca.pem --cert cert.pem --key key.pem "
          f"-i kb-test -t '{P.topic_wildcard(dev.model, dev.id)}' -v")
    return 0


def cmd_ble(ctx: Context) -> int:
    from . import ble  # imported lazily: bleak is optional

    return ble.run_cli(ctx)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kitchenboss",
        description="Read and control KitchenBoss Wi-Fi sous-vide cookers (G322PT and friends).",
    )
    p.add_argument("--config", default=str(DEFAULT_PATH), help=f"config file (default {DEFAULT_PATH})")
    p.add_argument("--debug", action="store_true", help="verbose logging incl. MQTT payloads")
    p.add_argument("--timeout", type=float, default=10.0, help="network timeout in seconds")
    p.add_argument("--client-id", default=None, help="MQTT client id (default random, app-like prefix)")
    p.add_argument("--mimic-app", action="store_true",
                   help="use the Android app's MQTT client id (android_client_snxtest); kicks the phone app off")
    sub = p.add_subparsers(dest="command", required=True)

    def add_device(sp):
        sp.add_argument("-d", "--device", help="device id, name or MAC (default: configured/only device)")

    s = sub.add_parser("login", help="log in with the KitchenBoss app account and cache devices")
    s.add_argument("email", nargs="?")
    s.add_argument("-p", "--password")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("devices", help="list devices")
    s.add_argument("--refresh", action="store_true", help="fetch from cloud instead of cache")
    s.add_argument("--json", action="store_true")
    s.add_argument("--raw", action="store_true", help="with --json: full server record incl. certificates")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("use", help="set the default device")
    s.add_argument("device")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("status", help="query the cooker (cook_get)")
    add_device(s)
    s.add_argument("--json", action="store_true")
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("attrs", help="query device attributes (attr_get)")
    add_device(s)
    s.set_defaults(func=cmd_attrs)

    s = sub.add_parser("watch", help="print every status report until Ctrl-C")
    add_device(s)
    s.add_argument("--poll", type=float, default=None, help="additionally send cook_get every N seconds")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.add_argument("--all", action="store_true", help="also print non-status messages")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("start", help="start cooking")
    add_device(s)
    s.add_argument("temperature", type=float)
    s.add_argument("-u", "--unit", type=parse_unit, default=TempUnit.CELSIUS, help="C or F (default C)")
    s.add_argument("-t", "--time", type=parse_duration, required=True, help="duration, e.g. 1h30m, 90m, 2:30")
    s.add_argument("--recipe", type=int, default=P.RECIPE_ID_CUSTOM, help="recipe id 0..28 (default 29 = custom)")
    s.add_argument("--wait", action="store_true", help="print status after sending")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_start)

    for name, fn, hlp in (
        ("pause", cmd_pause, "pause cooking"),
        ("resume", cmd_resume, "resume cooking"),
        ("stop", cmd_stop, "stop cooking"),
        ("reset", cmd_reset, "return to idle (what the app sends after 'finished')"),
    ):
        s = sub.add_parser(name, help=hlp)
        add_device(s)
        s.add_argument("--wait", action="store_true", help="print status after sending")
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=fn)

    s = sub.add_parser("set", help="raw cook_set with explicit fields")
    add_device(s)
    s.add_argument("--state", type=int, required=True, choices=[int(w) for w in WorkState],
                   help="work_state: 0 idle, 2 pause, 3 start/cook, 4 stop")
    s.add_argument("--temperature", type=float, required=True)
    s.add_argument("-u", "--unit", type=parse_unit, default=TempUnit.CELSIUS)
    s.add_argument("-t", "--time", type=parse_duration, required=True)
    s.add_argument("--recipe", type=int, default=P.RECIPE_ID_CUSTOM)
    s.add_argument("--no-wait", action="store_true", help="do not wait for cook_set.sub_reply")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("send", help="send an arbitrary command on /mcu/sub and wait for its reply")
    add_device(s)
    s.add_argument("cmd", help="e.g. cook_get.sub, attr_get.sub")
    s.add_argument("data", nargs="?", help="JSON object for body.data")
    s.add_argument("--reply", help="reply cmd to wait for (default: <cmd>_reply)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("config", help="show or change device settings via the cloud (name, unit, sound, language)")
    add_device(s)
    s.add_argument("--name")
    s.add_argument("--unit", type=parse_unit, default=None, help="C or F")
    s.add_argument("--silent", type=parse_onoff, default=None, help="on = mute device sounds")
    s.add_argument("--language", type=parse_language, default=None, help="it fr es de en ja")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("firmware", help="show firmware/version info from the cloud")
    add_device(s)
    s.set_defaults(func=cmd_firmware)

    s = sub.add_parser("user", help="show account info")
    s.set_defaults(func=cmd_user)

    s = sub.add_parser("export-certs", help="write the device certificate/key/CA for use with other MQTT tools")
    add_device(s)
    s.add_argument("dir")
    s.set_defaults(func=cmd_export_certs)

    s = sub.add_parser("ble", help="Bluetooth LE: scan / info / wifi-list / provision (needs bleak)")
    bs = s.add_subparsers(dest="ble_command", required=True)
    b = bs.add_parser("scan", help="list nearby KitchenBoss cookers in pairing mode")
    b.add_argument("--seconds", type=float, default=8.0)
    b = bs.add_parser("info", help="read MAC / area / firmware versions (cmd 0)")
    b.add_argument("address")
    b = bs.add_parser("wifi-list", help="ask the cooker to scan Wi-Fi networks (cmd 6)")
    b.add_argument("address")
    b.add_argument("--seconds", type=float, default=12.0)
    b = bs.add_parser("provision", help="full setup: Wi-Fi creds -> cloud bind -> push certs -> connect")
    b.add_argument("address")
    b.add_argument("--ssid", required=True)
    b.add_argument("--password", required=True)
    b.add_argument("--model", help="override model (default from BLE name KB-<model>)")
    b.add_argument("--mac", help="override MAC sent to the cloud (default: BLE address)")
    b.add_argument("--area", help="override area code (default: reported by device)")
    b = bs.add_parser("raw", help="send a raw JSON command and print replies")
    b.add_argument("address")
    b.add_argument("json")
    b.add_argument("--seconds", type=float, default=5.0)
    s.set_defaults(func=cmd_ble)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles often default to cp1252; keep the degree sign printable.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ctx = Context(args)
    try:
        return args.func(ctx) or 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        if str(exc.code) == "401":
            print("session invalid - run: kitchenboss login", file=sys.stderr)
        return 3
    except MqttError as exc:
        print(f"MQTT error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
