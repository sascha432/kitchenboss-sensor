"""
Bluetooth LE provisioning for KitchenBoss cookers (JSON over GATT).

The cooker advertises as "KB-<model>" (e.g. KB-G322PT) while in pairing mode.
The app writes UTF-8 JSON to the write characteristic and receives UTF-8 JSON
on the notify characteristic.  Requires `bleak`.

Setup flow replicated from WifiPwdActivity / WifiproActivity:

  1. cmd 1  {ssid, password}        -> {mac, area_code, wifi_ver, mcu_ver}
  2. REST   device-app-info/create  -> device id, endpoint, privateKey, certificate
  3. cmd 2  item=host  (endpoint)      \
     cmd 2  item=devid (device id)      |  64-char chunks, each base64 encoded
     cmd 2  item=key   (private key)    |  wait for reply after each chunk
     cmd 2  item=crt   (certificate)   /
  4. cmd 3  {}                         -> code 200
  5. cmd 4  {count} polled            -> state 6 (wifi), 7 (cloud), 8 (done); 106..108 failed
  6. cmd 5  {}                         exit provisioning
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any, Optional

from . import protocol as P

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = None  # type: ignore
    BleakScanner = None  # type: ignore


class BleError(Exception):
    pass


def _require_bleak() -> None:
    if BleakClient is None:
        raise BleError("bleak is not installed: pip install bleak")


def chunk_string(text: str, size: int = P.BLE_CHUNK_LEN) -> list[str]:
    text = text.replace("\r", "")
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class KitchenBossBle:
    def __init__(self, address: str, log=print):
        _require_bleak()
        self.address = address
        self.log = log
        self.client: Optional[BleakClient] = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._buf = bytearray()

    async def __aenter__(self) -> "KitchenBossBle":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        self.client = BleakClient(self.address)
        await self.client.connect()
        await self.client.start_notify(P.BLE_NOTIFY_UUID, self._on_notify)

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop_notify(P.BLE_NOTIFY_UUID)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    def _on_notify(self, _handle, data: bytearray) -> None:
        # Each notification normally carries one complete JSON document
        # (the app requests MTU 512). Buffer in case it gets fragmented.
        self._buf.extend(data)
        try:
            text = self._buf.decode("utf-8")
            obj = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            if len(self._buf) > 4096:
                self._buf.clear()
            return
        self._buf.clear()
        self._queue.put_nowait(obj)

    async def send(self, cmd: int, payload: Optional[dict] = None, key: str = "data") -> None:
        msg = {"cmd": cmd, key: payload or {}}
        raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        assert self.client is not None
        await self.client.write_gatt_char(P.BLE_WRITE_UUID, raw, response=True)

    async def recv(self, cmd: Optional[int] = None, timeout: float = 10.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BleError(f"timeout waiting for BLE reply cmd={cmd}")
            obj = await asyncio.wait_for(self._queue.get(), remaining)
            if cmd is None or obj.get("cmd") == cmd:
                return obj
            self.log(f"(ignoring reply {obj})")

    async def call(self, cmd: int, payload: Optional[dict] = None, key: str = "data", timeout: float = 10.0) -> dict:
        await self.send(cmd, payload, key)
        return await self.recv(cmd, timeout)

    # high level --------------------------------------------------------- #
    async def device_info(self) -> dict:
        reply = await self.call(P.BLE_CMD_GET_DEVICE_INFO)
        self._check(reply)
        return reply.get("data") or {}

    async def set_wifi(self, ssid: str, password: str) -> dict:
        # note: the app uses key "param" for this command only
        reply = await self.call(P.BLE_CMD_SET_WIFI, {"ssid": ssid, "password": password}, key="param", timeout=20)
        self._check(reply)
        return reply.get("data") or {}

    async def wifi_list(self, seconds: float = 12.0) -> list[dict]:
        await self.send(P.BLE_CMD_WIFI_LIST)
        found: dict[str, dict] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                obj = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                break
            if obj.get("cmd") != P.BLE_CMD_WIFI_LIST:
                continue
            d = obj.get("data") or {}
            if d.get("ssid"):
                found[d["ssid"]] = d
            if d.get("num") and d.get("inx") is not None and d["inx"] + 1 >= d["num"]:
                break
        return sorted(found.values(), key=lambda x: -int(x.get("rssi", -100)))

    async def push_item(self, item: str, value: str) -> None:
        chunks = chunk_string(value)
        for idx, chunk in enumerate(chunks):
            b64 = base64.b64encode(chunk.encode("utf-8")).decode("ascii")
            reply = await self.call(P.BLE_CMD_SET_ITEM, {"item": item, "num": len(chunks), "inx": idx, "dat": b64}, timeout=15)
            self._check(reply)
            self.log(f"  {item}: chunk {idx + 1}/{len(chunks)} ok")

    async def start_provision(self) -> None:
        reply = await self.call(P.BLE_CMD_START_PROVISION, timeout=15)
        self._check(reply)

    async def query_result(self, count: int = 0) -> dict:
        reply = await self.call(P.BLE_CMD_QUERY_RESULT, {"count": count}, timeout=15)
        return reply.get("data") or {}

    async def exit_provision(self) -> None:
        try:
            await self.send(P.BLE_CMD_EXIT_PROVISION)
            await self.recv(P.BLE_CMD_EXIT_PROVISION, timeout=3)
        except BleError:
            pass

    @staticmethod
    def _check(reply: dict) -> None:
        code = reply.get("code", 200)
        if int(code) != 200:
            raise BleError(f"device rejected cmd {reply.get('cmd')}: code={code} message={reply.get('message')}")


# --------------------------------------------------------------------------- #
async def scan(seconds: float = 8.0) -> list[tuple[str, str, Optional[int]]]:
    _require_bleak()
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        if P.BLE_NAME_FILTER in name:
            out.append((addr, name, adv.rssi))
    return sorted(out, key=lambda x: -(x[2] or -100))


def model_from_name(name: str) -> str:
    # "KB-G322PT" -> "G322PT"  (app: name.split("-")[1])
    parts = name.split("-")
    return parts[1] if len(parts) > 1 else name


async def provision(ctx, address: str, ssid: str, password: str,
                    model: Optional[str], mac: Optional[str], area: Optional[str]) -> int:
    from .api import ApiError

    if not ctx.cfg.token:
        raise BleError("log in first: kitchenboss login <email>")

    ble_name = ""
    try:
        for addr, name, _ in await scan(5.0):
            if addr.lower() == address.lower():
                ble_name = name
                break
    except Exception:  # noqa: BLE001
        pass
    model = model or (model_from_name(ble_name) if ble_name else None)
    if not model:
        raise BleError("could not derive model from BLE name; pass --model G322PT")

    async with KitchenBossBle(address, log=print) as ble:
        print(f"connected to {address} ({ble_name or 'unknown name'}), model {model}")
        try:
            info = await ble.device_info()
            print("device info:", json.dumps(info))
        except BleError as exc:
            print(f"(device info failed: {exc})")
            info = {}

        print(f"sending Wi-Fi credentials for {ssid!r} ...")
        wifi = await ble.set_wifi(ssid, password)
        print("device accepted:", json.dumps(wifi))
        area_code = area or wifi.get("area_code") or info.get("area_code") or ""
        mac_addr = mac or address
        print(f"binding in cloud: model={model} mac={mac_addr} area={area_code}")
        try:
            dev = ctx.api_call(ctx.api.bind_device, model, mac_addr, area_code)
        except ApiError as exc:
            await ble.exit_provision()
            raise BleError(f"cloud bind failed: {exc.msg}") from exc
        print(f"cloud device id {dev.id}, endpoint {dev.endpoint}")
        if not dev.has_mqtt_credentials:
            await ble.exit_provision()
            raise BleError("cloud did not return endpoint/certificate/privateKey")

        print("pushing credentials to device ...")
        await ble.push_item("host", dev.endpoint)
        await ble.push_item("devid", dev.id)
        await ble.push_item("key", dev.private_key)
        await ble.push_item("crt", dev.certificate)

        print("starting network provisioning ...")
        await ble.start_provision()
        count = 0
        seen: set[int] = set()
        for _ in range(60):
            await asyncio.sleep(2.0)
            res = await ble.query_result(count)
            state = int(res.get("state", -1))
            if state not in seen:
                seen.add(state)
                label = {6: "Wi-Fi connected", 7: "cloud connected", 8: "done"}.get(state, f"state {state}")
                print(f"  {label} {res.get('msg') or ''}")
            if state == P.BLE_STATE_DONE:
                print("provisioning complete")
                await ble.exit_provision()
                ctx.refresh_devices()
                return 0
            if state in P.BLE_STATE_FAILED:
                print(f"provisioning FAILED (state {state}: {res.get('msg')})")
                await ble.exit_provision()
                return 1
        await ble.exit_provision()
        print("timeout waiting for provisioning result")
        return 1


def run_cli(ctx) -> int:
    args = ctx.args
    _require_bleak()

    async def _run() -> int:
        if args.ble_command == "scan":
            devs = await scan(args.seconds)
            if not devs:
                print("no KitchenBoss devices found (put the cooker in pairing mode)")
                return 1
            for addr, name, rssi in devs:
                print(f"{addr}  {name}  rssi={rssi}")
            return 0
        if args.ble_command == "info":
            async with KitchenBossBle(args.address) as ble:
                print(json.dumps(await ble.device_info(), indent=2))
            return 0
        if args.ble_command == "wifi-list":
            async with KitchenBossBle(args.address) as ble:
                for net in await ble.wifi_list(args.seconds):
                    print(f"{net.get('rssi', ''):>5}  {net.get('ssid')}")
            return 0
        if args.ble_command == "raw":
            obj = json.loads(args.json)
            async with KitchenBossBle(args.address) as ble:
                assert ble.client is not None
                await ble.client.write_gatt_char(P.BLE_WRITE_UUID, json.dumps(obj).encode(), response=True)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + args.seconds
                while loop.time() < deadline:
                    try:
                        print(json.dumps(await asyncio.wait_for(ble._queue.get(), deadline - loop.time())))
                    except asyncio.TimeoutError:
                        break
            return 0
        if args.ble_command == "provision":
            return await provision(ctx, args.address, args.ssid, args.password, args.model, args.mac, args.area)
        return 2

    try:
        return asyncio.run(_run())
    except BleError as exc:
        print(f"BLE error: {exc}", file=sys.stderr)
        return 5
