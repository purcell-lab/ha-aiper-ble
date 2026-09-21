# Reverse-engineered Surfer S1 endpoints and commands

Updated 22 September 2026 (AEST), for integration version 0.10.0.
This is the navigation and implementation reference for the legacy Surfer S1
BLE interface. It is not an official Aiper API, an exhaustive APK endpoint
inventory, or evidence that other models accept these commands.

## Evidence and scope

The static source is Android `com.aiper.link` 3.6.1, build 82, with base APK
SHA-256 `b2e81eb9db09873dee7f0f3887008f810383d66b4df354894e78da565021d2a1`.
See [APK provenance and inspection limits](aiper_ble_apk_361_evidence.md) for
acquisition, signature checks and incomplete-decompilation caveats.

- **Observed:** endpoint discovery and successful status-query replies on one
  Surfer S1, documented in the [BLE specification](AIPER_POOL_ROBOT_BLE_SPEC.md).
- **APK-derived:** call sites and state predicates recovered by static inspection.
  These establish app intent, not universal firmware behaviour.
- **Implemented:** integration policy and offline-tested request/response
  handling. This does not itself establish live compatibility.
- **Unverified:** physical start/stop behaviour, setter acknowledgement format
  on the test robot, and unsupported command candidates.

In this document, an endpoint means a BLE GATT characteristic, while a command
is an application message carried over it. The HA actions are a third, local
software interface. No cloud REST URL or MQTT topic is needed by this
integration, and this document does not claim to inventory or validate those
separate app interfaces. Account APIs, provisioning, pairing and ECDH are out
of scope. No credentials, robot identifiers, APK binaries or copied decompiled
classes are included.

## BLE endpoint inventory

| Endpoint | UUID | Use and evidence |
| --- | --- | --- |
| Legacy service | `4a5ad444-2537-11ee-be56-0242ac120002` | Observed service on the test S1 |
| Legacy data characteristic | `4a5a54e6-2537-11ee-be56-0242ac120002` | Shared application-write and notification-reply endpoint; observed for telemetry |
| Key-exchange characteristic | `4a5a64e6-2537-11ee-be56-0242ac120002` | APK-derived newer-protocol indicator; its presence vetoes the legacy operation, rather than initiating key exchange |

These identifiers are traced through the app's `DeviceControllerFactory` and
`BleTransport`. The integration requires an unambiguous discovered legacy
service/characteristic and matching notification/write capabilities; it does
not blindly write a remembered characteristic handle. The configured full
device identity must match. Neither a UUID nor a device name authenticates
the robot cryptographically.

Company ID `0x0000` manufacturer data beginning with `0x01` indicates the
newer/ECDH path in the inspected app. Missing data is unknown, not proof of
legacy support. A separately enabled missing-advertisement option cannot
override positive incompatible-protocol evidence.

## Message and transport contract

The legacy app path serializes compact UTF-8 JSON, XORs bytes with repeating
hexadecimal key `12 34 56 78`, Base64-encodes the result and appends LF.
Replies arrive through notifications on the same data characteristic.
Notification boundaries need not coincide with application-frame boundaries.
The decoded AT reply string has its own CRLF terminator, distinct from the
outer Base64 frame's LF.

`chksum` is a decimal CRC-16 over the compact JSON **data object**, with initial
register `0x9966`, reflected polynomial `0xA001`, and no final XOR.
Received data-key order is preserved for verification. Null input to the
legacy serializer becomes `{}` without a checksum; explicit nonempty AT data
requires the checksum. The newer SDK's different serializer must not be mixed
with this path. XOR and CRC provide neither secure encryption nor sender
authentication.

The implementation writes control frames using write-with-response. A frame
may require several ATT chunks, so a diagnostic `write_attempts` value above
one is not, by itself, evidence of command retransmission. The current chunk
helper uses up to `min(200, MTU - 3)` bytes when MTU is known and at least 23;
otherwise it uses 20 bytes. This is implementation policy, not a universal
firmware limit. The current HA Bluetooth implementation does not pass an MTU
to that helper, so it uses 20-byte chunks; the direct-local path passes discovered
MTU metadata when available.

## Fixed application requests

