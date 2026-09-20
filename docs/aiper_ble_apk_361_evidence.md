# Aiper 3.6.1 static evidence for the guarded harness

This note preserves the static-inspection stage. Subsequent live results,
including the observed S1 CRLF terminator and polling response-CRC validation,
are documented in the [discovered BLE specification](AIPER_POOL_ROBOT_BLE_SPEC.md).
Statements below about missing wire validation describe the earlier stage,
not the current integration's full evidence.

Inspection date: 20 September 2026. Scope: package `com.aiper.link`, version 3.6.1, build 82, acquired through the [APKCombo download page](https://apkcombo.com/aiper/com.aiper.link/download/apk). The package was not installed or executed; no hardware traffic was captured.

## Provenance and limits

```text
Base APK SHA-256
b2e81eb9db09873dee7f0f3887008f810383d66b4df354894e78da565021d2a1

XAPK SHA-256
6e880feb2f2e2dc2ba7a4196f34a03b8445d3931814d86d757693169367c57f6

Signing certificate SHA-256
05f52336af50e794a6c9d671637cf014160690e62ba035781bf1c032b29ae94e
```

Archive checks and APK v2/v3 signature verification passed. This verifies cryptographic integrity, not independent publisher identity against a Google Play installation or absence of malware. JADX 1.5.6 recovered the relevant classes; the full decompile was incomplete. One failed Java method was additionally examined in JADX fallback/raw-instruction output. No APK, decompiled source tree, device identifiers or authentication material is committed here.

The initial harness followed the [community specification attributed to app 3.3.2](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md). Differences below may reflect errors in that account or changes between app versions; this inspection does not establish which.

## Serializer call chain

These are original static-analysis findings from the package identified above.

- `com.aiper.device.surfer.ui.activity.S1PanelActivity.loadDataForCmd` calls `CmdManager.sendAndSubscribe` with command `OpInfo`.
- `com.aiper.device.common.cmd.CmdManager.sendAndSubscribe$lambda$9` passes null data into `BleManager.BleDeviceManager.send$default`.
- The pending-command collector calls `BleDeviceManager.processBleWriteWithRetry`. The recovered Java for that method was incomplete. A targeted fallback decompile of `BleManager` shows a call to `CmdFactory.create$default` using command/input, the device type, `activeEncryptProtocol()`, and mask 8. `CmdFactory.create$default` interprets mask 8 as encryption enabled.
- `com.aiper.device.api.CmdFactory.create` serializes null data for the non-X9 path as an empty data object and does not add a checksum. This gives `{"type":"OpInfo","data":{}}` before legacy XOR/Base64/newline framing.
- For a non-null explicit empty object, `CmdFactory.crc16` uses `CRC16Modbus(39270)`: seed `0x9966`, reflected polynomial `0xA001`. `{}` yields 6921. The initial harness's `0x9996` produced 6989 and is not what this factory uses.

The app has a separate SDK serializer, `com.aiper.sdk.devicecontroller.cmd.protocol.CmdContentConverterKt`, with a non-reflected `0x1021` algorithm and the same seed. Its `{}` result is 54666, and its type/data converter omits null data entirely. The harness deliberately follows the traced legacy factory; it does not mix these two serializers. Fixed regression vectors distinguish both algorithms and the earlier seed.

## Guard evidence

- `BleManager.isNewProtocol` treats absent manufacturer data as false rather than fatal. Its scan caller checks company ID zero; first byte one identifies the newer protocol.
- `BleManager.activeEncryptProtocol` uses an exchanged key if present and can otherwise select XOR. This is not evidence that every device with absent advertisement data supports XOR.
- `DeviceControllerFactory` describes the legacy service `4a5ad444-2537-11ee-be56-0242ac120002`, legacy characteristic `4a5a54e6-2537-11ee-be56-0242ac120002`, and key-exchange characteristic `4a5a64e6-2537-11ee-be56-0242ac120002`.
- `BleTransport` checks discovered service/characteristic availability for key exchange and can fall back to `WRITE_DEFAULT` for a write-capable endpoint.

The correction is stricter than the app: absent evidence needs a separate false-by-default per-call confirmation; any target key-exchange characteristic vetoes the test; a fully resolved and unambiguous legacy endpoint is still required. It does not implement ECDH, pairing, automatic protocol selection, transport fallback or retry.

## Fixed S1_INFO query and temperature scope

`S1PanelActivity` reads shadow `Machine.temp` with a divide-by-ten scale. Its separate `sendQueryATAndSubscribe("S1_INFO")` handler parses response field zero as a float, divides by ten and assigns it to the temperature state; field one supplies solar state. The display treats that state as Celsius and optionally converts it to Fahrenheit.

The panel's `OpInfo` handler extracts Wi-Fi RSSI and network name. These findings do not establish that a particular robot's OpInfo response supplies temperature or identify the sensor's physical location. Version 0.4.0 adds `S1_INFO` as a second fixed query choice, not a generic AT-command API or a temperature entity.

The additional static trace is:

- `CmdManager.sendQueryATAndSubscribe("S1_INFO")` delegates to `BleManager.BleDeviceManager.sendQueryATAndSubscribe`.
- That helper calls `sendQueryAT$default` with an empty argument array and null serial. `sendQueryATInternal`'s no-arguments path constructs a `Machine` request with `cmd: "AT+S1_INFO?"` and null `sn`.
- The legacy `CmdFactory` uses a Gson builder with null serialization disabled. The recovered builder's `f11631g` is false, passed into Gson's `f11603i` field, labelled `serializeNulls` by its `toString`. This setting is passed to its writer. Null `sn` is therefore omitted, not sent as an identifier.
- Non-null data is serialized compactly and receives the same legacy CRC. `{"cmd":"AT+S1_INFO?"}` gives 49921, independently cross-checked with a table-based CRC calculation.
- `CmdResponse.getATDataString()` accepts type `Machine`, preferring string `data.report` and otherwise using string `data.ack`. The query parser requires prefix `+S1_INFO:`, removes that prefix and two trailing characters, then splits fields by comma.
- The S1 panel consumes the first two fields: float temperature divided by ten, and integer solar state. It does not identify the physical sensor location in this trace.

The fixed cleartext request before XOR/Base64/newline framing is:

```json
{"type":"Machine","data":{"cmd":"AT+S1_INFO?"},"chksum":49921}
```

No command arguments, assignment form, serial, subscription command or caller-supplied payload is exposed. Each invocation sends either this request or OpInfo, never both. The existing empty-data checksum setting applies only to OpInfo; it cannot suppress S1_INFO's mandatory nonempty-data CRC.

The harness accepts only a `Machine` report/ack with `+S1_INFO:<decimal>,<integer>\r\n`. **CRLF is a conservative framing assumption:** the recovered parser strips two trailing characters, but this inspection did not obtain a robot wire capture proving their values. Exactly two fields, bounded decimal notation and an int32 solar state are deliberately stricter than the app. A matching reply with another shape fails closed without retries or guessed field mappings; unrelated replies are ignored within the existing deadline.

Only `temperature_raw`, `temperature_celsius` and `solar_status` are surfaced as numeric diagnostic candidates. Temperature scaling is app-derived, not sensor-location or hardware validation. The solar value remains an integer, not an inferred charging boolean. Response checksum and request-response causality remain unverified; there is no request ID.

## Validation boundary

Local and CI tests exercise the real HA framework with a fake BlueZ bus and disabled network sockets. They cover corrected golden frames, opt-in defaults, target-scoped key-exchange vetoes, evidence changing before connect and before writes, cleanup, privacy and existing bounds. Passing tests are not a live robot validation.
