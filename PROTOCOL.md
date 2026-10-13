# KitchenBoss Wi-Fi sous-vide protocol (G322PT, G320PX, G300PT, ...)

Reverse engineered from the Android app **KitchenBoss** (`com.gerun.simmer`, version 1.4.5,
2026-01-31, SHA-256 `997e298c…1ed80`). No public documentation exists. Everything below is
derived from the decompiled app; field semantics that the app does not use are marked *unknown*.

There is **no local LAN protocol**. The cooker talks to AWS IoT Core over MQTT/TLS and the app
does the same. Bluetooth LE is used only for the initial Wi-Fi/cloud provisioning.

```
 phone / this tool ──HTTPS──> simmer.kitchenbossglobal.com   (account, device list, certs, settings)
 phone / this tool ──MQTT/TLS 8883, client cert──> AWS IoT Core <──MQTT/TLS── cooker (Wi-Fi module)
 phone ──BLE (JSON over GATT)──> cooker        (only during setup)
```

---

## 1. REST API

Base URL: `https://simmer.kitchenbossglobal.com/`  
Framework looks like *yudao / ruoyi-vue-pro* (`app-api/...`, `Tenant-Id` header).

### Headers (every request)

| Header            | Value                                              |
|-------------------|----------------------------------------------------|
| `Authorization`   | the raw `accessToken` from login (no `Bearer`)     |
| `Tenant-Id`       | `1`                                                |
| `Accept-Language` | e.g. `en_US`, `de`                                 |
| `Content-Type`    | GET: `application/x-www-form-urlencoded`; POST: the app sends a JSON body but declares `multipart/form-data` (OkHttp quirk). `application/json` is the sane choice; the tool falls back to the app's header on 400/415. |

### Response envelope

```json
{"code": 200, "data": ..., "msg": "..."}
```
`code` is `200` on success (sometimes a string). `401` means token expired → app calls refresh-token or logs in again.

### Endpoints used for device control

| Method | Path | Body / query | Notes |
|---|---|---|---|
| POST | `app-api/member/auth/login` | `{"mailbox": email, "password": md5hex(password), "loginIp": ip}` | password is **lower-case hex MD5** of the plain password. `loginIp` = public IP from `https://checkip.amazonaws.com/`. Returns `accessToken`, `refreshToken`, `expiresTime`, `userId`, `nickname`, `mailbox`, `avatar`. |
| POST | `app-api/member/auth/refresh-token` | form body `refreshToken=…` | app calls it once per day (`tokenTime`); returns new `accessToken` + `refreshToken` |
| GET  | `app-api/member/user/get` | | account info |
| GET  | `app-api/device/device-app-info/list` | | **device list incl. MQTT credentials** (see below) |
| GET  | `app-api/device/device-app-info/get?id=…` | | device detail (`unBindingId`, `cookList`, `userList`, …) |
| POST | `app-api/device/device-app-info/create` | `{"model","macAddress","area","status":0,"notes":""}` | bind a new device (during BLE setup). Returns the device record incl. certs. |
| POST | `app-api/device/device-app-info/updateConfig` | `{"id", "name"?, "silentSign"?, "temperatureUnit"?, "language"?}` | device settings. The cloud forwards these to the cooker (no MQTT message from the app). |
| DELETE | `app-api/device/info-member/unDevice?id=<unBindingId>` | | unbind |
| GET  | `app-api/device/device-app-info/getVersion?deviceId=…` | | firmware versions |
| GET  | `app-api/device/device-app-info/upgradeMcu?deviceId=…` | | trigger MCU OTA |
| GET  | `app-api/device/device-app-info/upgradeWifi?deviceId=…` | | trigger Wi-Fi module OTA |
| GET  | `app-api/device/device-app-info/getDeviceModel` | | supported models |

Setting values for `updateConfig`:

| Field | Values |
|---|---|
| `temperatureUnit` | `"0"` = °F, `"1"` = °C (same encoding as MQTT `temp_unit`) |
| `silentSign` | `"0"` = sound on, `"1"` = silent |
| `language` | `0` it, `1` fr, `2` es, `3` de, `4` en, `5` ja |

### Device record (`deviceInfoDO`)

Relevant fields of each entry in the device list:

```
id              cloud device id            -> used in MQTT topics
model           e.g. "G322PT"              -> used in MQTT topics ("productId")
name            user-given name
macAddress      BLE MAC used at bind time
endpoint        AWS IoT endpoint, e.g. xxxx-ats.iot.<region>.amazonaws.com
certificate     PEM X.509 device certificate   \
privateKey      PEM RSA private key             |  the app uses these for its OWN MQTT connection
rootCA          PEM Amazon root CA              /
thingId         AWS IoT thing id
version         protocol version string, e.g. "1.0.0" -> MQTT "version": 100 (dots removed)
versionNumberMcu / versionNumberWifi   firmware versions
isOnline, lastOnlineTime, language, temperatureUnit, silentSign, serialNum, area, status
```

