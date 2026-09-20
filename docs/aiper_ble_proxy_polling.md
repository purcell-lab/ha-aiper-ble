# Aiper BLE: HA-managed polling and battery evidence

## Version 0.8.0: device and operator polling

All 23 existing sensor entities are grouped under one `Aiper Surfer S1 (BLE)`
device with manufacturer `Aiper` and model `Surfer S1`. Its stable identifier
is scoped to this integration and the configured Bluetooth address. Entity
unique IDs and names are unchanged, including across reloads. We do not infer
an association with the separate cloud integration or create a speculative
shared connection identifier. This uses the standard
[HA device registry](https://developers.home-assistant.io/docs/device_registry_index/).

After switching the robot on, use Developer Tools > Actions:

```yaml
action: aiper_ble_diagnostics.poll_now
data:
  entry_id: YOUR_AIPER_BLE_CONFIG_ENTRY_ID
  confirm_app_closed: true
```

Keep the app closed and other BLE clients idle. The action runs exactly one
existing S1_INFO plus OpInfo polling cycle using HA Bluetooth, including proxy-only
entries, and updates entity states only after successful CRC and cleanup checks.
It does not enable polling, change options, pair, control the robot, issue INFO,
subscribe to DevInfo or send arbitrary commands.

Polling and exclusive-access authorisation must already be enabled. Disabled,
unloading, busy or suspended entries are rejected. The action can shorten an
offline failure backoff, but cannot bypass the configured normal interval
(at least 300 seconds), measured from completion of the previous attempt.
An early request raises a cooldown error with seconds remaining rather than
silently returning cached data. Failed manual attempts retain automatic
backoff; uncertain cleanup still suspends polling. A successful call returns
only `status` and `last_successful_poll` when a response is requested.

## Version 0.8.1 failure reporting

Repeated failed manual polls now refresh the polling-status entity, even when
the previous attempt already failed. Telemetry remains unavailable after failure;
no partial or stale readings are published as fresh.

For callers requesting a response, an operational polling failure returns
`status` (`failed` or `suspended`), `error_code`, `consecutive_failures`, and
`details`, instead of leaking an `UpdateFailed` exception as a generic HTTP 500.
The successful response is unchanged. REST clients must use
`POST /api/services/aiper_ble_diagnostics/poll_now?return_response` and inspect
`service_response.status`: HTTP 200 means the action returned a result, not that
the robot answered successfully. Automations requesting a response must likewise
check `status == "ok"` before acting on telemetry.

Calls without a requested response raise an actionable service validation error
on operational failure. HA's REST endpoint does not guarantee structured error
responses in that mode; REST/MCP callers should request the response. Prerequisite
and cooldown violations still raise validation errors, and cancellation still
propagates. No new query, retry, scan, or guard bypass is introduced.

Downloaded diagnostics include `polling.last_poll_details`, a cached summary of
the latest attempted query only. Downloading it causes no Bluetooth activity.
It contains the query type, transport, phase/failure stage, fixed error category,
write-attempt and notification/byte counts, and cleanup results when available.
The second query replaces the first summary if reached; a failure in the first
query still prevents the second query.

Stages distinguish route validation, connection, connected-route validation,
endpoint validation, notification subscription, pre-write validation, writing,
response waiting/decoding and CRC/value verification. Cleanup errors separately
identify notification-stop or disconnect problems while retaining any earlier
failure stage. Error categories are fixed labels (`timeout`, `bleak`, `connection`,
`os`, `unexpected`, `protocol`, or `cancelled`), not exception messages. A category
and stage narrow the investigation but do not establish the root cause.

The summary excludes raw replies, exception text and class names, MAC addresses,
robot names/serials, network names, proxy identifiers, and backend details.
This release improves observability; it does not claim to fix an underlying
Bluetooth connection failure.

## Version 0.7.0 scope

Recurring telemetry now uses Home Assistant's Bluetooth integration and
`bleak-retry-connector`, not the private local D-Bus query transport. HA selects
an available connectable route, including an active ESPHome Bluetooth proxy.
There is no private scanner, forced adapter, pairing request or robot control.
This follows the [HA Bluetooth integration guidance](https://developers.home-assistant.io/docs/bluetooth/).

Existing entries retain their unique IDs, entity IDs, options and legacy adapter
metadata. No entry migration is required. New entries can be selected from HA's
shared discovery cache without a local adapter. Passive-only receivers cannot
carry GATT queries. Missing manufacturer-data consent does not bypass the
requirement for a connectable HA route.

The `preflight`, `discover`, `read_once`, `query_once` and `listen_once` legacy
diagnostic services still use the pinned local BlueZ transport for old entries.
They reject proxy-only entries before accessing BlueZ. `protocol_preview` works
offline for either entry type. This release migrates recurring sensor polling,
not the older diagnostic experiments.

## Bounded transport and safety differences

- Fresh HA-wrapped client per fixed request, selected by the shared framework.
- One physical connect attempt, bounded at 30 seconds. The connector's transient
  retry budget is explicitly blocked by a single-attempt client subclass;
  `max_attempts=1` alone would not provide that guarantee.
- Only existing `S1_INFO` and `OpInfo` requests, with conservative 20-byte chunks.
  No new `INFO` query or persistent subscription is enabled in this release.
- Exact configured address/name on all selectable routes; ECDH or malformed
  manufacturer evidence on any route vetoes the operation. Revalidate after
  connect and after notification subscription, before writing.
- Exactly one legacy service and characteristic; key-exchange characteristics,
  security flags, and missing write/notify capabilities veto writes.
- A 15-second exchange deadline, bounded notification queue and decoder,
  fixed-frame requests, no application retry, and no startup-notification reuse.
- Client references are retained before connect returns, allowing bounded
  cleanup on connection failure, deadline expiry and unload cancellation.
- Stop-notify deadline 5 seconds; disconnect deadline 10 seconds. Unconfirmed
  cleanup suspends polling. The existing 300-second minimum interval, backoff,
  mutual exclusion and CRC-checked atomic sensor publication remain.

Remote Bleak backends do **not** expose all local BlueZ metadata. The old
pre-connect pairing/trust/notification ownership assertions cannot be claimed
for proxies. Negative local properties are honoured when available; otherwise
the integration relies on the explicitly confirmed exclusive-access condition.
Do not run the app or another BLE client while polling. A shared framework
connection is not proof that an external client is idle. CRC is corruption
detection, not device authentication.

HA may choose a different adapter/proxy between queries or cycles. This is
intentional; backend route choice belongs to HA. Local direct-D-Bus fallback is
not attempted when a managed polling route is unavailable.

## Entity coverage

All fields below have a dedicated sensor. Values are published only after both
requests complete successfully, their CRCs pass and connection cleanup is
confirmed. Missing or invalid fields become unavailable, not zero and not a
carried-forward reading. Unknown enum codes are left raw.

| Response path | HA sensor label / interpretation |
|---|---|
| `S1_INFO` value 1 | Aiper BLE temperature (value / 10, °C) and Aiper BLE S1 temperature raw |
| `S1_INFO` value 2 | Aiper BLE solar status raw |
| `S1_INFO` envelope `data.timeZone` | Aiper BLE S1 time zone, bounded diagnostic text |
| `OpInfo.data.wifi_rssi` | Aiper BLE Wi-Fi RSSI raw; no dBm or sentinel claim |
| `OpInfo.data.wifi_name` | Aiper BLE Wi-Fi network; local HA state, not exported in integration diagnostics |
| `OpInfo.data.bat` | Aiper BLE OpInfo battery raw; no percentage claim |
| `OpInfo.data.status` | Aiper BLE OpInfo status raw |
| `OpInfo.data.link` | Aiper BLE OpInfo link raw |
| Optional `OpInfo.data.Machine.cap` | Aiper BLE OpInfo Machine battery capacity raw |
| Optional `OpInfo.data.Machine.mode` | Aiper BLE OpInfo Machine mode raw |
| Optional `OpInfo.data.Machine.solar_status` | Aiper BLE OpInfo Machine solar status raw |
| Optional `OpInfo.data.Machine.status` | Aiper BLE OpInfo Machine status raw |
| Optional `OpInfo.data.Machine.temp` | Aiper BLE OpInfo Machine temperature raw; separate from S1_INFO |
| Optional `OpInfo.data.Machine.warn` | Aiper BLE OpInfo Machine warning raw |
| Optional `OpInfo.data.Machine.warn_code` | Aiper BLE OpInfo Machine warning code raw |
| Optional `OpInfo.data.Machine.in_water` | Aiper BLE OpInfo Machine in water raw; not a boolean interpretation |
| Optional `OpInfo.data.Machine.link` | Aiper BLE OpInfo Machine link raw |
| Optional `OpInfo.data.Machine.light` | Aiper BLE OpInfo Machine light raw |
| Optional `OpInfo.data.Machine.visual` | Aiper BLE OpInfo Machine visual raw |

This produces 20 DP/metadata sensors plus the existing last-success, polling-status
and discovery-result sensors: 23 entities total, 17 added to the previous six.
The direct `OpInfo` fields are declared in app 3.6.1's `OpInfo.java`. The 11
`Machine` fields are declared in `Machine.java`; their nested presence in an
OpInfo response remains a candidate shape, not an observation from the user's S1.
They are exposed if returned, without issuing any extra query.

Serial numbers, credentials, unknown payload keys, complete `ack`/`report`
strings and protocol `type`/`res`/`chksum` fields are deliberately not entities.
The last three are envelope validation, not telemetry DPs. This is an explicit
decoded-field allowlist, not an unrestricted dump of device payloads into HA's
recorder. SSID strings are bounded to 32 UTF-8 bytes; integer fields reject
booleans, floats and numeric strings.

## Battery/SOC evidence

The [third-party protocol specification](https://github.com/knobunc/aiper/blob/main/docs/AIPER_POOL_ROBOT_BLE_SPEC.md)
identifies `Machine.cap` as battery capacity/percentage and proposes obtaining
shadow state using `DevInfo`. This is a protocol lead, not a verified S1 reply.

Independent static inspection of Aiper Android 3.6.1 build 82 provides two
S1-specific observations:

- `S1PanelActivity` shadow handling takes `Machine.getCap()` into `battLevel`,
  which is passed to `BatteryView.set(...)`.
- Its `loadDataForCmd()` subscribes to the `INFO` AT query, separately from
  `S1_INFO`. The `AnonymousClass6` handler parses three integers. Its update
  maps the first to status, the second to mode, and the third to battery level.
  This is a stronger S1 BLE lead than expecting battery in `OpInfo`.

Reproducible static locations in
`com/aiper/device/surfer/ui/activity/S1PanelActivity.java`:
lines 343-348 (battery widget), 487-493 (shadow capacity), 1010-1044
(`INFO` three-field mapping), and 1404-1405 (separate subscriptions).
APK SHA-256:
`b2e81eb9db09873dee7f0f3887008f810383d66b4df354894e78da565021d2a1`.
Line numbers refer to the session's JADX output; see
[APK inspection evidence](aiper_ble_apk_361_evidence.md) for acquisition context.

The app's `OpInfo.java` additionally declares an integer `bat` field, but its
meaning and scale on this S1 have not been verified. It is exposed only as raw.

The captured live `OpInfo` response contained `wifi_rssi` only; the captured
`S1_INFO` response contained temperature and solar-state fields. Neither proves
live SOC availability. Optional raw battery DP sensors are now included at the
user's request, but no verified SOC percentage or additional command is added.
A separately reviewed bounded `INFO` test should verify
the response prefix, field order, CRC, range and correspondence to the app
before a battery entity is enabled.

## Validation and deployment

Offline tests cover proxy-only routes and entries, full coordinator/entity
updates, stale/changed identities, negative evidence on alternate routes,
passive/unavailable routes, endpoint/security vetoes, queue limits, failures,
connection timeout, cancellation and cleanup. Existing legacy diagnostic tests
are retained separately from the HA transport tests.

This change requires merge/deployment approval and an HA restart to load the new
transport. A real proxy round trip is still required after deployment; mocked
tests establish API behaviour and safeguards, not radio reachability. Existing
enabled polling options will resume using HA-managed routes after the restart.
