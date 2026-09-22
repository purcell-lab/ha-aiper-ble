# Surfer S1 state enumeration and guarded cleaning controls

Prepared 22 September 2026 (AEST) for version 0.10.0. These are independently
implemented, APK-derived controls, not yet validated by moving a physical robot.
No live BLE action was performed while preparing this change.

## Evidence and provenance

The retained Android `com.aiper.link` 3.6.1 build 82 base APK was re-hashed:
`b2e81eb9db09873dee7f0f3887008f810383d66b4df354894e78da565021d2a1`.
See [APK provenance](aiper_ble_apk_361_evidence.md), including the
[APKCombo acquisition page](https://apkcombo.com/aiper/com.aiper.link/download/apk),
signature-validation limits and incomplete-decompile caveat. No APK, copied
decompiled classes or private robot identifiers are included in this repository.

The targeted evidence paths are:

- `com.aiper.device.surfer.model.StatusType`: eight displayed state names.
- `com.aiper.device.surfer.model.S1StatusInfo.getStatusType`: state predicates.
- `com.aiper.device.surfer.ui.activity.S1PanelActivity.startOrStopClean`:
  operation state zero selects integer 1; otherwise integer 0; calls
  `CmdManager.sendSetAT("MODE", [value], ...)`. The UI sets standby on stop.
- `S1PanelActivity.onClick`: blocks update, disconnected and charger states,
  plus warnings whose decoded `interceptorTask` is 1.
- `BleManager$BleDeviceManager$sendSetATInternal$1.invokeSuspend`: constructs
  `Machine` with `cmd: "AT+" + name + "=" + comma-joined arguments` and optional
  serial; invokes `sendForResult` with `isQueryAt=false`.
- `BleManager.BleDeviceManager.receiveResponse`: retained JADX fallback
  instruction dump establishes case-insensitive `+ok` prefix acceptance for
  setters, and `+error` rejection. The normal Java method failed to decompile.
- `CmdFactory.create`: the established legacy serializer omits null serial,
  adds the nonempty-data CRC and applies XOR/Base64/LF framing.

The earlier [knobunc/aiper specification](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md)
remains a development reference, not the evidence for these S1-specific
commands. Existing [acknowledgements](../ACKNOWLEDGEMENTS.md) remain unchanged.

## App states are not wire enum ordinals

`StatusType` declaration order is DISCONNECT, UPDATING, WARNING, STANDBY,
CHARGING, FULL_CHARGED, SUNWARD, WORKING. Its ordinal 0-7 values must not be
applied to INFO field 0.

The actual `getStatusType` decision order is:

| Priority | App condition | Displayed enum |
| --- | --- | --- |
| First | Neither MQTT online nor BLE connected | DISCONNECT |
| Next | Decoded `warnInfo` is present | WARNING |
| Next | INFO status 4, or external OTA state 1 or 2 | UPDATING |
| Next | INFO status 2 and battery not 100 | CHARGING |
| Next | INFO status 3, or status 2 and battery 100 | FULL_CHARGED |
| Next | INFO mode 9 | SUNWARD |
| Next | INFO status 1 and mode not 8 | WORKING |
| Otherwise | App fallback, including status 0 or mode 8 | STANDBY |

SUNWARD uses the app's intermittent-mode text resource. This is not evidence
that the robot is navigating toward sunlight. Mode 8 suppresses WORKING, but
its physical meaning is not established; other mode names are not guessed.
Status 0 is consistent with the app's standby branch and start/stop UI.
Live polling on 22 September 2026 (AEST) read status 0 with mode 0 while idle
in the pool and while paused, status 1 with mode 1 while cleaning, status 2
with mode 0 on the wall charger below 100%, and status 3 with mode 0 at 100%.

### Home Assistant representation

`sensor.aiper_ble_operating_state` exposes the INFO-derived subset:
`standby`, `working`, `charging`, `fully_charged`, `updating`, `sunward`,
`unknown_code`. It is updated only from a successful existing polling cycle.
Unknown status codes remain `unknown_code`, rather than inheriting the app's
potentially misleading standby fallback. Existing raw status/mode sensors stay.

The sensor deliberately does not fabricate app cloud-connectivity, decoded
warning or external OTA overlays. Normal disconnect after a bounded BLE query
does not mean the robot is offline. A failed poll makes the sensor unavailable.
Charging means plugged into the wall charger; this robot has no dock.

## Fixed command frames

The start request before XOR/Base64/LF encoding is:

```json
{"type":"Machine","data":{"cmd":"AT+MODE=1"},"chksum":11049}
```

The stop request is:

```json
{"type":"Machine","data":{"cmd":"AT+MODE=0"},"chksum":60280}
```

Stop means return to standby, not power-off, docking, or a proven pause/resume
operation. These are explicit setters, never a state-dependent toggle.
CRC is the existing legacy Modbus variant with seed `0x9966`.

The integration conservatively accepts only `Machine` with a bare `+OK\r\n`
(case-insensitive) selected using the app's report-before-ack rule, integer
`res: 0`, and a correct full-data CRC. This is stricter than the app's prefix
test. The exact bare acknowledgement and CRC envelope still need live
validation for setters. A generic `+OK` has no transaction identifier, so it
cannot prove either causality or motion. It is never published as operating state.

## Actions and guardrails

Two actions are registered under `aiper_ble_diagnostics`:
`start_cleaning` and `stop_cleaning`. Both require:

```yaml
entry_id: YOUR_LOADED_S1_ENTRY
confirm_app_closed: true
confirm_safe_to_move: true
```

The safety confirmation means the robot is in water, unplugged, no people are
in the pool, no firmware update is running, and this action is authorised.
Other BLE clients must be idle. Both fields default to false in the action UI.
There are no one-click buttons or autonomous cleaning schedules in this change.
Polling authorisation is not control authorisation.

- **Start:** fresh INFO then WARN, one MODE=1 setter, then one INFO readback.
  Start requires status 0/1, INFO-derived standby/working and warning code zero.
  Any nonzero or malformed warning blocks start because fault bit meanings
  are not implemented. This is stricter than the app's selective warning gate.
- **Stop:** one MODE=0 setter, then one INFO readback. It does not require a
  successful warning read or fresh cached status before attempting standby.
  It bypasses the isolated diagnostic actions' shared cooldown (`poll_now` has
  none since version 0.9.15), not an active BLE lock or suspended
  cleanup/protocol guard.