Note: the app's built-in default broker is `a38cainc5ht0it-ats.iot.cn-northwest-1.amazonaws.com.cn`
(AWS China, Ningxia); the real endpoint always comes from the device record.

---

## 2. MQTT (AWS IoT Core)

* Transport: MQTT 3.1.1 over TLS 1.2, port **8883**, mutual TLS with the **device certificate** from the device record. No username/password.
* Client id used by the app: `"android_client_snx" + account` – `account` is never written in 1.4.5, so every phone connects as `android_client_snxtest`. AWS IoT disconnects duplicate client ids, so use a different id unless you *want* to kick the app off.
* Clean session, keep-alive 3000 s (BannerConfig.SCROLL_TIME), QoS 1 subscribe, QoS 0 publish.
* On connect the app publishes `{"key":"HELLO Android"}` to `iot/test` (harmless, ignored).

### Topics

`{productId}` = `model`, `{deviceId}` = `id` from the device record.

| Topic | Direction | Content |
|---|---|---|
| `/cookie/{productId}/{deviceId}/mcu/sub`       | app → cooker | commands (`cook_set.sub`, `cook_get.sub`, `attr_get.sub`) |
| `/cookie/{productId}/{deviceId}/mcu/sub_reply` | cooker → app | replies (`cook_set.sub_reply`, `cook_get.sub_reply`, `attr_get.sub_reply`, `upgrade.sub_reply`) |
| `/cookie/{productId}/{deviceId}/mcu/pub`       | cooker → app | unsolicited status (`cook_report.pub`) |
| `/cookie/{productId}/{deviceId}/mcu/pub_reply` | app → cooker | (defined, unused by app) |
| `/cookie/{productId}/{deviceId}/wifi/{sub,sub_reply,pub,pub_reply}` | | Wi-Fi-module channel (defined; app only routes `wifi/sub_reply` to the same handler, never publishes) |

The app subscribes to `/cookie/{productId}/{deviceId}/#`.

### Message envelope

```json
{
  "msgId": 12345,                 // random 0..65530
  "version": 100,                 // int(device.version without dots)
  "ts": 1760000000,               // unix seconds
  "body": { "cmd": "<command>", "data": { ... } }
}
```
Replies carry the same envelope; `body` may additionally contain `code` / `msg`.

### Commands (app → cooker, topic `…/mcu/sub`)

#### `cook_set.sub` – set program / change state

```json
{"body":{"cmd":"cook_set.sub","data":{
  "id": 29,            // recipe id 0..28 from the app's recipe DB, anything else -> 29 (custom)
  "set_time": 5400,    // seconds
  "set_temp": 565,     // tenths of a degree in the unit below (56.5 °C)
  "temp_unit": 1,      // 0 = °F, 1 = °C
  "work_state": 3      // requested state, see table
}}}
```
Reply: `cook_set.sub_reply` (body not parsed by the app).

`work_state` values the app **sends**:

| value | meaning |
|---|---|
| 3 | start cooking / resume from pause (device then reports 1 = preheating, later 3 = cooking) |
| 2 | pause |
| 4 | stop |
| 0 | back to idle – sent when the user dismisses the "finished" screen, with recipe 0, `set_time` 6000 and `set_temp` 500 (°C) / 1200 (°F) |

Pause/resume/stop resend the current `set_time`/`set_temp`/`temp_unit` with the new `work_state`.
Temperature range in the app's UI: 0–100 °C / 32–212 °F (device itself probably clamps to 40–90 °C / 104–194 °F per the manual).

#### `cook_get.sub` – query status

```json
{"body":{"cmd":"cook_get.sub","data":{}}}
```
Reply `cook_get.sub_reply` with the status structure below.

#### `attr_get.sub` – query device attributes

```json
{"body":{"cmd":"attr_get.sub","data":{}}}
```
Reply `attr_get.sub_reply`; content *unknown* (the app sends it but never parses the reply).

### Status (cooker → app, `cook_report.pub` on `…/mcu/pub`, and `cook_get.sub_reply`)

```json
{"body":{"cmd":"cook_report.pub","data":{
  "id": "29",          // recipe id
  "set_time": 5400,    // seconds
  "rem_time": 5321,    // seconds remaining (app shows "ready at" = now + rem_time)
  "total_time": 0,     // seconds (unknown exact meaning; probably elapsed/total incl. preheat)
  "set_temp": 565,     // tenths of a degree
  "cur_temp": 231,     // tenths of a degree, current water temperature
  "temp_unit": 1,      // 0 = °F, 1 = °C
  "work_state": 1,     // see table
  "err_code": 0,       // see table
  "wifi_rssi": -55     // dBm
}}}
```