Checksums below are reproducible codec vectors, not private captures. The
full wire encoding and receive gates are described in the
[BLE specification](AIPER_POOL_ROBOT_BLE_SPEC.md#legacy-frame-encoding).

| Purpose | JSON `type` | JSON `data` | `chksum` | Evidence |
| --- | --- | --- | ---: | --- |
| Temperature and solar code | `Machine` | `{"cmd":"AT+S1_INFO?"}` | 49921 | APK-derived; telemetry reply observed |
| Status, mode and battery | `Machine` | `{"cmd":"AT+INFO?"}` | 10442 | APK-derived; three-field app consumer and five-field live reply |
| Raw warning code | `Machine` | `{"cmd":"AT+WARN?"}` | 10501 | APK-derived; raw zero reply observed |
| Wi-Fi metadata | `OpInfo` | `{}` | Omitted | Legacy null-input path; RSSI-only reply observed |
| Start cleaning | `Machine` | `{"cmd":"AT+MODE=1"}` | 11049 | APK-derived; implemented; physical effect not live-validated |
| Stop to standby | `Machine` | `{"cmd":"AT+MODE=0"}` | 60280 | APK-derived; implemented; physical effect not live-validated |

The complete start envelope is
`{"type":"Machine","data":{"cmd":"AT+MODE=1"},"chksum":11049}`.
The complete stop envelope is
`{"type":"Machine","data":{"cmd":"AT+MODE=0"},"chksum":60280}`.
Neither includes a serial number or caller-supplied arguments. Stop means the
app's standby setter, not power-off, docking, or a proven pause/resume operation.

### Replies and field interpretation

For `Machine`, a string `data.report` takes precedence over `data.ack`.
Telemetry publication requires the expected reply type, integer `res: 0`,
a correct CRC over the complete received data object, valid field shape and
confirmed cleanup. A generic matching reply has no transaction identifier,
so checksum validity alone cannot establish request-response causality.

| Request | Accepted AT text or data | Published interpretation |
| --- | --- | --- |
| S1_INFO | `+S1_INFO:<temperature_raw>,<solar_code>\r\n` | Temperature divided by 10 in Celsius; solar code stays raw |
| INFO | `+INFO:<status>,<mode>,<battery>\r\n`, or the observed five-integer form | First three fields only; last two fields remain unknown |
| WARN | `+WARN:<signed-int64>\r\n` | Raw warning code; no inferred fault labels |
| OpInfo | Direct fields such as `wifi_rssi` and optional `wifi_name` | Observed RSSI `-127` remains raw; its sentinel meaning is unverified |
| MODE setter | Bare `+OK\r\n`, case-insensitive, within a valid Machine response | Acknowledgement only; a separate INFO readback is required |

The app setter parser accepts a case-insensitive `+ok` prefix and rejects
`+error`. The integration's bare-OK requirement is deliberately stricter and
has not yet been established by a live setter capture. Bad CRC, nonzero result,
unexpected reply shape or uncertain cleanup never justifies resending a command.
Full private replies must not be pasted into public issues: deleting identifying
fields changes the checksum and cannot produce a valid anonymised CRC example.

Temperature sensor location remains unknown. INFO battery is the app's
0-100 battery-level field, not an inferred OpInfo capacity measurement.
See the [DP validation matrix](info_dp_validation.md) for retained and excluded
fields.

## State enumeration

The app declares `DISCONNECT`, `UPDATING`, `WARNING`, `STANDBY`, `CHARGING`,
`FULL_CHARGED`, `SUNWARD`, `WORKING`. These enum ordinals are **not INFO wire
values**. The app combines connectivity, decoded warnings, OTA state and INFO
fields in a priority decision tree.

The integration exposes only the INFO-derived subset:

| INFO condition, evaluated in order | HA operating state |
| --- | --- |
| Status outside the known 0-4 range | `unknown_code` |
| Status 4 | `updating` |
| Status 2 and battery not 100 | `charging` |
| Status 3, or status 2 and battery 100 | `fully_charged` |
| Mode 9 | `sunward` |
| Status 1 and mode not 8 | `working` |
| Otherwise | `standby` |

The `sunward` label comes from the enum, but the app associates it with
intermittent-mode text; navigation toward sunlight is not established.
Mode 8 suppresses the working branch but has no proven physical interpretation.
The integration does not manufacture the app's connectivity, warning or
external-OTA overlays. Ordinary disconnection between bounded polls is not
reported as an app DISCONNECT state. The full app predicate order and exact
evidence paths are in [S1 states and controls](s1_states_and_controls.md).

## Home Assistant actions

Both controls use the configured transport and existing integration device.
There is no arbitrary AT-command service, automatic control retry, transport
fallback, one-click control button or cleaning schedule.

```yaml
# Run only after confirming the physical safety conditions below.
action: aiper_ble_diagnostics.start_cleaning
data:
  entry_id: YOUR_LOADED_S1_ENTRY
  confirm_app_closed: true
  confirm_safe_to_move: true
```

```yaml
action: aiper_ble_diagnostics.stop_cleaning
data:
  entry_id: YOUR_LOADED_S1_ENTRY
  confirm_app_closed: true
  confirm_safe_to_move: true
```

Each confirmation authorises that action only: the robot is in water,
unplugged, with nobody in the pool, no firmware update running, the app closed
and other BLE clients idle. Polling permission is not movement permission.
The robot has no dock; charging means manually connected to its wall charger.

Start executes fresh INFO, fresh WARN, MODE=1, then INFO. It requires known
standby/working status and warning zero before the setter. Stop executes
MODE=0 then INFO, without requiring a successful warning preflight.
Each request has its own bounded connection and cleanup. Both share the
poll/probe lock; neither can interrupt an active operation or bypass suspension.
The complete control has a 180-second deadline plus bounded cancellation cleanup.

Start is verified only by INFO status 1 with derived working; stop requires
status 0 with derived standby. There is one readback, not a convergence loop.
`state_verified` describes the robot's report, not visual observation of motion.
BLE stop is not an emergency stop; use physical controls if communications fail.

Any attempted control write invalidates the previous atomic sensor snapshot.
Its INFO readback is returned separately, never combined with older temperature
or warning readings. A successful subsequent scheduled cycle, or a separately
authorised `aiper_ble_diagnostics.poll_now`, refreshes sensors.
Cached diagnostics record `last_control`, including `acknowledged`,
`state_verified`, `motion_may_have_changed`, outcome and bounded exchange details.
Downloading diagnostics does not send BLE traffic.

## Deliberately excluded commands

`RECORD`, `ULTRAS`, `DevInfo`, `POWER_SAVE`, `AUTO`, `CYCLE_CLEAN` and
argument-bearing `TIMING` appear in targeted app paths, but their S1 BLE
compatibility, reply structure or side effects are not sufficiently established.
Only the two fixed MODE setters above are supported controls; this is not
permission to probe other MODE values. Factory reset, warning clearing,
provisioning, remote steering and firmware updates are not implemented.
See [additional APK query assessment](apk_query_catalog.md).

## Source map and validation boundary

| Finding | APK path or implementation reference |
| --- | --- |
| GATT endpoints and security path | `DeviceControllerFactory`, `BleTransport`, `BleManager.isNewProtocol` |
| Legacy serialization and CRC | `com.aiper.device.api.CmdFactory` |
| S1 telemetry consumers | `S1PanelActivity.loadDataForCmd` and its INFO/S1_INFO/WARN/OpInfo handlers |
| Start/stop selector and app gates | `S1PanelActivity.startOrStopClean`, `S1PanelActivity.onClick` |
| Setter construction | `BleManager$BleDeviceManager$sendSetATInternal$1.invokeSuspend` |
| Setter acknowledgement | `BleManager.BleDeviceManager.receiveResponse`, targeted fallback instruction dump |
| App state enum and predicates | `StatusType`, `S1StatusInfo.getStatusType` |
| Fixed codec and response validation | [`protocol.py`](../custom_components/aiper_ble_diagnostics/protocol.py) |
| Bounded control orchestration | [`controls.py`](../custom_components/aiper_ble_diagnostics/controls.py) |
| INFO-derived HA enum | [`s1_states.py`](../custom_components/aiper_ble_diagnostics/s1_states.py) |
| Offline control regression tests | [`test_controls.py`](../tests/aiper_ble_diagnostics/test_controls.py) |

PR #23 passed 868 offline tests, including fake-hardware execution through both
transport implementations, and its CI checks. Those results do not prove a
physical control worked; service registration and deployment do not prove that
either. Live setter acknowledgement, post-command state, actual movement and
cleanup still require a separately authorised acceptance test.
See [PR #23](https://github.com/purcell-lab/ha-aiper-ble/pull/23) and retain
that distinction in future documentation updates.

The work builds on the repositories credited in
[acknowledgements](../ACKNOWLEDGEMENTS.md), including the
[knobunc/aiper protocol reference](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md)
and [kmich/ha-aiper](https://github.com/kmich/ha-aiper).
