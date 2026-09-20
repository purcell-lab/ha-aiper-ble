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
adapter was still seeing the robot at that moment.

A read-only review of the first night on v0.9.11, recorded on issue #16, then
established from the HAOS host journal that every direct-local `bluez_Failed`
in that window, and the three failures in the original report, coincided to the
second with a kernel `hci0: ACL packet for unknown connection handle` event
carrying the same handle each time, on a local adapter that a second
integration polls every ~41 seconds. About half of the failures were the first
connect of a cycle after five or more minutes idle. The failure is therefore
adapter state, not integration timing: no settle delay would change it, and the
inter-query gap is of secondary interest. The fields below remain useful
because they make per-query attribution, connect duration and adapter-side
visibility explicit in ordinary diagnostics instead of inferred from recorder
history, which is how that review had to be done.

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

`cycle_query_index` and `connect_ms` show directly which connect of a cycle
refused and how fast, which the issue #16 review could only infer from cycle
timing. `preconnect_rssi_dbm` and the snapshot's advertisement age show whether
the pinned adapter was still seeing the robot at that moment.
`seconds_since_previous_query` is retained because it is cheap and rules the
reconnect gap in or out per failure, but the host journal has already shown
that failures occur on cold connects too.

None of this identifies the controller-level cause of a refused connection.
That needs host-side `bluetoothd`, kernel or `btmon` evidence, which this
integration deliberately does not collect; the issue #16 review obtained it
from the HAOS host journal. No bounded-retry or settle-delay change is included
here. A retry would likely succeed in practice but would treat a symptom of
adapter state; remediation belongs with the shared local adapter first.

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