`work_state` as **reported**:

| value | meaning (app label) |
|---|---|
| 0 | idle ("Cook") |
| 1 | preheating |
| 2 | paused |
| 3 | cooking |
| 4 | finished (app shows "cooking finished" popup) |

`err_code`:

| value | meaning (app label) |
|---|---|
| 0 | none |
| 1 | OVERHEAT |
| 2 | LOW WATER LEVEL ("Add more water") |
| 3 | OVERTIMED |
| 4 | TEMP. SENSOR FAULT |

The cooker sends `cook_report.pub` periodically on its own while it is online; the app also sends
`cook_get.sub` when a screen opens.

#### `upgrade.sub_reply` – OTA progress (cooker → app)

```json
{"body":{"cmd":"upgrade.sub_reply","code":200,"msg":"","data":{"otaItem":"wifi"|"mcu","progress":42}}}
```

---

## 3. Bluetooth LE provisioning

Only used to get the cooker onto Wi-Fi and hand it its cloud credentials. Enter pairing mode on the
cooker (Wi-Fi button, see manual); it then advertises as **`KB-<model>`**, e.g. `KB-G322PT`
(the app filters on names containing `KB`).

| | UUID |
|---|---|
| Service | `59462f12-9543-9999-12c8-58b459a2712d` |
| Write characteristic (app → cooker) | `5c3a659e-897e-45e1-b016-007107c96df7` |
| Notify characteristic (cooker → app) | `5c3a659e-897e-45e1-b016-007107c96df6` |

MTU 512 is requested. Payloads are plain UTF-8 JSON, one document per write/notification.
Replies have the shape `{"cmd": n, "code": 200, "message": "...", "data": {...}}`.

| cmd | request | reply `data` |
|---|---|---|
| 0 | `{"cmd":0,"data":{}}` – device info | `{"mac","area_code","wifi_ver","mcu_ver"}` |
| 1 | `{"cmd":1,"param":{"ssid":"…","password":"…"}}` – Wi-Fi credentials (note key `param`) | `{"mac","area_code","wifi_ver","mcu_ver"}` |
| 2 | `{"cmd":2,"data":{"item":"host"\|"devid"\|"key"\|"crt","num":N,"inx":i,"dat":"<base64>"}}` – credential chunks | `{"item","pkg_num","pkg_inx"}` |
| 3 | `{"cmd":3,"data":{}}` – start connecting | code 200 / error |
| 4 | `{"cmd":4,"data":{"count":n}}` – poll result | `{"state": 6\|7\|8\|106\|107\|108, "msg"}` |
| 5 | `{"cmd":5,"data":{}}` – leave provisioning mode | |
| 6 | `{"cmd":6,"data":{}}` – Wi-Fi scan | one reply per network: `{"num","inx","ssid","rssi"}` |

Flow (as the app does it):

1. cmd 1 with SSID/password → reply gives `mac`, `area_code`, firmware versions.
2. `POST device-app-info/create` with `model` (taken from the BLE name after `KB-`), `macAddress`
   (BLE address) and `area` → cloud creates the device and returns `id`, `endpoint`, `privateKey`,
   `certificate`, `rootCA`.
3. Push, in this order, `host` = endpoint, `devid` = id, `key` = privateKey, `crt` = certificate.
   Each string is split into 64-character pieces, each piece base64-encoded, sent as cmd 2 with
   `num` = number of pieces and `inx` = index; wait for the cmd 2 reply before the next piece.
4. cmd 3, then poll cmd 4 every ~2 s: state 6 = Wi-Fi connected, 7 = cloud connected, 8 = done;
   106/107/108 = failure at the respective step.
5. cmd 5 to exit.

Cooking cannot be controlled over BLE – only the commands above exist.

---

## 4. Decompiled sources of interest

| Class | What |
|---|---|
| `com.gerun.simmer.aws.AwsConnect` | MQTT connect (Paho), TLS with device cert, subscribe `#`, topic routing |
| `com.gerun.simmer.aws.Command` | builds `cook_set.sub`, `cook_get.sub`, `attr_get.sub` |
| `com.gerun.simmer.aws.CommandBean` | status / OTA JSON models |
| `com.gerun.simmer.base.MainApplication` | topic templates, status handling |
| `com.gerun.simmer.aty.DeviceDetailActivity`, `CookingActivity`, `CookSuccessActivity` | state machine, temperature scaling |
| `com.gerun.simmer.utils.ContentApi`, `Content`, `OkHttp3Utils` | REST paths, bodies, headers |
| `com.gerun.simmer.ble.BleCommandId`, `BleUtile`, `aty.WifiPwdActivity`, `aty.WifiproActivity` | BLE provisioning |
