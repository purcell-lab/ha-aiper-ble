# Additional Aiper Android query assessment

Reviewed against Aiper Android 3.6.1 build 82 on 20 September 2026.
This is a targeted S1-focused assessment, not an exhaustive list of every
command in the APK. Static call sites do not establish firmware compatibility
or prove that a query-looking command has no side effects.

See [APK provenance and hash](aiper_ble_apk_361_evidence.md). The implementation
is independently written; no APK, decompiled source, secrets or private robot
identifiers are included in this repository.

## Implemented fixed telemetry queries

| Query | App evidence | Decision |
| --- | --- | --- |
| `OpInfo` | S1 panel reads Wi-Fi RSSI/name | Existing query; only RSSI returned in the observed reply |
| `AT+S1_INFO?` | S1 panel reads temperature and solar state | Existing live-observed query; temperature scaled /10, solar remains raw |
| `AT+INFO?` | S1 panel maps fields 0/1/2 to status/mode/battLevel; live five-field reply `0,0,93,0,155` | Battery %, raw status/mode; final two fields unknown and not entities |
| `AT+WARN?` | S1 panel parses first field as decimal Java Long and assigns warnCode; live raw 0 | Raw warning-code entity; fault meanings remain unverified |

The S1 evidence is `com.aiper.device.surfer.ui.activity.S1PanelActivity`:
`loadDataForCmd` issues all four through `CmdManager`, and its WARN consumer
parses a signed decimal 64-bit value. The shared BLE no-argument AT-query
serializer constructs `Machine.data.cmd` with `AT+<fixed-name>?`.
The integration does not expose that serializer as a generic command interface.

WARN request before legacy XOR/base64 framing:

```json
{"type":"Machine","data":{"cmd":"AT+WARN?"},"chksum":10501}
```

Expected acknowledgement/report text:

```text
+WARN:<signed-decimal-int64>\r\n
```

Strict one-field/CRLF acceptance is integration policy inferred from the app's
first-field consumer and established AT framing. It is not a captured WARN
reply. Additional fields, malformed numbers or out-of-range values fail closed.
The polling path also requires `Machine`, integer `res: 0`, full-data CRC and
confirmed cleanup. Report takes precedence over ack. Negative values remain raw,
not assigned a fault meaning. Even zero is displayed as code `0`, not promoted
to an asserted "healthy" state.

All four queries use the HA Bluetooth/proxy framework in recurring polling,
with separate connections, a shared 180-second cycle deadline and no retries.
If WARN fails, the cycle does not publish partial data. The existing local-only
manual action can select one query; it does not update polling sensors.

## Interesting candidates deliberately not enabled

| Query | Located app path | Potential use and current limitation |
| --- | --- | --- |
| `RECORD` | S1 panel, `S1SettingActivity.sendCleanRecord`, and S1 `DeviceModeFragment` | Cleaning-record retrieval or synchronisation is plausible, but the inspected settings consumer only logs a result. Payload structure, transfer bounds and any upload/synchronisation side effects are not established. Not added to the allowlist. |
| `ULTRAS` | `S1FAQActivity.loadUltras` calls `MqttDeviceManager.sendQueryAT` | Potential ultrasonic/motion-calibration information. The located path is MQTT to a selected calibration device, not a verified S1 robot BLE query. Nearby `setUltras` is a setter and is excluded. |
| `DevInfo` | S2 panel direct command | Potential model/firmware metadata. No equivalent S1 BLE call established by this review; do not assume the S2 schema works on S1. |
| `POWER_SAVE` | S2 panel no-argument AT query | Potential power-saving configuration readback, but different model and unvalidated S1 support. |
| `MODE`, `AUTO`, `CYCLE_CLEAN` | Scuba panel/view-model query paths | Potential mode, automatic/scheduled-cleaning configuration. Model-specific semantics, response schemas and S1 compatibility are unverified. |
| `TIMING` | S2 task-memory path, with `R` and an index argument | Query helper accepts arguments here, so it is not equivalent to our fixed no-argument `AT+NAME?` form. Excluded rather than introducing assignments or arbitrary arguments. |

These are research candidates, not supported services. No automatic probing,
fallback queries or state changes are performed for them. Factory restore,
firmware update, provisioning, setters, cleaning/motion controls and warning
clearing remain outside this integration's scope.

## Promotion criteria

A further query needs a model-specific request and response trace, a bounded
parser, a useful data-point mapping and an assessment of side effects before it
enters the fixed allowlist. Offline tests must cover wrong-query isolation,
bad CRC/result, malformed data, deadlines and cleanup. A separately approved
live test must then establish the actual S1 wire shape before claiming support.
See [live acceptance](info_dp_validation.md#live-acceptance-still-required).
