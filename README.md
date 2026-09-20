# Aiper BLE for Home Assistant

Experimental local Bluetooth telemetry for the Aiper Surfer S1, with Home
Assistant-managed adapters and active Bluetooth proxies. This is an independent
community integration, not an official Aiper product.

**Version 0.9.4.** The integration domain remains `aiper_ble_diagnostics` for
compatibility with existing installations.

The INFO parser now accepts the CRC-verified five-field reply observed on the
test S1, as well as the app-derived three-field form. Only the documented first
three positions become sensors; the final two remain uninterpreted. Diagnostics
retain a bounded trace of each query in the last cycle, without raw responses
or device identifiers.

### Local transport rollback

Enable **Use saved local adapter directly (no proxies or Bleak)** in integration
options to restore the original direct-BlueZ connection path. This requires an
entry with saved local adapter metadata and applies to polling and all four
isolated query actions. It never falls back to a proxy. The existing HA/Bleak path
remains available when this option is off; existing entries are not silently
changed. Both paths retain the coordinator's CRC verification, shared cooldown,
exclusive-operation guard, cleanup checks and atomic sensor publication.

At 12:49 AEST on 20 September 2026, a single direct-BlueZ OpInfo query completed
in 5.26 seconds after the HA/proxy path had timed out at connection establishment.
This changes both radio selection and connection implementation, so it narrows
the issue to the connection path but does not isolate Bleak versus ESPHome.
See [the rollback record](docs/local_transport_rollback.md).

## What it does

- Polls fixed `S1_INFO`, `OpInfo`, `INFO` and `WARN` status requests using the
  selected local BlueZ or HA Bluetooth transport. All four have live response
  evidence; raw code meanings and comparison against the app remain unverified.
- Groups entities under one **Aiper Surfer S1 (BLE)** device, with nine enabled
  by default: temperature, battery, raw operating status/mode, raw warning code, raw solar status,
  last successful poll, polling status and manual discovery result.
- Keeps 18 optional/raw diagnostics disabled by default on new installations.
  Missing fields remain unavailable; speculative OpInfo/Machine fields are not
  presented as confirmed capabilities.
- Provides a guarded `aiper_ble_diagnostics.poll_now` action.
- Provides independent `query_s1_info`, `query_opinfo`, `query_info` and
  `query_warn` actions for [guarded bisection](docs/isolated_query_bisection.md)
  while recurring polling is disabled. These return diagnostics, not partial
  sensor refreshes.
- Reports repeated manual failures to the status entity and exposes cached,
  privacy-limited transport-stage diagnostics.
- Retains local-adapter-only discovery, read, query and listen diagnostics.

Temperature sensor location is unverified. Battery comes only from INFO field 2,
using the app's 0-100 battery-level scale, not from speculative `bat`/`cap` fields.
Status/mode/solar/warning codes and Wi-Fi RSSI sentinels are not interpreted.
No pairing, provisioning, cleaning controls, arbitrary commands or cloud API is
implemented. See the [DP validation matrix](docs/info_dp_validation.md) and
[additional APK query assessment](docs/apk_query_catalog.md).

## Requirements and safety

- Home Assistant **2026.9.3 or later**; the offline test environment uses 2026.9.3.
  Later releases are not automatically guaranteed compatible.
- The Bluetooth integration and a connectable local adapter or active proxy that
  can see the robot.
- A legacy-protocol Surfer S1. Other models and ECDH devices are unsupported.
- Keep the Aiper app closed and other BLE clients idle while polling is enabled.

Polling is disabled by default. Enabling it explicitly authorises recurring
status writes and notification subscriptions, including after a restart.
This is not write-free passive reception. It never sends robot control commands.

One cycle contains four bounded connections, one for each fixed request.
Readings publish only after matching, CRC-verified replies and confirmed cleanup.
Failures back off; unsafe cleanup or unsupported security/protocol evidence
suspends polling. Review the cause before reloading or restarting, which clears
volatile suspension. Remote proxies cannot expose all local BlueZ ownership
metadata, so exclusive access remains an operator responsibility.

Read the [full safety and diagnostics guide](docs/aiper_ble_diagnostics.md)
before enabling polling.

## Install

### Manual installation

1. Copy `custom_components/aiper_ble_diagnostics/` from this repository into
   your HA `/config/custom_components/` directory.
2. Restart Home Assistant once.
3. Go to **Settings > Devices & services > Add integration** and select
   **Aiper BLE Diagnostics**.
