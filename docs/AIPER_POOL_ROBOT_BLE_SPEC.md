# Aiper Surfer S1 BLE: discovered protocol and validation status

Updated 20 September 2026 for integration version 0.9.0. This is a limited,
evidence-led description of the legacy Surfer S1 telemetry path, not a universal
Aiper protocol specification or an official vendor document.

## Scope and evidence

Four evidence classes are kept separate:

- **Observed:** anonymised results from controlled tests on one Surfer S1:
  service discovery, fixed S1_INFO and OpInfo queries, a 60-second listen-only
  observation, and subsequent polling. Raw captures remain private because they
  can contain Bluetooth addresses, serial numbers and installation metadata.
- **App-derived:** offline inspection of `com.aiper.link` Android version 3.6.1,
  build 82. See [APK evidence](aiper_ble_apk_361_evidence.md) for hashes,
  acquisition provenance, incomplete-decompilation caveats and call chains.
- **Implementation policy:** restrictions imposed by this integration for
  predictable, bounded operation. They are not claims about firmware limits.
- **Unverified:** candidate fields, semantics, other models and protocol variants
  not established by the observed exchanges.

The initial reference was
[knobunc/aiper's pool robot BLE specification](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md),
which attributes its findings to app version 3.3.2. The account below incorporates
later independent inspection and tests; discrepancies do not establish whether
the original account was mistaken or the app changed.
See [acknowledgements](../ACKNOWLEDGEMENTS.md) for all development references.

## Discovery and protocol selection

The implementation accepts configured names beginning `Aiper-Surfer S1-` or
`Aiper_Surfer S1_`. It then requires the full configured name and Bluetooth
address to match, rather than accepting any device sharing the prefix.
Names and addresses are selection checks, not cryptographic authentication.

| Item | UUID | Status |
| --- | --- | --- |
| Legacy service | `4a5ad444-2537-11ee-be56-0242ac120002` | Observed on the test unit |
| Legacy data characteristic | `4a5a54e6-2537-11ee-be56-0242ac120002` | Observed status exchange endpoint |
| Key-exchange characteristic | `4a5a64e6-2537-11ee-be56-0242ac120002` | Unsupported-protocol veto, not used |

App-derived manufacturer-data handling uses company ID `0x0000`. A first byte of
`0x01` indicates the newer/ECDH path; another first byte is a legacy candidate.
Absent or empty data is unknown, not positive proof of legacy support.
The test unit lacked the relevant advertisement evidence, so initial tests
required explicit permission to proceed with the guarded legacy probe.

Implementation policy rejects malformed manufacturer data, ECDH evidence,
security-gated endpoints and ambiguous service/characteristic matches.
The legacy endpoint must have notification and the required write capability.
Missing advertisement evidence needs a separate opt-in and never overrides
contradictory evidence. No pairing or key exchange is implemented.

## Legacy frame encoding

The app-derived encoding, exercised by successful local queries, is:

1. Serialize the outer object as compact UTF-8 JSON.
2. XOR every byte with the repeating byte sequence `12 34 56 78` hexadecimal.
3. Base64-encode the resulting bytes as ASCII.
4. Append one LF byte (`0A`).

Receive processing reverses those steps after reassembling LF-delimited frames.
A notification is not necessarily a complete frame: fragmentation and multiple
frames in one notification are handled. XOR is obfuscation and the checksum
detects corruption; neither authenticates the robot or encrypts it securely.

### Checksum

The legacy `CmdFactory` checksum is a reflected CRC-16:

- Initial register: **`0x9966`**.
- Reflected polynomial: **`0xA001`**.
- XOR each input byte into the register, then perform eight right-shift steps,
  XORing the polynomial whenever the previous low bit was one.
- Return the low 16 bits, with no additional final XOR.
- Input is the compact UTF-8 JSON **data object**, not the complete envelope.
  The receiver implementation preserves received data-key order.

| Input bytes (UTF-8) | Decimal checksum |
| --- | ---: |
| Empty byte string | 39270 |
| `{}` | 6921 |
| `123456789` | 8055 |
| `{"key":"value"}` | 42401 |
| `{"cmd":"AT+S1_INFO?"}` | 49921 |
| `{"wifi_rssi":-127}` | 52546 |

These are deterministic test vectors, not private capture contents.
The earlier `0x9996` seed produces a different result and is not the seed
recovered from this legacy app factory. A separate SDK checksum using a
non-reflected `0x1021` algorithm must not be substituted.

## Supported status requests

Only the following two fixed requests are used by recurring polling.
No provisioning, movement, cleaning, mode-setting or arbitrary AT command is
exposed. Request writes and notification subscriptions still change connection
state; “telemetry-only” does not mean no GATT writes occur.

### S1_INFO

Plaintext request:

```json
{"type":"Machine","data":{"cmd":"AT+S1_INFO?"},"chksum":49921}
```

Base64 wire line, followed by a literal LF:

```text
aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTQQUJMVxyGUcwSXpacVw9C2dZdEImDW9KI0k=
```

The matching reply has type `Machine`. The parser uses `data.report` if it is a
string, otherwise `data.ack`. The accepted status text is:

```text
+S1_INFO:<temperature_raw>,<solar_status_raw>\r\n
```

Here `\r\n` denotes actual CR and LF characters inside the decoded string.
The observed reply was `+S1_INFO:215,0\r\n`, with `timeZone` equal to `UTC+10`.
It supports **21.5 °C** using the app-derived division by ten, and solar code
`0`. Later successful polling also produced raw temperature `292`, or 29.2 °C.
Neither result establishes whether the sensor measures water, air, the enclosure,
or a circuit-board temperature.

The integration requires exactly two bounded numeric fields. This is a safety
restriction stricter than a general-purpose app parser, not a claim that every
firmware must return exactly this format. Solar codes remain uninterpreted.
The full captured data object and its checksum are not reproduced because the
data can include device identifiers; deleting or redacting fields changes CRC.

### OpInfo

The observed working request, matching the app's null-input serializer, is:

```json
{"type":"OpInfo","data":{}}
```

Base64 wire line, followed by a literal LF:

```text
aRYiAWJRdEIweyYxfFI5Wj4WMhlmVXRCaUkr
```

The legacy serializer emits an empty object but omits `chksum` for this null
input path. The diagnostic harness retains an explicit-empty-object alternative
with `"chksum":6921` for offline inspection and controlled comparison.
Recurring polling uses the working omission form, not automatic fallback trials.

The observed response contained only this telemetry:

```json
{"type":"OpInfo","data":{"wifi_rssi":-127},"chksum":52546,"res":0}
```

The outer key order above is presentational; the checksum covers only the
displayed data object. `-127` is kept as a raw value. Its meaning as a sentinel,
disconnected state or physical signal strength has not been established.
There was no `bat`, `cap` or nested `Machine` telemetry in this response.

### Separate INFO query: five-field reply observed

The S1 app panel requests `OpInfo`, `INFO` and `S1_INFO` separately.
`S1PanelActivity.loadDataForCmd` maps the three INFO positions to status, mode
and `battLevel`; its OpInfo handler reads Wi-Fi information only. The separate
MQTT shadow `Machine` component does not establish a nested BLE OpInfo shape.

The fixed request implemented in v0.9.0 is:

```json
{"type":"Machine","data":{"cmd":"AT+INFO?"},"chksum":10442}
```

At 13:15 AEST on 20 September 2026, a single pinned local BlueZ query received
`+INFO:0,0,93,0,155\r\n` in `Machine.data.ack`, with a valid data CRC and result
zero. The former three-field-only parser rejected this legitimate five-field
response. The app reads only indices 0/1/2, assigning status, mode and battery.
Version 0.9.4 accepts exactly three or five signed int32 decimal fields with
CRLF termination. The last two fields are validated as integers but are neither
interpreted nor published as sensors. Their meanings remain unknown.
A battery outside 0-100
becomes unavailable, without clamping or preventing valid status/mode values
from being used. Status and mode remain raw codes. Other field counts or
malformed matching responses fail closed. Report takes precedence over ack,
as in the existing S1_INFO parser.

See [DP validation and deployment acceptance](info_dp_validation.md) for the
remaining live checks and the distinction between app evidence and wire evidence.

### Separate WARN query: raw warning code observed

The S1 panel also issues `WARN`. Its consumer reads the first decimal field
using Java `Long.parseLong` and assigns `warnCode`. Version 0.9.0 adds the
fixed request `{"type":"Machine","data":{"cmd":"AT+WARN?"},"chksum":10501}`.
The integration requires `+WARN:<signed-int64>\r\n` in Machine ack/report,
with exactly one field. At 13:00 AEST on 20 September 2026, a pinned local BlueZ
query returned the verified raw value zero, with notification and connection
cleanup confirmed. Warning codes have no units, statistical state class or inferred
fault labels. The existing full-data CRC, result and cleanup gates apply.
No clear-warning or other setter is exposed.

See the [APK query assessment](apk_query_catalog.md) for additional candidates
including RECORD, ULTRAS, DevInfo and other-model configuration queries.
They remain excluded from the executable allowlist.

### Response acceptance

The polling coordinator requires the expected reply type and shape, integer
`res` equal to zero, an integer checksum in `0..65535`, and a matching CRC over
the full data object before selecting publishable values. Duplicate JSON keys,
nonfinite numbers and unsupported envelopes are rejected.

Early one-shot diagnostics reported response checksums as unverified.
The later polling coordinator validates them before publishing sensors.
All four fixed query replies and confirmed connection cleanup are required before
a cycle publishes. A reply alone is not a successful polling cycle.
There is no request ID, so a matching response and valid CRC do not by themselves
prove request-response causality or exclude an unsolicited matching frame.

## Advertisements, notifications and polling

BLE advertisements supplied discovery identity, but no battery or temperature
advertisement payload was established in this work. A connected, subscribed
listen-only test observed **zero notifications over approximately 60 seconds**.
That result is limited to one robot, state and interval; it does not prove that
unsolicited notifications never occur.

The reliable path demonstrated here is an application query followed by a
notification response. Recurring polling is therefore implemented rather than
assuming that advertisements or a standing subscription will refresh sensors.
Listen-only means no application query was sent; enabling notifications can
still cause a CCCD write by the Bluetooth stack.

## Published data points and uncertainty

The component defines 24 telemetry sensors and three integration-status sensors.
On new installations, six telemetry sensors and all three status sensors are
enabled by default. The remaining 18 diagnostics are disabled by default.
Optional fields are unavailable when absent or invalid; they are not
filled with zero, inferred from another query or dynamically generated from
arbitrary response keys.

| Reply field | Published value | Evidence and interpretation |
| --- | --- | --- |
| INFO third field | Battery, % | Live 93; app `battLevel` and BatteryView 0-100 scale; app comparison pending |
| INFO first field | Operating status raw | Live 0; app-derived position; enum meaning unverified |
| INFO second field | Operating mode raw | Live 0; app-derived position; enum meaning unverified |
| WARN first field | Warning code raw | Live 0; app-derived signed int64; fault meanings unverified |
| S1_INFO first field | Temperature, °C | Observed numeric reply; app-derived scale ÷10; physical sensor location unknown |
| S1_INFO first field | Temperature raw | Observed |
| S1_INFO second field | Solar status raw | Observed; enum meanings unverified |
| S1 reply `timeZone` | Time zone | Observed; bounded metadata string |
| OpInfo `wifi_rssi` | Wi-Fi RSSI raw | Observed `-127`; sentinel meaning unverified |
| OpInfo `wifi_name` | Wi-Fi network | App-derived optional field; not present in observed reply |
| OpInfo `bat` | Battery raw | Optional candidate, not verified SOC |
| OpInfo `status` | Status raw | Optional candidate, enum unverified |
| OpInfo `link` | Link raw | Optional candidate, enum unverified |
| OpInfo `Machine.cap` | Battery capacity raw | Optional nested candidate; not observed or verified SOC |
| OpInfo `Machine.mode` | Mode raw | Optional nested candidate |
| OpInfo `Machine.solar_status` | Solar status raw | Optional nested candidate |
| OpInfo `Machine.status` | Status raw | Optional nested candidate |
| OpInfo `Machine.temp` | Temperature raw | Optional nested candidate; no assumed scale |
| OpInfo `Machine.warn` | Warning raw | Optional nested candidate |
| OpInfo `Machine.warn_code` | Warning code raw | Optional nested candidate |
| OpInfo `Machine.in_water` | In-water raw | Optional nested candidate; no assumed boolean mapping |
| OpInfo `Machine.link` | Link raw | Optional nested candidate |
| OpInfo `Machine.light` | Light raw | Optional nested candidate |
| OpInfo `Machine.visual` | Visual raw | Optional nested candidate |

The other three sensors show last successful poll, polling status and the manual
diagnostic result. All 27 entities are grouped under one integration-scoped HA
device, without automatically merging into a cloud integration's device.
Existing entity unique IDs remain unchanged. A once-only migration hides
uncustomised existing optional diagnostics, but does not disable them, delete
registry records or discard history. User hiding/disabling and custom names are
preserved. Users may unhide them after migration; reloads respect that choice.

Integer candidates accept signed 32-bit integers, except `warn_code` and WARN, which
accepts signed 64-bit. Booleans, numeric strings and floats are not accepted as
these integer fields. Wi-Fi names must be printable, nonempty strings of at most
32 UTF-8 bytes; the time-zone value is restricted to 1-64 characters matching
`[A-Za-z0-9_+:/.-]`. These limits are integration policy.

SSID state can appear in local HA state/history if returned. It is excluded from
the integration's diagnostic export. Serial numbers, tokens, passwords and
unrecognised response fields are not turned into sensors.

### Battery evidence

Static inspection found the Surfer app panel mapping `Machine.getCap()` to its
battery-level view, and a separate `INFO` parser handling status, mode and battery
level. This is not evidence that the tested OpInfo reply carries SOC. Version
0.9.0 implements the separate fixed `INFO` and `WARN` queries, but not `DevInfo`.

Neither `bat` nor `cap` is labelled as a battery percentage, and neither receives
a battery device class. Only INFO field 2 receives the battery class, based on
the app's explicit `battLevel`/0-100 display mapping. A guarded live exchange and
comparison against the app's displayed percentage remain required.

## HA Bluetooth transport and operational bounds

Recurring polling uses HA's shared Bluetooth framework and its connectable local
adapters or active proxies, rather than creating an independent scanner.
The implementation uses `async_ble_device_from_address` and
`async_scanner_devices_by_address`, then the HA-wrapped Bleak connection path.
This design builds on [Home Assistant Core](https://github.com/home-assistant/core)
and [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector).

The following are implementation policies, not measured firmware capacities:

| Bound | Current policy |
| --- | --- |
| Queries per cycle | S1_INFO, OpInfo, INFO, WARN, each in a fresh bounded connection |
| Actual connection attempts | One per exchange; no retry loop |
| Connect / exchange deadlines | 30 seconds / 15 seconds |
| Write deadline | 5 seconds per chunk |
| HA transport chunking | 20-byte chunks, write with response |
| Notification queue | 32 entries |
| Frame / total capture / notification limits | 4,096 bytes / 8,192 bytes / 64 notifications |
| Stop-notify / disconnect deadlines | 5 seconds / 10 seconds |
| Whole-cycle bound | 180 seconds |
| Normal polling interval | Default/minimum 300 seconds; maximum 3,600 seconds |
| Failure handling | Exponential backoff, capped at 3,600 seconds; unsafe cleanup suspends polling |

Identity and protocol evidence are rechecked before connecting, after connecting,
and after enabling notifications before writing. All selectable routes must
pass the guard. Nameless or differently named proxy records can therefore produce
`identity_changed` even when a different adapter sees the expected name.
This is a known fail-closed limitation, not proof that the robot changed identity.
Proxy support in the code and mocked tests does not establish successful radio
operation through every proxy.

Polling is disabled by default. Enabling it requires an explicit exclusive-access
confirmation. Keep the app closed and other BLE clients idle; remote proxies do
not expose every local BlueZ connection-ownership property.

`aiper_ble_diagnostics.poll_now` requires an enabled, authorised config entry and
`confirm_app_closed: true`. It may shorten failure backoff after the configured
normal interval has elapsed since the preceding attempt finished. It cannot
bypass the normal interval, an active operation, suspension, unloading or identity
checks. Scheduled polling and manual diagnostics share an exclusion guard.

Legacy discovery/read/query/listen actions still use their local BlueZ path;
they are not silently routed through remote proxies. Downloading diagnostics
does not initiate Bluetooth traffic.

## Reproduction and remaining work

The offline tests in `tests/aiper_ble_diagnostics/` cover encoding vectors,
fragmented framing, CRC acceptance, field validation, connection bounds, cleanup,
proxy-route guards, manual polling and entity/device lifecycle. They do not
substitute for physical tests across firmware versions.

Open questions include physical temperature location, solar enum meanings,
Wi-Fi sentinel meaning, availability and units of battery fields, spontaneous
notifications in other robot states, and safe handling of incomplete proxy
identity metadata. ECDH, other models, provisioning and robot controls remain
outside this integration's supported scope.
