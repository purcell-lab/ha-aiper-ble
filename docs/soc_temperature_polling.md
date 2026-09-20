# Direct-local polling for SOC and temperature

Version 0.9.11 reduces direct-local BlueZ polling to two fixed read-only queries:

| Order | Query | Required reading |
| --- | --- | --- |
| 1 | S1_INFO | Temperature, with the existing raw-to-Celsius conversion |
| 2 | INFO | Battery SOC from validated field 2, on a 0-100% scale |

`OpInfo` and `WARN` are no longer part of automatic direct-local polling or its
`poll_now` action. The HA/Bleak polling path and all explicit isolated diagnostic
actions are unchanged. No diagnostic action is invoked automatically.

This change mitigates the extra connection exposure observed in
[direct-local polling issue #16](https://github.com/purcell-lab/ha-aiper-ble/issues/16).
It does not claim to fix the underlying local connection failure or the separate
[ESPHome proxy issue #7](https://github.com/purcell-lab/ha-aiper-ble/issues/7).

## Safety and publication

The configured interval remains unchanged (300 seconds in the current
installation). Each request still uses a separate bounded connection, with no
retry or transport fallback. Both responses must pass CRC and protocol checks,
and both connections must have confirmed notification stop and disconnection,
before the coordinator publishes a new cycle.

Failures retain the existing backoff and suspension rules. No cleanup-timeout
recovery experiment is included. The shared cooldown and exclusive-operation
guard also apply to manual `poll_now`.

Battery and temperature keep their existing entity IDs. Other validated scalar
fields already contained in S1_INFO and INFO remain available without extra
queries. WARN/OpInfo-only values are not carried forward as fresh telemetry;
their existing entities become unavailable rather than being deleted.

Cached diagnostics and the polling-status entity expose `configured_queries`
so the two-query cycle can be verified without initiating Bluetooth activity.

## Validation and deployment

Offline tests cover exact two-query order and wire requests, battery/temperature
publication, five-field INFO responses, first/second-query failure, cleanup
suspension, atomic publication, cooldown, and unchanged isolated actions.
Existing HA-path tests retain the four-query expectation.

Installing this code requires an HA restart to load it. A successful deployment
must be verified from a normal automatic cycle and cached diagnostics; this
change does not authorise further manual BLE experiments. Until deployment and
verification are recorded, no live success is claimed.