4. Select the robot from HA's cached Bluetooth discovery.
5. Open the integration's configuration options if you wish to enable polling.
   Read and confirm the exclusive-access and legacy-protocol options.

The repository includes `hacs.json` and the single-integration directory layout
for use as a HACS custom repository. It is not a default HACS listing.
See [HACS integration repository requirements](https://www.hacs.xyz/docs/publish/integration/).
HACS validation and a default-directory submission have not been completed.

### Existing installations from ha-config

Do not remove and re-add the integration. Keep the existing domain, config entry,
options and entity registry. Install this repository's component over the same
directory and restart once after reviewing the update. Entity unique IDs and
default IDs are retained, and user-renamed IDs take precedence.

Version 0.9.0 adds `AT+INFO?` and `AT+WARN?` to enabled polling, including the first poll after
restart. Review this expanded query scope before deployment. A once-only registry
migration hides previously enabled optional diagnostics without disabling them,
deleting their IDs or changing history. User-hidden, user-disabled and custom-named
entities are left alone. Unhide any retained diagnostic in entity settings after
migration if needed; later reloads do not re-hide it.

Only one update mechanism should own the component directory. Before switching
from a configuration-repository deployment to HACS, stop that deployment from
overwriting this directory. Publishing this repository does not itself change
any live HA installation or remove the old repository's copy.

## Poll now

```yaml
action: aiper_ble_diagnostics.poll_now
data:
  entry_id: YOUR_AIPER_BLE_CONFIG_ENTRY_ID
  confirm_app_closed: true
```

Choose the config entry with the selector in Developer Tools > Actions.
Polling must already be enabled and authorised. The action can shorten offline
failure backoff after the configured normal interval has elapsed, but cannot
bypass its minimum 300-second gap, an active query, suspension or identity checks.
An early request raises a cooldown error with seconds remaining.

With a response requested, the action returns `status` and
`last_successful_poll`. It updates the same sensors as automatic polling.

## Troubleshooting

- **Unavailable optional sensors:** The robot did not return those fields.
  Unavailable does not mean zero.
- **`identity_changed`:** A selectable route lacks the exact configured robot
  name/address. Nameless proxy discovery records can trigger this guard even if
  another adapter has the expected name. The integration fails closed; `poll_now`
  does not override the guard.
- **`no_connectable_route`:** HA has no eligible cached route. Check proxy
  connectivity and signal coverage.
- **`bluetooth_transport_error`:** A bounded backend operation failed. Check HA
  Bluetooth diagnostics and ensure the app and other clients are idle.
- **Suspended polling:** Review diagnostics and connection ownership before
  reloading. Do not repeatedly restart to bypass a safety veto.

Download diagnostics through the integration menu. Review all files before
sharing them publicly, including any HA-generated wrapper metadata.
The [passive transport diagnostics guide](docs/transport_diagnostics.md)
explains phase timings, anonymous route metadata, timeout evidence and the
limits of selected-backend attribution.

## Development

Use Python 3.14:

```sh
python -m pip install -r requirements-aiper-ble-test.txt
ruff check --config ruff-aiper.toml custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
ruff format --config ruff-aiper.toml --check custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
python -m pytest -q tests/aiper_ble_diagnostics --disable-socket --allow-unix-socket --asyncio-mode=auto
```

Tests use fake Bluetooth
transports and cannot establish real-world radio reachability or prove every
firmware variant compatible.

## Protocol provenance and project status

This repository is a clean source-only extraction of the integration developed
in `purcell-lab/ha-config`. No HA configuration, credentials, APKs, decompiled
application source, production diagnostics or original repository history are
included.

Protocol work is informed by the
[community BLE specification](https://github.com/knobunc/aiper/blob/main/docs/AIPER_POOL_ROBOT_BLE_SPEC.md)
and independent static inspection documented in
[APK evidence](docs/aiper_ble_apk_361_evidence.md).
The current [discovered BLE specification](docs/AIPER_POOL_ROBOT_BLE_SPEC.md)
records working frames, checksum rules, observed replies, sensor fields and
remaining uncertainties. [Acknowledgements](ACKNOWLEDGEMENTS.md) credit the
repositories and tools used during development.
See [sensor coverage, proxy routing and battery limits](docs/aiper_ble_proxy_polling.md).

The separate [ha-aiper integration](https://github.com/kmich/ha-aiper) is not
replaced or automatically merged into this integration's HA device.

No new licence grant is added by this extraction; a standalone project licence
has not yet been selected.
