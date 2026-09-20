# Aiper BLE Diagnostics

An opt-in Home Assistant custom integration for local BLE diagnostics and experimental Surfer S1 telemetry. Version 0.8.0 adds HA device grouping and a guarded poll-now action to the HA-managed Bluetooth/proxy transport introduced in 0.7.0. Existing entries retain their polling options and entity IDs. New entries default to polling disabled. Legacy diagnostic actions remain local-only. See [proxy polling and battery evidence](aiper_ble_proxy_polling.md) for route selection, guard limitations and validation details. It is not an Aiper controller or a replacement for `kmich/ha-aiper`.

## Guarded polling and sensors

Version 0.9.0 adds separate fixed INFO/WARN requests and narrows the default entity
set. Read the [DP validation matrix](info_dp_validation.md) before upgrading:
INFO/WARN are app-derived and still need live validation. Existing enabled polling
will include these additional queries after restart.

In the integration's **Configure** options, enable polling only when exclusive use of this robot's BLE connection can be maintained. The separate recurring-query acknowledgement is required. Pause by disabling polling before opening the app or using another BLE client, and let connection cleanup finish. Enabling is persistent and authorises a first poll on setup/reload/restart.

The default successful-poll interval is **300 seconds**, configurable from 300 to 3600 seconds. Each cycle uses four independently bounded connections: `S1_INFO`, `OpInfo`, `INFO`, then `WARN`, disconnecting after each. All use `request` write mode; OpInfo omits the empty-data checksum, while S1_INFO, INFO and WARN always include their data CRC. HA selects an available adapter or active proxy; the integration never retries a physical connect or replays a query. No control, pairing, provisioning, private scanner or arbitrary query is permitted. This is not write-free: each cycle writes four fixed status requests and toggles notifications.

The entire cycle has a 180-second deadline, followed where necessary by bounded notification/disconnection cleanup. Existing per-stage deadlines still apply. One failed query stops the cycle. Subsequent attempts back off to 10, 20, 40 and at most 60 minutes with the default interval. The explicit `poll_now` action may shorten failure backoff after the normal configured interval has elapsed; it cannot bypass that minimum gap or safety checks. Ordinary entity refreshes retain backoff. All actions share the same in-flight guard. A cleanup failure, ECDH evidence or another explicitly unsupported protocol/security condition suspends polling until reviewed and reloaded. Restart/reload clears the volatile suspension, so review before doing either. Diagnostic downloads never query the device.

If company-zero advertisement evidence is absent, the separately labelled persistent legacy exception must be enabled. It does not override ECDH/key-exchange, malformed advertisements, identity checks or endpoint validation. Unlike the local diagnostic transport, remote backends cannot expose full pairing/trust or connection/notification ownership metadata; exclusive access remains an operator prerequisite.

| Sensor | Meaning |
|---|---|
| Aiper BLE temperature | S1_INFO first value divided by 10, in °C. Physical sensor location is unverified; not labelled water or outdoor temperature. |
| Aiper BLE battery | INFO third integer, accepted only in 0-100 and shown as %. App-derived mapping; live validation pending. |
| Aiper BLE operating status/mode raw | INFO first/second integers. No speculative enum labels. |
| Aiper BLE warning code raw | WARN signed decimal int64. App-derived; no inferred fault labels or statistical state class. |
| Aiper BLE solar status raw | S1_INFO second integer. No charging/not-charging mapping is asserted. |
| Aiper BLE Wi-Fi RSSI raw | Integer `data.wifi_rssi` from OpInfo. No dBm unit or sentinel interpretation is asserted, including for -127. Missing/non-integer data makes this sensor unavailable. |
| Aiper BLE last successful poll | Completion time of the last fully verified cycle. |
| Aiper BLE polling status | Disabled, waiting, ok, busy, failed, interrupted or suspended, with fixed error codes and failure count. |

