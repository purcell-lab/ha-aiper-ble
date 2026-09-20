# Bounded native ESPHome trace

This is an explicit diagnostic action, not a production transport change.
It supports the unresolved investigation in [issue #7](https://github.com/purcell-lab/ha-aiper-ble/issues/7).
It has not yet been live-validated. Merge, installation, restart and one live
query need separate operator authorisation.

## Scope and isolation

`aiper_ble_diagnostics.query_opinfo_proxy_trace` takes `entry_id` and
`proxy_entry_id` (the ESPHome config entry, not the Bluetooth entry).
It requires all four confirmations: `confirm_app_closed`,
`confirm_query_write`, `confirm_notifications`, and `confirm_proxy_logging`.

Recurring polling must be disabled. The existing task mutex, completion-based
cooldown (at least 300 seconds), fixed OpInfo frame, CRC checks, single connect
attempt, no pairing, no retries and cleanup suspension remain in force. It
does not publish partial telemetry or change options. It overrides transport
selection for this action only, even when production uses direct local BlueZ.

The selected proxy must be loaded and connected with a fresh (at most 10-second)
target advertisement, all reported slots free and no connection in progress.
The per-client HA selector is restricted to that source. The actual
`ESPHomeClient` backend and source are checked after connection and before
application writes. No other scanner is unregistered or modified.

Package support is deliberately narrow: habluetooth 6.26.11, Bleak 3.0.2,
bleak-retry-connector 4.7.0, bleak-esphome 4.0.0 and aioesphomeapi 46.2.0.
ESPHome firmware must report 2026.9.0. Different versions fail closed.
This is a compatibility gate, not proof of firmware binary provenance.

## Native logging

A separate native API connection uses credentials already held inside HA.
No credentials are accepted by the action or returned in results. Its identity
is checked against the loaded proxy before logging or the BLE query.
It uses the shared connection's numeric peer address, avoiding a separate
mDNS lifecycle. No reconnect loop or new Bluetooth scanner is created.

The connection subscribes at DEBUG, without `dump_config`. It does not alter
the MCU logger level, firmware, HA logger levels or HA's existing subscription.
ESPHome stores the subscription level on each API connection and sends logs
to matching clients independently
([subscription implementation](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/api/api_connection.h),
[log fanout](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/api/api_server.cpp)).

A same-connection device-info round trip (three-second limit) orders the
subscription before the BLE attempt. It does not acknowledge event availability.
The dedicated socket and loaded proxy entry identity are checked before
connection and again before notification subscription and query writes.

The action has an 80-second work deadline, including the existing 30-second
outer BLE connection and 15-second exchange budgets, plus bounded cleanup
during timeout/cancellation. Native API setup is limited to 12 seconds.
A two-second tail follows BLE cleanup. Cleanup sends NONE on the dedicated
connection, removes its callbacks and force-closes that connection within
five seconds. Closing the dedicated connection does not close HA's API client.
NONE has no delivery acknowledgement; results distinguish send from confirmed
local socket closure. Uncertain cleanup suspends further operations.

Retained data is capped at 128 structured events. Processing stops after
2,048 messages or 256 KiB; individual messages over 2 KiB are ignored.
The remaining stream is discarded until the bounded action ends. A reached
capture limit aborts at the next pre-connect/pre-write guard; an already
started query is never replayed. It does not
write raw logs to disk, the HA log, sensors or diagnostics.

Only exact, allowlisted `esp32_ble_client` messages bearing the target's
address are converted into events. Addresses and raw text are discarded.
Events cover connecting/open, GATT lifecycle, MTU result, service discovery,
disconnect and selected error codes
([firmware event formats](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/esp32_ble_client/ble_client_base.cpp)).
Unknown formats and messages from other devices are dropped. Slot snapshots
remain HA-side metadata, not native slot events.

The formatter also accepts the optional task-name bracket emitted by the
[2026.9.0 logger](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/logger/log_buffer.h).
Task names are never retained. Fixed counters distinguish oversized lines,
unmatched headers (including a separate BLE-tag count), other-device messages,
target headers, and unmatched target messages. They retain no raw text.

The first v0.9.7 live run received 14 native log messages but matched zero
events. It timed out during connection with no query writes; BLE and dedicated
API cleanup were confirmed. This does not establish where the MCU connection
failed. v0.9.8 adds the documented header variation and counters for a repeat
bounded investigation, not a transport or firmware fix.

## Interpretation limits

- Times are HA receipt UTC and monotonic offsets, not MCU event timestamps.
  Network buffering can delay delivery.
- Firmware compiled/runtime logging below DEBUG may omit MTU/GATT events.
  Requesting DEBUG does not raise its firmware logger level.
- A subscription request has no positive delivery acknowledgement. Empty or
  partial traces are inconclusive, never proof a phase did not occur.
- The extra API connection and log traffic may affect timing. Do not compare
  latency directly to an uninstrumented run.
- Slot state is a reported snapshot, not independently acknowledged teardown.
  A target allocation, unavailable shared API or connections still in progress
  causes cleanup review; the trace does not silently retry or fall back.
- No fallback or default proxy routing has been accepted for production.

## Authorised live run procedure

1. Confirm the app is closed, robot stationary and other robot clients idle.
2. Save original options, pause polling and confirm previous cleanup/cooldown.
3. Select the exact ESPHome entry and invoke this action once with all
   confirmations. Do not weaken guards to force a test.
4. Read `last_result.proxy_trace` and `transport_diagnostics` together. Inspect
   actual backend, anonymous route, writes, events and both cleanup paths.
5. If cleanup is uncertain, stop and investigate. Do not reload to clear
   suspension or issue a second query.
6. After confirmed cleanup and the full cooldown, restore the saved direct-local
   polling options and verify an automatic four-query cycle.