- **Transport:** uses the configured HA Bluetooth route (including active
  proxies) or explicitly selected saved local BlueZ adapter. No transport
  setting changes, fallback strategy or retry is introduced.
- **Bounds:** one shared task reservation across polling, probes and controls;
  each exchange retains the established transport deadlines and cleanup.
  The complete action has a 180-second deadline plus bounded cancellation
  cleanup. Automatic polling never sends MODE.
- **Outcome:** success requires a CRC-verified post-command INFO response.
  Start requires status 1 with derived WORKING; stop requires status 0 with
  derived STANDBY. One readback only; no state-convergence loop. Firmware latency
  can therefore produce an unconfirmed result even after an effective command:
  on 22 September 2026 (AEST) a poll at 09:13:48 still read status 0 after the
  robot had resumed cleaning, and the next cycle read 1. Treat an unconfirmed
  start as unresolved and check the next poll before repeating anything.
- **Uncertainty:** a write failure, malformed acknowledgement or failed readback
  never triggers retransmission. Diagnostics distinguish possible actuation,
  acknowledgement and verified readback. An unsuccessful action raises an HA
  error even when a caller requested a response.
- **State freshness:** any attempted control write invalidates the previous
  atomic telemetry cycle. Its INFO readback is returned in the action result,
  not merged with stale temperature or warning fields. The next ordinary poll,
  or separately authorised `poll_now`, restores the sensor snapshot.

`state_verified` means the robot reported the expected state, not that its
physical movement was observed. BLE stop is not a safety-rated emergency stop:
if communications fail, use the robot's physical controls.

## Validation boundary

Offline tests cover golden control frames, enum precedence, both real transport
implementations with simulated hardware, no arbitrary setters, checksum/result
rejection, confirmations, preflight blocks, readback mismatch, mutex, cancellation,
cleanup suspension and no retry after ambiguous actuation. Existing telemetry
and registry tests remain in the regression suite with network sockets disabled.

Live acceptance is still required, under fresh owner authorisation: deploy and
restart separately, then test one start and one stop with the robot physically
safe and the app closed. Record bounded acknowledgement shape and readback,
verify actual movement/standby, and inspect cleanup. Do not repeat an ambiguous
command automatically. Do not loosen acknowledgement parsing without evidence.