The historical v0.7.0 candidate list is in [Entity coverage](aiper_ble_proxy_polling.md#entity-coverage).
It included speculative direct OpInfo and nested Machine fields, not confirmed
robot capabilities. After the [entity retirement](entity_retirement.md), 13
entities remain, nine enabled by default: six operational readings and three
status/timestamp indicators. Raw duplicate temperature, time zone and the two
Wi-Fi metadata sensors remain opt-in diagnostics. The 14 speculative
OpInfo/Machine registry entries are removed once, even if renamed or disabled.
Retained optional entities preserve their IDs and user choices. Missing data is
never filled with zero.

Telemetry publishes atomically only after all four replies have the expected query shape, integer `res: 0`, matching legacy CRC and confirmed notification shutdown/disconnection. CRC covers compact UTF-8 JSON `data` in received key order using seed `0x9966`; it detects corruption, not spoofing. Invalid/missing CRC, unsuccessful replies and partial cycles do not publish. Disabled or failed polling makes telemetry unavailable rather than presenting old readings as current. No battery/mode fields are invented from absent data, and serials, raw frames and complete responses never enter telemetry entities or coordinator data. The existing discovery-result entity and unique ID are unchanged; it continues to report manual actions separately.

This remains an **exclusive-access experimental pilot**. Recurring polling uses HA's shared connection routing, including active proxies, while legacy diagnostic services retain pinned local BlueZ exchanges. Remote backends cannot expose all local ownership checks, and no transport can eliminate races with an external client. See HA's [Bluetooth integration guidance](https://developers.home-assistant.io/docs/bluetooth/) and the specific safety differences in the proxy-polling document.

The fixed-query choices follow [PR #238](https://github.com/purcell-lab/ha-config/pull/238). The listen-only action was added in [PR #239](https://github.com/purcell-lab/ha-config/pull/239), and the original polling scheduler in [PR #240](https://github.com/purcell-lab/ha-config/pull/240). Subsequent live polling published CRC-verified S1 readings, but success does not establish reachability through every proxy. See the [discovered BLE specification](AIPER_POOL_ROBOT_BLE_SPEC.md) for observed results and the fail-closed proxy identity limitation.

## One bounded listen-only test

`aiper_ble_diagnostics.listen_once` connects to the configured Surfer S1, enables notifications and observes for at most 60 seconds from the start of subscription. Unlike query mode, startup notifications are retained. No `ReadValue`, `WriteValue`, application query or protocol subscription command is permitted; enabling notifications can still write the CCCD.

```yaml
action: aiper_ble_diagnostics.listen_once
data:
  entry_id: YOUR_CONFIG_ENTRY_ID
  confirm_app_closed: false
  confirm_notifications: false
  confirm_legacy_probe: false
```

Change the first two confirmations only after authorising this run with the app closed, other BLE clients idle and the robot stationary. Missing advertisement evidence requires separately authorising the per-call legacy exception. ECDH, key-exchange characteristics, malformed advertisement data, changed identity, an occupied device or an unsafe endpoint veto the test. A unique resolved endpoint with `notify` is required, but application read/write flags are not.

The listener uses a 15-second setup deadline, a fixed 60-second observation window, a 32-item queue, 64-notification and 8,192-byte capture limits, and the existing 4,096-byte frame decoder. Malformed frames, overflow or disconnect stop the test early, without retry. Cached BlueZ connection properties are checked during silent periods; these checks do not issue GATT reads. Notification shutdown, signal-handler removal and disconnection are attempted on completion, timeout, error and cancellation.

`listen_complete` with zero notifications is a valid observation, not a query timeout. It does not prove that the firmware never sends unsolicited updates: updates might require a state change, an application subscription command or a longer interval. This test observes connected GATT notifications, not connectionless advertisements.

Authenticated action responses retain decoded frames and timestamped raw notification samples for private inspection. Diagnostic downloads redact both collections, and the result sensor exposes neither. Partial trailing frames are counted but not interpreted. Listen-only results do not update telemetry entities; only the separate verified polling path does.

## Safety contract

- **No automatic BLE connection by default:** Setup, reload and startup connect only after recurring polling is explicitly enabled. Diagnostic downloads never connect. There is no automatic discovery flow or button entity.
- **Explicit actions:** `discover` requires `confirm_app_closed: true` and a specific entry. The separate `read_once` action additionally requires `confirm_read_only: true`. Neither is scheduled.
- **Separate protocol action:** `query_once` requires three confirmations, an explicit checksum policy and an explicit write transport. Missing advertisement evidence additionally requires per-call `confirm_legacy_probe: true`, default false and never saved. It enables notifications and writes exactly one fixed request selected by `query_type`: `OpInfo` (default), `S1_INFO`, `INFO` or `WARN`. It is not write-free. Discovery and raw-read transports still cannot write or subscribe.
- **Discovery remains metadata only:** One connect attempt, service/characteristic/descriptor UUIDs and characteristic flags, then disconnect. It cannot call `ReadValue`.
- **Optional read boundary:** `read_once` permits one `ReadValue({})` on the exact Aiper endpoint below, after fresh metadata checks. At most 512 returned bytes are accepted into the result. No application writes, notification subscriptions, descriptor reads/writes, pairing, trust changes, WiFi provisioning or robot-control commands.
- **Guards:** Target and adapter identity checks; reject an existing connection, pairing/trust changes, a blocked device or a powered-off adapter. No fallback adapter, retries, scanner restart or device removal.
- **Bounded cleanup:** Connection timeout 20 seconds, service resolution 10 seconds, read stage 5 seconds (including fresh metadata), disconnect 10 seconds, plus metadata calls bounded to 5 seconds each. Unload and normal HA shutdown cancel an in-flight probe and attempt cleanup. Forced process termination, bus failure or host failure can prevent confirmed disconnection.

This is an active connection test, not passive listening. The Bluetooth stack exchanges discovery packets and may perform generic profile discovery and cache updates. BlueZ's `Connect` method is a generic profile/device connection operation; `ServicesResolved` indicates discovery completion, not successful application authentication ([BlueZ device API](https://bluez.readthedocs.io/en/latest/device-api/)).

## Scope and compatibility

Recurring polling and `poll_now` use HA-managed Bluetooth, including active ESPHome proxies. Only the older one-shot diagnostic actions retain **local BlueZ adapter** scope and require an accessible Linux system bus. No cloud API is used. `dbus-fast` is supplied by the Bluetooth dependency; no independent package version is installed by this component ([HA Bluetooth manifest](https://github.com/home-assistant/core/blob/dev/homeassistant/components/bluetooth/manifest.json)).

The configured device must already appear by an Aiper name in HA's discovery cache, with legacy local metadata as a fallback. Selecting a device does not initiate a BLE scan or connection. Robot identity remains pinned. Recurring polling uses HA routing; only the older local diagnostics use the stored adapter. Existing cloud integrations are not changed.

**Close the Aiper phone app and keep all other clients for this device idle** for tests and the entire time recurring polling is enabled. HA-managed routing does not grant exclusive ownership over external clients. Busy-state guards reduce but cannot eliminate races. Shared-client production use is outside this pilot's scope ([HA Bluetooth development guidance](https://developers.home-assistant.io/docs/bluetooth/)).

This standalone repository contains one custom integration, its tests and documentation. It does not replace files belonging to the separate `aiper` integration.

## Install after review

1. Review the source and compatibility notes in the [repository README](../README.md).
2. Copy only `custom_components/aiper_ble_diagnostics/` into `/config/custom_components/`, or use the standalone repository with HACS.
3. Restart HA once to register the new custom integration.
4. Add **Aiper BLE Diagnostics** from Settings > Devices & services > Add integration.
5. Select the known Aiper device. No BLE connection is made by this setup flow.

Runtime compatibility is exercised in an isolated HA 2026.9.3 test harness with mocked Bluetooth. That does not replace installation and hardware validation on the live instance.

## Run the preflight

Use Developer Tools > Actions, select **Aiper BLE Diagnostics: Check discovery prerequisites**, and choose the integration entry. The entry ID is also exposed as an attribute of the diagnostic result sensor.

```yaml
action: aiper_ble_diagnostics.preflight
data:
  entry_id: "<diagnostic integration entry ID>"
```

Expected status: `preflight_passed_no_connection`. This reads existing local BlueZ metadata only. If it fails, download the integration diagnostics and stop; do not reset Bluetooth or change device settings.

## Run one discovery attempt

Keep the robot stationary in a safe operating condition and ensure nobody needs app control. Close the Aiper app, keep other clients idle, and explicitly authorise the one connection:

```yaml
action: aiper_ble_diagnostics.discover
data:
  entry_id: "<diagnostic integration entry ID>"
  confirm_app_closed: true
```

The connection actions support optional response data, so they can be invoked through HA/MCP once this version is installed. `protocol_preview` requires a response and does not overwrite the last hardware-test result. The latest hardware result is held in memory; the result sensor records only a compact summary. Reload/restart clears the result. Diagnostic downloads redact configured addresses, device names, raw sample hex, complete protocol responses and free-form errors. Authenticated action responses contain target metadata and may contain raw bytes or private device data: keep them private. Fixed `error_code` and `failure_stage` fields survive redaction.

No robot credentials, addresses, serials or webhook secrets are embedded in this repository.

## Prepare one bounded read

This action is separate from discovery and requires separate approval to deploy and execute. Preparation and mock tests are not hardware validation. Keep the robot stationary and the app and all other BLE clients idle for the full attempt.

```yaml
action: aiper_ble_diagnostics.read_once
data:
  entry_id: "<diagnostic integration entry ID>"
  confirm_app_closed: true
  confirm_read_only: true
response_variable: aiper_private_read_result
```

For an MCP service call, request `return_response: true` instead of the script-only `response_variable`. Use a one-off call, not a recurring automation. HA script traces and MCP conversation history may retain response data; do not publish those traces or raw bytes without reviewing them.

The read stage rechecks the pinned device and adapter, connection and service-resolution state, and pairing/trust baseline. It requires exactly one matching service and one matching characteristic, validates parent relationships and object paths, requires the `read` flag and refuses an already-notifying characteristic. It also rejects endpoints advertising `encrypt-read`, `encrypt-authenticated-read`, `secure-read` or `authorize`. A single-use transport permit allows only `org.bluez.GattCharacteristic1.ReadValue` with signature `a{sv}` and empty options. Direct reads in discovery mode, arbitrary UUIDs, offsets, writes and notification methods remain outside the allowlist.

The integration does not call `Pair`, register an authentication agent or change trust. Metadata is not proof of every firmware or stack behaviour: final pairing/trust state is checked again, and any change makes the result `cleanup_requires_review`. The integration does not try to undo such a change.

The five-second read-stage deadline includes fresh metadata validation. There is no retry, offset/chunk loop, authentication command, pairing fallback or subscription. BlueZ implements the underlying ATT exchange; one D-Bus read does not mean exactly one radio packet. A read may fail with `NotAuthorized`, `NotPermitted` or `NotSupported`, and the probe will disconnect rather than escalate ([BlueZ GATT API](https://bluez.readthedocs.io/en/latest/gatt-api/)).

The configured stage budgets total at most 65 seconds after opening the system bus, plus up to 5 seconds for that bus connection and scheduling overhead. Timeouts bound coroutine waits, not a guarantee against host stalls or a failed Bluetooth stack. Disconnect and final state verification remain bounded even when the read fails.

An accepted response reports `read_complete`, `sample_bytes`, `sample_hex` and `sample_interpretation: unparsed` (or `empty` for zero bytes). More than 512 bytes or a malformed byte array is rejected, not truncated or retried. `read_attempted` means the guarded read stage was entered; endpoint validation may refuse it before any `ReadValue` is sent. Check `cleanup: disconnected_confirmed` before doing anything further.

No bytes are interpreted as temperature, battery level or robot status. A readable flag and a successful raw read do not establish that the characteristic returns telemetry. An empty or opaque response is evidence to review, not permission to send status-request commands. Raw hex is retained only in volatile integration state and the optional action response, never in the integration's sensor attributes or diagnostic download; a subsequent action replaces that volatile result.

## Interpret the result

The community reverse-engineered specification proposes this standard endpoint for Surfer S1; the integration reports whether it was actually found, rather than assuming support ([pool-robot BLE specification](https://github.com/knobunc/aiper/blob/main/docs/AIPER_POOL_ROBOT_BLE_SPEC.md)):

| Candidate | UUID |
|---|---|
| Service | `4a5ad444-2537-11ee-be56-0242ac120002` |
| Characteristic | `4a5a54e6-2537-11ee-be56-0242ac120002` |

Check that the characteristic belongs to that service and inspect its actual flags. `read_once` specifically requires `read`; `write`, `write-without-response` and `notify` are not used. These are metadata capabilities, not authorisation to send commands ([BlueZ GATT API](https://bluez.readthedocs.io/en/latest/gatt-api/)).

- **`discovery_complete` + `disconnected_confirmed`:** Inventory obtained and connection released. Missing expected UUIDs still require review.
- **`read_complete` + `disconnected_confirmed`:** One bounded raw read returned and the connection was released. This is not decoded telemetry.
- **`failed` or `no_gatt_services_returned`:** Inconclusive. Do not infer that BLE is unsupported.
- **`interrupted`:** Probe was cancelled; check the cleanup field.
- **`cleanup_requires_review`:** Do not repeat automatically. Confirm connection ownership/state before further activity.

Service cache reuse is possible; the probe intentionally does not force invalidation. Discovery/read success does not establish encryption, application-level authentication, valid status packets, temperature units/scaling or control support. The protocol action below is a separate experiment requiring review and approval.

## Experimental protocol harness

The initial implementation used the community [pool-robot BLE specification at commit 20b5200](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md), attributed to Android app 3.3.2. The 0.4.0 corrections use independent static inspection of app 3.6.1 build 82, including raw instructions for its BLE serializer call. See [APK evidence and limitations](aiper_ble_apk_361_evidence.md). Mock fixtures and static analysis do not prove compatibility with robot firmware.

| Spec area | Harness coverage |
|---|---|
| Standard/S1 UUID mapping | Pinned Surfer S1 name, local adapter and exact service/characteristic parents |
| Legacy XOR transport | Four-byte XOR, Base64, newline framing and fragmented/coalesced response decoding |
| Legacy CRC16-Modbus | Corrected initial value `0x9966`; `{}` gives decimal 6921; separate SDK 0x1021 algorithm is not implemented |
| Status request | One fixed OpInfo request, optionally with empty-data checksum, OR Machine `AT+S1_INFO?` (CRC 49921), `AT+INFO?` (CRC 10442), or `AT+WARN?` (CRC 10501) |
| Notifications | Explicit StartNotify, exact BlueZ sender/path/interface filtering, bounded queue, StopNotify cleanup |
| Chunking | At most 200 bytes and at most reported MTU minus three; 20-byte fallback |
| State evidence | OpInfo candidates remain unscaled; S1_INFO supplies temperature/solar; INFO supplies raw status/mode and bounded app-derived battery percentage; WARN supplies a raw warning code |
| New ECDH/AES protocol | Manufacturer hint or target key-exchange characteristic vetoes the query; no handshake implemented |
| Other models, DevInfo subscriptions, controls, provisioning, firmware | Not implemented or exposed |

### Known ambiguities are not silently resolved

- **Empty-data CRC:** For OpInfo prefer `omit_empty_crc`: the app's legacy factory serializes null query data as `{}` without a checksum. `include_empty_crc` is retained as an explicitly selected experimental form representing a non-null empty object, now with CRC seed `0x9966`. This setting only affects OpInfo. S1_INFO, INFO and WARN always include their mandatory nonempty-data CRC, whichever empty-data policy is supplied. No retry or second variant is sent.
- **Write transport:** `command` requires `write-without-response`; `request` requires `write`. App 3.6.1 supports a write-with-response fallback, but this harness keeps the transport explicitly selected and never falls back.
- **Protocol evidence:** Company ID zero first byte `0x01` vetoes the query. Other valid nonempty first-byte values indicate legacy. Missing/empty data remains `unknown`; only `confirm_legacy_probe: true` allows the connection experiment. Malformed evidence is a separate unconditional veto. Cached hints are checked twice before connecting and again before notifications and writes. The target's key-exchange characteristic `4a5a64e6-2537-11ee-be56-0242ac120002` vetoes a query even if legacy advertisement data is present or the opt-in is true. Cached GATT can veto, not positively authorise, a connection. A connected, ServicesResolved device with one exact legacy endpoint and correct flags/parents is required before any notification subscription. Resolution can reuse BlueZ's cache; it is not fresh over-the-air proof.
- **Temperature:** App 3.6.1 maps shadow `Machine.temp` and the separate `S1_INFO` first response field to Celsius by dividing by ten. Its S1 `OpInfo` panel handler reads Wi-Fi fields. Only the fixed S1_INFO parser applies that scale; OpInfo candidates remain unscaled. Manual query actions do not update the polling sensors. The physical sensor location remains unverified, so this is not labelled outdoor or water temperature.
- **Response validity:** For OpInfo, `query_complete` means a standard JSON envelope with case-insensitive type `OpInfo` arrived. S1_INFO additionally requires exact type `Machine` and a matching `+S1_INFO:` report/ack with the strict numeric shape below. Neither proves command acceptance, device operation, response checksum validity or causality. There is no request ID; an unsolicited matching response is possible. Raw response fields remain available privately for inspection.

### Preview without Bluetooth activity

After a separately approved merge/deployment, invoke:

```yaml
action: aiper_ble_diagnostics.protocol_preview
data:
  entry_id: "<diagnostic integration entry ID>"
  query_type: OpInfo
  checksum_mode: omit_empty_crc
  write_mode: request
response_variable: aiper_protocol_preview
```

The example follows the statically inspected legacy null-data serializer, subsequently exercised by a successful query on one Surfer S1. That result is not a compatibility guarantee for other firmware or models. The preview returns exact request JSON, encoded hex, byte count, APK hash and original static-analysis assumptions. It opens no system bus, reads no Bluetooth metadata and does not connect. For MCP use `return_response: true`.

For the fixed S1 query, change only `query_type` to `S1_INFO`. The preview then shows `{"type":"Machine","data":{"cmd":"AT+S1_INFO?"},"chksum":49921}` before encoding and `checksum_semantics: nonempty_data_crc_required`. Omitting `query_type` preserves the earlier OpInfo default.

For INFO, select `query_type: INFO`. The preview shows
`{"type":"Machine","data":{"cmd":"AT+INFO?"},"chksum":10442}`. The separate
[INFO validation guide](info_dp_validation.md) defines its strict response
shape, evidence limits and deployment acceptance checks.

For WARN, select `query_type: WARN`. The preview shows
`{"type":"Machine","data":{"cmd":"AT+WARN?"},"chksum":10501}`. The
[additional-query assessment](apk_query_catalog.md) defines its strict signed
int64 parser and explains why other APK candidates remain excluded.

### S1_INFO diagnostic interpretation

The Android no-arguments query path sends `AT+S1_INFO?` in `data.cmd`. The harness exposes no AT name, arguments, assignment form, serial or arbitrary payload. It does not reproduce the app's subscriptions or retry loop.

Only `Machine` responses whose `data.report` (preferred) or `data.ack` matches `+S1_INFO:<decimal>,<integer>\r\n` are accepted. Exactly two fields, bounded decimal notation and an int32 second field are required; malformed matching replies fail with `invalid_s1_info_response`. Unrelated replies are ignored within the same 15-second exchange. CRLF was initially inferred from the app stripping two trailing characters and subsequently observed in a live S1 reply. If firmware uses another shape, stop and review rather than widening the parser automatically.

Accepted numeric candidates are `temperature_raw`, `temperature_celsius` (first field divided by ten) and `solar_status` (second field, uninterpreted integer). Synthetic `+S1_INFO:265,1\r\n` gives 26.5 Celsius and solar state 1; this is a test fixture, not a reading from the user's robot. `temperature_interpretation` explicitly records the app-derived scale and unverified sensor location. Manual actions retain these as private diagnostic candidates; only the additional CRC-verified polling path promotes them to HA sensors. Neither proves outdoor/water temperature.

### One separately authorised protocol query

Review the preview and cached protocol hint first. Keep the robot stationary and every other BLE client idle. The following is deliberately non-executable until the three confirmations are changed following approval:

```yaml
action: aiper_ble_diagnostics.query_once
data:
  entry_id: "<diagnostic integration entry ID>"
  query_type: S1_INFO
  checksum_mode: omit_empty_crc
  write_mode: request
  confirm_app_closed: false
  confirm_query_write: false
  confirm_notifications: false
  confirm_legacy_probe: false
response_variable: aiper_private_query_result
```

This example selects only S1_INFO. Select `OpInfo`, `INFO` or `WARN` for the other fixed
requests; a separate invocation and approval are needed for another manual query.

If company-zero advertisement evidence is missing, a separate explicit approval is needed to set `confirm_legacy_probe: true` for this call. This permits only the guarded legacy experiment; it never overrides ECDH, malformed data, identity or endpoint checks and is not persisted in entry data or options. Leave it false when evidence is present.

The test connects once, validates the endpoint, enables notifications, revalidates the endpoint and protocol hint, sends one fixed status request, then waits for a matching response. A request can span multiple ATT writes; each precomputed chunk has a single-use transport permit consumed before the D-Bus call. No arbitrary command, payload, characteristic, retry, write offset, pairing or trust-changing service parameter exists.

Notification subscription itself can write the CCCD. `StopNotify` is attempted even if the StartNotify call times out, except when BlueZ explicitly reports another operation/session may own it. Uncertain ownership or failed notification/signal cleanup reports `cleanup_requires_review`, even if the final disconnect is confirmed. See the [BlueZ GATT API](https://bluez.readthedocs.io/en/latest/gatt-api/) for notification sessions and write types.

The exchange has a 15-second total deadline including validation, signal setup, notification enablement, all request chunks and response waiting. Each chunk write also has a five-second deadline. Receive limits are 4,096 bytes per encoded frame, 8,192 bytes total, 64 processed notifications and a 32-item queue. Malformed Base64/JSON, duplicate JSON keys, nonfinite JSON constants, incompatible envelopes, oversize frames and queue overflow fail closed. Cached characteristic `Value` is never decoded. Pre-request notifications are discarded, including any partial frame.

Notification stop and signal-match removal each have five-second cleanup budgets. Existing disconnect and final-state checks still run after failure or cancellation. Full configured waits total at most 90 seconds including opening the bus, plus scheduling overhead; host/bus failure can prevent confirmed cleanup. No operation retries automatically.

### Review the evidence

- `protocol_preview`: offline only; no robot evidence.
- `query_complete` with `notification_cleanup: stop_confirmed` and `cleanup: disconnected_confirmed`: bounded transport experiment completed; inspect the private response and its caveats.
- `protocol_unknown`: missing evidence without the explicit per-call opt-in; no connection.
- `ecdh_unsupported`, `ecdh_gatt_unsupported`, `protocol_malformed` or `unsupported_model`: vetoed before connection if known then, or before application writes if discovered later; the opt-in cannot override these.
- `protocol_evidence: explicit_probe_resolved_legacy_gatt_no_advertisement`: the opt-in and resolved GATT gate were used. This is an experiment, not a verified protocol classification.
- `unsupported_write_or_notify_flags`: discovered endpoint cannot support the chosen transport; no notification subscription or application write is sent.
- `timeout`: use `failure_stage` to distinguish connection, resolution, subscription, write and response waits. There is no retry.
- `cleanup_requires_review`: stop and inspect ownership/connection state before another action.

The private `protocol_response` is retained only in volatile integration results and the optional authenticated action response. Diagnostic downloads redact that entire object. Only allowlisted scalar `telemetry_candidates` survive in diagnostic downloads; they are never recorder sensor attributes. The diagnostic sensor receives fixed failure/cleanup codes, never packet contents. HA action traces and MCP history may retain responses, so do not post them publicly without review.

## Development and tests

Use Python 3.14 and the isolated pinned test requirements:

```sh
python -m venv .venv-aiper
.venv-aiper/bin/pip install -r requirements-aiper-ble-test.txt
.venv-aiper/bin/ruff check --config ruff-aiper.toml custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
.venv-aiper/bin/ruff format --config ruff-aiper.toml --check custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
.venv-aiper/bin/python -m pytest -q tests/aiper_ble_diagnostics --disable-socket --allow-unix-socket --asyncio-mode=auto
```

The suite uses the real HA config-flow/service/entity framework with fake BlueZ objects, no real Bluetooth adapter, and network sockets disabled. Unix sockets are allowed for the asyncio event loop; BlueZ itself is mocked. Tests cover setup safety, manual selection, duplicate entries, confirmations, discovery, exact read/write message shape, single-use permissions, endpoint ambiguity, response size/type limits, codec fragmentation, signal filtering, privacy, timeouts, concurrency, unload cancellation and cleanup. The dedicated GitHub Actions workflow runs the same checks.
