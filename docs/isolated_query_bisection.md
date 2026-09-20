# Isolated HA Bluetooth query bisection

The four independent actions below use the same selected transport and
CRC/scalar verification as recurring polling. From v0.9.2, the
`use_local_adapter` option selects pinned direct BlueZ instead of HA/Bleak
routing. Local mode never falls back to an active proxy. These actions are not
aliases for the legacy `query_once` action: coordinator verification and cooldown
still apply.

| Action in `aiper_ble_diagnostics` | Fixed request | Useful returned values |
| --- | --- | --- |
| `query_s1_info` | S1_INFO | Temperature and raw solar status |
| `query_opinfo` | OpInfo | Only present allowlisted fields, including raw Wi-Fi RSSI |
| `query_info` | INFO | Valid 0-100 battery, raw operating status and mode |
| `query_warn` | WARN | Raw warning integer, not a decoded fault description |

## Safe procedure

Disable recurring polling in integration options before starting. Leave other
options unchanged, close the app and other BLE clients, and keep the robot
stationary. Disabling polling does not authorise a query: each action requires
three explicit confirmations. Keep the same proxy and robot position throughout.

```yaml
action: aiper_ble_diagnostics.query_opinfo
data:
  entry_id: YOUR_ENTRY_ID
  confirm_app_closed: true
  confirm_query_write: true
  confirm_notifications: true
```

Select response data when calling from a client; Developer Tools and downloaded
diagnostics also expose the last isolated result. A successful result has
`status: query_complete`, `phase: verify_response`, confirmed notification and
disconnect cleanup, and `values` containing only verified, non-null allowlisted
scalars. It does not refresh telemetry entities or the last-successful-cycle
timestamp. Unknown/sentinel values are not promoted to trustworthy measurements.

Each call makes one connection and one fixed request, with no application retry.
The execution deadline is 60 seconds, with bounded transport cleanup that can
take up to another 15 seconds on cancellation. All four actions share the
configured 300-3600 second cooldown, measured after cleanup. Switching actions
does not bypass it. An active probe, unloading, or suspended cleanup blocks calls.
The persistent missing-advertisement exception remains opt-in; security and
identity checks remain in force. No arbitrary command, pairing or robot control
is exposed.

Start with OpInfo, which previously failed at connection establishment immediately
after S1_INFO. Then test INFO, WARN and S1_INFO separately, waiting the configured
interval after every attempt. Stop on unsafe cleanup, protocol mismatch or an
unexpected response rather than repeatedly replaying it. A connection failure
with zero writes cannot establish whether a command is supported.

If each query works alone but the combined cycle fails, investigate reconnect
timing or connection lifetime next. This PR deliberately does not switch to a
persistent connection or alter production polling behaviour. Such an A/B change
needs its own review and bounded live test. Do not use reloads/restarts to reset
the in-memory cooldown or suspension.

## APK concurrency and timing evidence

Static inspection of the previously acquired Aiper 3.6.1 APK found:

* `BleManager.TaskQueue` tracks waiting and executing tasks. Its
  `executeNextTask` coroutine awaits completion for non-IrriSense devices before
  advancing; the IrriSense branch instead starts the task without awaiting it.
  This supports serial command processing, not concurrent S1 requests.
* `BleDeviceManager.establishConnection` uses a mutex and has a ready-state
  branch that checks the existing SDK transport connection. Connection lifetime
  is managed separately from the command queue; reconnecting for every query is
  not a requirement established by this code.
* The SDK includes a shared `BleGattConnectGate` mutex. `BleGattInitPolicy`
  defines a 600 ms MTU quiet period, 3,000 ms discovery watchdog, 5,000 ms missing
  MTU fallback and 2,500 ms CCCD-write retry. These are Android GATT
  initialization policies, not verified S1 inter-query cooldowns and not
  instructions to duplicate Android behaviour in an ESPHome proxy.
* The S1 panel's 50 ms then 3,000 ms sequence is in `startOrStopClean` and
  controls UI debouncing. It is not evidence of a three-second BLE reconnect
  minimum.

No general five-minute device-enforced query cooldown was identified in the
inspected paths. The integration's five-minute minimum is a deliberate testing
safety policy. Static app code does not prove the robot's firmware requirements,
the actual transport branch used at runtime, or that reconnect timing caused the
observed connection failure. No APK or decompiled source is redistributed here.

For acquisition provenance and limitations, see
[APK evidence](aiper_ble_apk_361_evidence.md). The production four-query design is
documented in [the query catalog](apk_query_catalog.md).
