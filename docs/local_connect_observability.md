# Direct-local connect observability and retained staleness evidence

Version 0.9.12 adds two changes for
[direct-local polling issue #16](https://github.com/purcell-lab/ha-aiper-ble/issues/16).
Neither changes Bluetooth behaviour: the same fixed queries run over the same
pinned transport, with one connection per request, no retry and no fallback.

## Why

The reported failure was a direct-local `bluez_Failed` at the **connect** stage
after approximately 1.686 seconds, with zero writes, zero notifications, zero
bytes and confirmed cleanup. That is well inside the 20-second connect budget,
so it is a fast local refusal rather than an expired attempt. The recorded
diagnostics could not show how long the connect took, where the query sat in the
cycle, how soon after the previous query's cleanup it started, or whether the
adapter was still seeing the robot at that moment. Query-to-query reconnect
timing therefore remained an untestable hypothesis.

## Recorded observations

Four bounded numeric fields join the existing fixed publication allowlist. All
are passive: none initiates Bluetooth activity, and none participates in
transport selection or any safety gate.

| Field | Meaning |
| --- | --- |
| `cycle_query_index` | 1-based position of this query in the automatic cycle |
| `seconds_since_previous_query` | Gap from the previous query's returned transport call, which completes after its confirmed cleanup, to the start of this one. Absent on the first query of a cycle |
| `connect_ms` | Elapsed milliseconds of the `Connect` call itself, recorded for refusals, timeouts and cancellations as well as successes |
| `preconnect_rssi_dbm` | The pinned adapter's own cached signal level, read during the existing pre-connect revalidation |

`cycle_query_index` and `seconds_since_previous_query` are recorded for both
polling transports, because they describe the cycle rather than the radio.
`connect_ms` and `preconnect_rssi_dbm` come from the pinned local probe; the HA
Bluetooth path already publishes per-phase timings and route snapshots.

BlueZ drops `RSSI` from a disconnected device once it is no longer being
observed, so `preconnect_rssi_dbm` of `null` records "not currently seen by this
adapter" rather than a missing measurement. Values outside -127 to 20 dBm are
recorded as `null`.

BlueZ exposes no last-advertisement timestamp, so the direct-local path also
takes one passive `local_preflight` route snapshot from Home Assistant's
existing per-scanner cache, in the same anonymised shape the HA path already
publishes. That supplies `advertisement_age_seconds`, per-route signal level and
slot counts without starting a scan, opening a connection or influencing the
pinned route. The snapshot reports `{"available": false}` when that cache cannot
be read; the query proceeds unchanged either way.

These fields are numbers only. No MAC address, device path, adapter identity,
SSID, serial, payload or exception text is added to diagnostics.

## What the data can and cannot settle

A distribution of `connect_ms` and `seconds_since_previous_query` across ordinary
production cycles, correlated with `cycle_query_index` and
`preconnect_rssi_dbm`, can show whether failures cluster after short gaps, on
later queries in a cycle, or when the adapter has stopped seeing the robot. That
would support or weaken the reconnect-timing hypothesis from normal polling,
with no live experiment and no additional connections.

It cannot identify the controller-level cause of a refused connection. That
still needs host-side `bluetoothd` or `btmon` evidence, which this integration
deliberately does not collect. No bounded-retry or settle-delay change is
included here; either remains a separate proposal requiring its own review.

## Retained last-successful-poll timestamp

Previously the first failed cycle made every telemetry entity unavailable,
including `sensor.aiper_ble_last_successful_poll`, because availability required
the coordinator's last update to have succeeded. The timestamp was still held in
memory; it was only hidden, exactly when it was most useful, leaving recorder
history as the only way to establish how stale the readings were.

Measured values still disappear on the first failure, so no stale reading is
ever presented as current. The last-successful-poll entity is staleness
evidence rather than a measurement, so it now keeps showing the retained
timestamp while polling fails or is suspended. It still becomes unavailable when
polling is disabled, when the entry is unloading, and before any successful
cycle has published a timestamp.

Downloaded diagnostics are unchanged in this respect: `field_evidence.current`
still reports `false` while the last cycle failed, so the retained timestamp
cannot be mistaken for fresh field evidence.

## Validation

Offline tests cover the per-query index and gap, connect timing on both a
successful and a refused connect, signal-level bounds and absence, the passive
snapshot stage, the retained timestamp alongside unavailable battery and
temperature during a failed cycle, and its disappearance when polling is
disabled. The full suite passes with 808 tests.

Installing this code requires an HA restart to load it. No live claim is made
here: any effect on direct-local reliability must be established from normal
automatic cycles after deployment.
