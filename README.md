# Aiper BLE for Home Assistant

Experimental local Bluetooth telemetry for the Aiper Surfer S1, with Home
Assistant-managed adapters and active Bluetooth proxies. This is an independent
community integration, not an official Aiper product.

**Version 0.8.1.** The integration domain remains `aiper_ble_diagnostics` for
compatibility with existing installations.

## What it does

- Polls fixed `S1_INFO` and `OpInfo` status requests using HA's Bluetooth framework.
- Groups 23 sensor entities under one **Aiper Surfer S1 (BLE)** device.
- Exposes temperature, raw temperature, time zone, raw solar status, and supported
  OpInfo fields. Optional fields remain unavailable when the robot omits them.
- Provides a guarded `aiper_ble_diagnostics.poll_now` action.
- Reports repeated manual failures to the status entity and exposes cached,
  privacy-limited transport-stage diagnostics.
- Retains local-adapter-only discovery, read, query and listen diagnostics.

Temperature sensor location is unverified. Raw battery fields are not verified
SOC percentages. Solar codes and Wi-Fi RSSI sentinels are not interpreted.
No pairing, provisioning, cleaning controls, arbitrary commands, cloud API or
additional `INFO` query is implemented.

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

One cycle contains two bounded connections, one for each fixed request.
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

## Development

Use Python 3.14:

```sh
python -m pip install -r requirements-aiper-ble-test.txt
ruff check --config ruff-aiper.toml custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
ruff format --config ruff-aiper.toml --check custom_components/aiper_ble_diagnostics tests/aiper_ble_diagnostics
python -m pytest -q tests/aiper_ble_diagnostics --disable-socket --allow-unix-socket --asyncio-mode=auto
```

The extracted v0.8.0 implementation has 416 offline tests. Tests use fake Bluetooth
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
