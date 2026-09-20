# Passive transport diagnostics

This diagnostic-only change supports [investigation #7](https://github.com/purcell-lab/ha-aiper-ble/issues/7).
It does not change the default transport, routing, polling interval, timeout
budgets, retries, cache policy, fixed query bytes, CRC checks or cleanup gates.
No automatic fallback is added. The working pinned local BlueZ path is retained.

## Where to find the evidence

After an already-authorised query or polling cycle, download integration
diagnostics from Home Assistant. Each retained polling query and isolated query
result includes `transport_diagnostics`; downloading only reads cached results
and does not scan, connect or poll.

The schema records:

| Field | Meaning and limits |
| --- | --- |
| `started_at`, `elapsed_ms` | UTC query start for log correlation, and monotonic elapsed time including cleanup |
| `phase_ms` | Aggregated HA-route phase durations, including connection, notification setup, writes, response wait, decoding and cleanup; repeated phases are summed, not an unbounded event log |
| `requested_transport` | Explicitly configured path, not evidence of the selected radio |
| `backend` | `local_bluez`, observed `bleak_bluez` or `bleak_esphome`, otherwise `unknown` |
| `selected_route` | Anonymous selected-scanner hint, if available; `null` otherwise |
| `route_snapshots` | Cached candidates before connection, after successful connection and after cleanup; not a routing decision or proof of historical slot release |
| Route fields | Per-target advertisement age and RSSI; scanner type, free/total slots, target-specific connection failure count, scanner-wide connections in progress |
| `timeouts_seconds` | Existing HA transport budgets, unchanged by this PR |
| `inner_connect_timeout_seconds` | Actual timeout argument seen by the single-attempt client, when observed; absent if the wrapper never receives a call |
| `outer_connect_expired`, `exchange_expired` | Whether those specific timeout scopes expired; `null` when never entered |
| `connect_calls_observed`, `retry_calls_refused` | Calls reaching the integration's single-attempt wrapper, and refused helper retries; these are not controller-level link-attempt counters |
| `errors`, `cancelled` | Bounded fixed exception-family chains and external cancellation evidence, without exception messages or arbitrary class names |

Pinned local D-Bus deliberately records only total
`local_probe_including_cleanup` timing. Its existing probe stages and cleanup
results remain available, but individual D-Bus phase timing is not instrumented.
It never queries HA scanner metadata for these new diagnostics.

## Unknown is not a proxy failure

Candidate order, best RSSI and a free slot do not prove which backend attempted
the connection. HA may clear its backend after a failed connection; a missing
selected route remains `null`, and a backend that cannot be classified remains
`unknown`. A successful observation is retained through cleanup.

The observer feature-detects public scanner metadata and uses two optional
read-only HA wrapper hints, `_backend` and `_connected_scanner`. These are private,
version-sensitive hints, not supported routing APIs; missing or failing metadata
must not affect connection safety checks. No monkeypatch, backend selection
override or parsing of human-readable reachability diagnostics is used.
The relevant implementation is the
[habluetooth wrapper](https://github.com/Bluetooth-Devices/habluetooth/blob/v6.26.11/src/habluetooth/wrappers.py);
the public discovery interface is described in the
[HA Bluetooth API](https://developers.home-assistant.io/docs/core/bluetooth/api/).

Advertisement age uses the target's timestamp from that scanner and the same
coarse monotonic clock used by habluetooth. Missing, invalid or future timestamps
produce `null`, not a fresh advertisement. Optional numeric fields likewise use
`null` when unavailable. No scanner-wide last-detection time substitutes for a
target advertisement.

## Privacy and bounds

Only fixed labels and validated numeric metadata leave the observer. Route IDs
are assigned within each query, are capped at 16 and cannot be used to correlate
a radio across separate queries. Snapshots indicate truncation when necessary.
Raw source identifiers, MACs, paths, names, SSIDs, serials, slot-owner lists,
advertisement payloads, exception text and arbitrary class names are excluded.
Exception chains are limited to three categories, including cyclic chains.

Review downloaded diagnostics before posting: these additions do not make
every existing HA diagnostic or external proxy log suitable for publication.
UTC timestamps and signal/slot metrics remain operational metadata.

## Validation and next experiment

Offline regression tests check fixed bytes, single-attempt refusal, timeout
attribution, cancellation, cleanup, coordinator propagation and passive download.
Metadata tests cover absent and raising APIs, invalid numbers, bounded routes,
anonymous IDs and backend/exception privacy.

This PR does not deploy, restart HA, change proxy firmware or run a live test.
A separately authorised experiment should first compare direct D-Bus and
HA/Bleak on the same radio, confirming actual route attribution rather than
assuming that a local BLEDevice pins HA routing. If attribution is unknown,
record the test as inconclusive instead of claiming the proxy was selected.
