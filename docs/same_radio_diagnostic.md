# Same-local-radio diagnostic

Related investigation: [issue #7](https://github.com/purcell-lab/ha-aiper-ble/issues/7).
This action is experimental and is not a production transport or a proxy fix.

## Purpose

Compare the existing direct BlueZ OpInfo action with
`aiper_ble.query_opinfo_local_bleak` on the same saved local adapter.
The second action still uses HA's patched Bleak client, slot accounting,
connection tracking, callbacks and disconnect lifecycle. It restricts route
selection for this client only, rather than disabling proxies globally.

HA does not pin its route simply because the caller passes a local BLEDevice.
This diagnostic therefore overrides one private selector on a per-call subclass.
It is explicitly gated to habluetooth 6.26.11, Bleak 3.0.2 and
bleak-retry-connector 4.7.0. Missing wrapper support, changed dependency versions,
an ambiguous route, stale advertisement, busy endpoint or unavailable slot
refuses the attempt. It does not fall back to unwrapped Bleak.

The expected local backend and saved device path are checked again after
connection and immediately before the fixed write. Production selection,
recurring polling and the other four isolated actions are unchanged.
After any observed connect call, a separate bounded BlueZ metadata read verifies
that the saved device is disconnected. A cleared HA backend alone is not taken
as proof. Missing state, an identity mismatch or a bus error suspends further
queries for review; this check never issues another connect or disconnect.

## Controlled comparison

1. Record the last completed poll and confirmed cleanup. Keep the robot in the
   same position, the app closed and all other BLE clients idle.
2. Disable recurring Aiper polling, preserving the other options. Wait at least
   the configured interval after the latest completed attempt. An options
   reload clears volatile cooldown state; do not treat that as permission to
   shorten the real elapsed cooldown.
3. With `use_local_adapter: true`, call `query_opinfo` once. Save its response.
   Stop if notification or disconnection cleanup is uncertain.
4. Wait at least the configured interval after cleanup. Call
   `query_opinfo_local_bleak` once, supplying `entry_id`, `confirm_app_closed`,
   `confirm_query_write` and `confirm_notifications`, all confirmations true.
5. Verify `transport: ha_bluetooth_local`, `backend: bleak_bluez`,
   `local_route_asserted: true`, one observed connect call, no refused retry,
   a matched CRC-verified response, stopped notifications and confirmed
   disconnection. A failure is evidence, not permission to retry immediately.
6. Only after the full post-cleanup interval, restore the original polling
   options. Verify a complete four-query cycle on direct local BlueZ.

Both actions share the coordinator mutex, completion-based cooldown, cancellation
handling and cleanup suspension. The diagnostic never publishes a partial sensor
cycle, pairs, sends controls, changes proxy firmware, changes another integration
or retries a query. Advertisement freshness is required within ten seconds for
this explicit local-route experiment.

## Interpretation

- If both paths pass, HA-managed local Bleak is viable for that test. The failing
  proxy route remains a separate radio/backend/firmware investigation.
- If direct BlueZ passes and local HA/Bleak fails, inspect the differing local
  connection lifecycle before investigating remote radio transport.
- This selector intentionally bypasses HA's normal route ranking. Success does
  not validate normal best-route selection or automatic failover.
- No outcome alone proves long-term reliability or identifies a specific
  ESPHome MTU/GATT defect. Native proxy-side event capture is still required.

Live execution of the new action is pending deployment and authorised isolation.
