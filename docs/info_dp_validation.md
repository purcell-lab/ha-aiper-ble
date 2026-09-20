# INFO/WARN queries and useful data-point validation

Version 0.9.0 preparation, 20 September 2026. This document separates actual
robot evidence from app-derived mappings and offline test fixtures. Adding code
does not constitute a successful live INFO or WARN exchange.

## Data-point decisions

| Data point | Evidence | Entity policy |
| --- | --- | --- |
| S1_INFO temperature | Live response; app divides first value by 10 | Enabled, °C; physical location unknown |
| S1_INFO solar state | Live integer; meaning not verified across states | Enabled as raw solar status, no charging assertion |
| INFO battery level | App maps index 2 to `battLevel`; BatteryView uses 0-100 | Enabled, battery class and %; accept 0-100 only; live/app comparison pending |
| INFO operating status | App maps index 0 to status | Enabled raw integer; enum/live comparison pending |
| INFO operating mode | App maps index 1 to mode | Enabled raw integer; enum/live comparison pending |
| WARN warning code | S1 app parses first field as signed decimal Long | Enabled raw integer, no units/statistics/fault labels; live validation pending |
| S1_INFO raw temperature | Live but duplicate of scaled value | Disabled by default, diagnostic |
| S1 time zone | Live metadata, not operational telemetry | Disabled by default, diagnostic |
| OpInfo Wi-Fi RSSI | Live reply contains -127; sentinel meaning unverified | Disabled by default, raw diagnostic without dBm |
| OpInfo network name | App-derived, absent from observed reply | Disabled by default; privacy-sensitive if enabled |
| OpInfo bat/status/link | Speculative, not returned in observed reply | Disabled by default; not used as INFO substitutes |
| OpInfo nested Machine fields | Speculative shape, not returned; app cloud Machine is separate | Disabled by default; not treated as confirmed support |
| Last successful poll, polling status, manual result | Integration-generated operational health | Enabled; manual result is independent of polling |

There are 27 registry entries: nine enabled by default and 18 opt-in diagnostics.
For existing installations, migration from config-entry minor version 1 to 2
hides eligible optional entities once. It does not disable them or remove their
IDs/history, and skips user-hidden, user-disabled and custom-named entries.
Users can unhide a retained diagnostic after migration; later reloads preserve
that choice. Existing dashboards that explicitly name a hidden entity still work.

## Static validation

Offline inspection of the previously acquired Aiper Android 3.6.1 build 82:

- `S1PanelActivity.loadDataForCmd` issues separate `OpInfo`, `INFO`, `S1_INFO`
  and `WARN` requests. This change adds INFO and WARN to the existing two.
- The INFO consumer reads indices 0/1/2 as integers and assigns status/mode/battery.
- `BatteryView` declares full battery level as 100 and bounds its ranges to 0-100.
- The WARN consumer parses its first field with `Long.parseLong` and assigns
  `warnCode`. This establishes a signed decimal int64, not fault-bit meanings.
- `BleDeviceManager.sendQueryATInternal` constructs a no-argument status query
  as `Machine` with `cmd: AT+INFO?`. No setter or arbitrary AT input is exposed.
- The S1 OpInfo consumer reads Wi-Fi fields. MQTT `Machine.cap` is a separate
  cloud source and must not be relabelled as an observed BLE OpInfo battery.

See [APK provenance](aiper_ble_apk_361_evidence.md) and the
[discovered specification](AIPER_POOL_ROBOT_BLE_SPEC.md). No APK or decompiled
application code is included in this repository.

## Implementation and offline acceptance

The fixed INFO request includes mandatory data CRC 10442 for
`{"cmd":"AT+INFO?"}`, legacy XOR/base64 framing and newline termination.
The same guarded HA Bluetooth/proxy transport is used, with one connection per
request, no retries, no pairing and confirmed notification/disconnect cleanup.
The existing 300-second minimum interval and 180-second whole-cycle bound remain.
All four requests share that deadline, not four separate 180-second allowances.
Slow stages can exhaust the budget; no retry or timeout extension is introduced.

Expected response: `Machine.data.ack` or string `report` containing
`+INFO:<status>,<mode>,<battery>\r\n`. The strict three-field shape and CRLF are
integration policy based on the app and established AT framing, not yet an INFO
wire capture. Extra fields, invalid integers, wrong type/prefix, nonzero result
or invalid/missing CRC are rejected. Unknown codes stay raw; battery sentinels
become unavailable instead of zero, 100 or stale values.

WARN uses fixed `AT+WARN?`, mandatory data CRC 10501, and the same envelope
validation. Its strict expected text is `+WARN:<signed-int64>\r\n`.
Exactly one field and CRLF are integration policy, not a captured WARN reply.
It exposes only `warning_code_raw`, with no fault labels or clear-warning command.
See the [additional-query assessment](apk_query_catalog.md) for excluded queries.

Offline tests cover exact frames/independent CRC, field order, report precedence,
integer/battery boundaries, invalid frames, query isolation, four-query polling
through the proxy simulator, third/fourth-query failure and cleanup, default entity
selection, once-only registry migration, history/ID preservation and diagnostics
privacy. INFO and WARN fixtures are explicitly synthetic.

## Live acceptance still required

After explicit approval to merge, deploy and restart:

1. Confirm the app is closed, other BLE clients idle and robot stationary.
   Ensure the nearby active proxy is online. Review existing polling options:
   if polling is enabled, the restart itself initiates the first four-query
   cycle. Do not immediately run an additional query or bypass cooldown.
2. Check the first cycle completes S1_INFO, OpInfo, INFO and WARN with successful
   checksum verification and confirmed cleanup. Diagnostics expose only
   allowlisted field names from the last successful cycle, not raw payloads,
   SSIDs, serials or arbitrary response keys.
3. Verify actual battery/status/mode/warning values appear in the new sensors.
   Do not call synthetic fixture values actual robot readings.
4. Observe one automatic follow-up cycle. Confirm timestamps advance and no
   connection failures or repeated application writes occur.
5. Disable polling and wait for cleanup before opening the Aiper app.
   Compare battery percentage and status/mode with the app, recording the time
   gap. Re-enable only after closing the app. Do not change robot state merely
   to infer enum codes without separate authority.
6. If INFO/WARN shape or CRC fails, stop and inspect private diagnostics rather than
   relaxing guards, fabricating missing values or retrying repeatedly.

No production deployment, HA restart or live INFO/WARN query was performed while
preparing this PR. Live validation results must be added after the approved test.
