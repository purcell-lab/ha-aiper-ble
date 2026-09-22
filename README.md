# Aiper BLE for Home Assistant

Experimental local Bluetooth telemetry and guarded cleaning actions for the Aiper Surfer S1, with Home
Assistant-managed adapters and active Bluetooth proxies. This is an independent
community integration, not an official Aiper product.

**Version 0.10.0.** The integration domain remains `aiper_ble` for
compatibility with existing installations.

Adds an INFO-derived operating-state enum sensor and explicit `start_cleaning`
and `stop_cleaning` BLE actions. Actions require per-call safety confirmation,
share the polling lock, never retry and verify a separate INFO readback.
These commands are traced from Android 3.6.1 and were live-validated on the
owner's robot on 22 September 2026 (AEST): one stop and one start, each
acknowledged and verified by INFO readback, with motion confirmed from signal
drift. They are never sent automatically by polling. See
[S1 states and controls](docs/s1_states_and_controls.md) before use.

The consolidated [reverse-engineered endpoint reference](docs/reverse_engineered_endpoints.md)
maps BLE UUIDs, fixed telemetry/control commands, reply validation, state
enumerations and HA actions to their APK evidence and live-validation status.

Direct-local polling now requests only `S1_INFO` (temperature) and `INFO`
(battery state of charge). It no longer connects for `OpInfo` or `WARN`.
This halves the connections per local cycle without changing the polling
interval, cleanup checks, atomic publication or transport selection. See
[SOC and temperature polling](docs/soc_temperature_polling.md).

The opt-in [native ESPHome proxy trace](docs/native_proxy_trace.md) remains
available for diagnostics; it does not enable proxy fallback.

Version 0.9.6 added an explicit, version-gated same-local-radio diagnostic action.
It does not change production transport selection, retry policy or polling
options. It retains v0.9.5's passive transport diagnostics and retirement of
14 speculative OpInfo entities. See
[same-radio diagnostics](docs/same_radio_diagnostic.md),
[transport diagnostics](docs/transport_diagnostics.md) and
[entity retirement](docs/entity_retirement.md) before upgrading.

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

For issue #7, an explicit
[same-local-radio diagnostic](docs/same_radio_diagnostic.md) compares direct
BlueZ with HA-managed local Bleak without
disabling proxies or changing the production polling transport.

- Direct local BlueZ polls only fixed `S1_INFO` and `INFO` requests for temperature
  and SOC. The HA Bluetooth path retains its four-query cycle (`S1_INFO`,
  `OpInfo`, `INFO`, `WARN`); no transport is switched automatically.
- Groups entities under one **Aiper Surfer S1 (BLE)** device that carries the
  robot's Bluetooth address as a registry connection, so the HA device page
  shows its Bluetooth section (last seen via which adapter or proxy, signal
  strength). Downloaded integration diagnostics still redact the address.
  Ten entities are enabled by default: temperature, battery, decoded operating state, raw operating status/mode, raw warning code, raw solar status,
  last successful poll, polling status and manual discovery result. In direct-local
  mode, warning and OpInfo-only entities are unavailable because those queries
  are no longer polled; entity registry entries are not deleted.
- Keeps four optional diagnostics disabled by default: raw temperature, time
  zone, Wi-Fi RSSI and network name. Retires 14 speculative OpInfo/Machine
  entities that were not returned by the S1.
- Provides a guarded `aiper_ble.poll_now` action.
- Provides independent `query_s1_info`, `query_opinfo`, `query_info` and
  `query_warn` actions for [guarded bisection](docs/isolated_query_bisection.md)
  while recurring polling is disabled. These return diagnostics, not partial
  sensor refreshes.
- Reports repeated manual failures to the status entity and exposes cached,
  privacy-limited transport-stage diagnostics, including bounded per-connect
  [direct-local connect observations](docs/local_connect_observability.md).
- Keeps the last-successful-poll timestamp visible while polling fails, so the
  age of the readings stays readable; measured values still become unavailable.
- Retains local-adapter-only discovery, read, query and listen diagnostics.

Temperature sensor location is unverified. Battery comes only from INFO field 2,
using the app's 0-100 battery-level scale, not from speculative `bat`/`cap` fields.
Status and special-mode predicates are app-derived; other mode values, solar,
warning codes and Wi-Fi RSSI sentinels remain uninterpreted.
No pairing, provisioning, arbitrary commands or cloud API is implemented.
The only control setters are explicit S1 start/standby actions.
See the [DP validation matrix](docs/info_dp_validation.md) and
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
After a Home Assistant restart the first cycle waits for startup to finish
plus a short settle period so the Bluetooth proxies can reconnect first.
Failures back off; unsafe cleanup or unsupported security/protocol evidence
suspends polling. On the HA-managed route a confirmed disconnect releases the
client's own notification subscription, so a failed unsubscribe on a link that
has already dropped fails that cycle with backoff instead of suspending. An
unconfirmed disconnect still suspends. Review the cause before reloading or
restarting, which clears volatile suspension. Remote proxies cannot expose all local BlueZ ownership
metadata, so exclusive access remains an operator responsibility.

Read the [full safety and diagnostics guide](docs/aiper_ble.md)
before enabling polling.

## Install

### Manual installation

1. Copy `custom_components/aiper_ble/` from this repository into
   your HA `/config/custom_components/` directory.
2. Restart Home Assistant once.
3. Go to **Settings > Devices & services > Add integration** and select
   **Aiper BLE**.
4. Select the robot from HA's cached Bluetooth discovery.
5. Open the integration's configuration options if you wish to enable polling.
   Read and confirm the exclusive-access and legacy-protocol options.

The repository includes `hacs.json` and the single-integration directory layout
for use as a HACS custom repository. It is not a default HACS listing.

### Migrating from the `aiper_ble_diagnostics` domain

Version 0.12.0 renamed the integration domain from `aiper_ble_diagnostics` to
`aiper_ble`. Home Assistant cannot move a config entry between domains, and a
HACS repository can hold only one integration directory, so there is no
in-place migration:

1. Update through HACS (or copy the new `custom_components/aiper_ble/`
   directory) and delete the old `custom_components/aiper_ble_diagnostics/`
   directory if it remains. Restart Home Assistant.
2. Remove the old **Aiper BLE Diagnostics** entry under Settings > Devices &
   services. Its entities are removed with it.
3. Add **Aiper BLE** and select the robot, then set the options again
   (polling, exclusive-access confirmation, transport, vacuum controls).

Entity IDs are fixed by the integration (`sensor.aiper_ble_*`,
`vacuum.aiper_surfer_s1`), so recorder history continues under the same IDs.
Entity customisations such as areas, custom names or renamed IDs need to be
re-applied. Automations that call `aiper_ble_diagnostics.*` actions must be
changed to `aiper_ble.*`.
See [HACS integration repository requirements](https://www.hacs.xyz/docs/publish/integration/).
HACS validation and a default-directory submission have not been completed.

### Removing the integration

Remove the **Aiper BLE** entry under Settings > Devices & services. The
device and all of its entities are removed with it; recorder history for the
entity IDs stays until Home Assistant's normal purge. Nothing is written to the
robot on removal. To remove the code as well, uninstall the repository in HACS
(or delete `custom_components/aiper_ble/`) and restart Home Assistant.

### Existing installations from ha-config

Do not remove and re-add the integration. Keep the existing domain, config entry,
options and entity registry. Install this repository's component over the same
directory and restart once after reviewing the update. Retained entity unique
IDs and default IDs are unchanged, and user-renamed IDs take precedence.

Version 0.9.0 adds `AT+INFO?` and `AT+WARN?` to enabled polling, including the first poll after
restart. Review this expanded query scope before deployment. A once-only registry
migration hides previously enabled optional diagnostics without disabling them,
deleting their IDs or changing history. User-hidden, user-disabled and custom-named
entities are left alone. Unhide any retained diagnostic in entity settings after
migration if needed; later reloads do not re-hide it.

Config-entry minor version 3 removes exactly 14 retired speculative sensor
registry entries, including renamed or disabled copies, and stops creating them.
It leaves 13 entities: nine enabled by default and four optional diagnostics.
Review references before deploying this breaking entity cleanup; see the
[retirement list and migration guide](docs/entity_retirement.md).
No recorder-history purge, device removal or transport change is performed.

Only one update mechanism should own the component directory. Before switching
from a configuration-repository deployment to HACS, stop that deployment from
overwriting this directory. Publishing this repository does not itself change
any live HA installation or remove the old repository's copy.

## Supported devices

Only the Aiper Surfer S1 (solar pool skimmer) is supported, and only over the
legacy Bluetooth LE protocol it exposes. Setup accepts robots whose Bluetooth
name starts with `Aiper-Surfer S1-` or `Aiper_Surfer S1_`. Robots that
advertise the newer key-exchange protocol are vetoed before any write. Other
Aiper models use different command sets and are not accepted.

## Supported functions

| Function | Entity or action |
| --- | --- |
| Battery, temperature, raw status, mode, warning and solar codes, INFO state | `sensor.aiper_ble_*` |
| Water temperature while working | `sensor.aiper_ble_water_temperature` |
| Vacuum state and start/stop | `vacuum.aiper_surfer_s1` |
| Buttons for poll now, start and stop | `button.aiper_ble_poll_now`, `button.aiper_ble_start_cleaning`, `button.aiper_ble_stop_cleaning` |
| Signal strength of the last cycle's route | `sensor.aiper_ble_signal_strength` |
| Start and stop with per-call confirmation | `aiper_ble.start_cleaning`, `aiper_ble.stop_cleaning` |
| Immediate poll | `aiper_ble.poll_now` |
| Isolated single-query diagnostics and transport experiments | `aiper_ble.query_*`, `preflight`, `discover`, `read_once`, `listen_once`, `protocol_preview`, `query_once` |
| Polling health, suspension and repair | `sensor.aiper_ble_polling_status`, Settings > Repairs |

## How data is updated

Polling is local push-free polling over Bluetooth LE: every cycle opens one
short connection per fixed query (S1_INFO, OpInfo, INFO, WARN over Home
Assistant's Bluetooth routes; S1_INFO and INFO over the direct-local adapter),
verifies each reply's CRC, and publishes all values at once. The interval is
300 s by default and counts from the end of the previous cycle. A failed cycle
doubles the interval up to an hour and makes measured values unavailable until
the next success. After a Home Assistant restart the first cycle waits for
startup plus 45 s so Bluetooth proxies can reconnect. Controls never run
automatically.

## Known limitations

- No pause or resume: the only known setter is stop-to-standby.
- No in-water field exists on the BLE interface; the operating state is the
  only water-related signal.
- The solar code has never read anything but 0; its meaning is unverified.
- The temperature probe's location is unverified.
- Controls are live-validated on one robot and one firmware only.
- The robot has no dock. "Docked" on the vacuum means on its wall charger.
- Proxy placement matters: a robot at the far side of a pool from the proxy
  drops to about -95 dBm and polls fail until it comes closer.
- The app and this integration cannot share the robot; keep the app closed.

## Use cases

- Battery and water temperature on a dashboard without opening the app.
- Notify when the vacuum reports `error` (a non-zero warning code).
- Start a clean from an automation once nobody is in the pool area, and stop
  it when the robot's battery drops below a threshold.
- Track cleaning sessions from the operating state and the minute counter.

## Examples

```yaml
# Stop the robot when a presence sensor sees someone at the pool.
automation:
  - alias: Aiper stop when pool occupied
    triggers:
      - trigger: state
        entity_id: binary_sensor.pool_area_occupancy
        to: "on"
    conditions:
      - condition: state
        entity_id: vacuum.aiper_surfer_s1
        state: cleaning
    actions:
      - action: vacuum.stop
        target:
          entity_id: vacuum.aiper_surfer_s1
```

```yaml
# Notify on a fault code.
automation:
  - alias: Aiper fault
    triggers:
      - trigger: state
        entity_id: vacuum.aiper_surfer_s1
        to: error
    actions:
      - action: notify.notify
        data:
          message: >
            Aiper reports warning code
            {{ state_attr('vacuum.aiper_surfer_s1', 'warning_code_raw') }}.
```

```yaml
# Tile card with the vacuum's start and stop buttons.
type: tile
entity: vacuum.aiper_surfer_s1
features:
  - type: vacuum-commands
    commands:
      - start_pause
      - stop
```

## Configuration options

Open the integration's **Configure** dialog to change these. Every change
reloads the entry and runs one poll if polling is enabled.

| Option | Default | Effect |
| --- | --- | --- |
| Enable recurring BLE status queries | off | Turns on the polling cycle (S1_INFO, OpInfo, INFO, WARN over the HA route; S1_INFO and INFO over the direct-local adapter). |
| Successful poll interval in seconds | 300 | Time from the end of one successful cycle to the start of the next, 300 to 3600. Failures back off from this value. |
| I authorise recurring queries and will keep other BLE clients idle | off | Required with polling. Confirms that the app and other BLE clients stay closed while polling runs. |
| Allow the legacy S1 protocol when advertisement evidence is missing | off | Lets polling and controls proceed when the cached advertisement lacks the company-zero data that proves the legacy protocol. Never overrides positive evidence of the newer protocol. |
| Use saved local adapter directly (no proxies or Bleak) | off | Uses the BlueZ adapter saved at setup instead of HA-managed routes and proxies. Requires local adapter metadata in the entry. |
| Enable vacuum entity start/stop | off | Lets the vacuum entity's buttons send the guarded start and stop commands without a per-call confirmation. Turn on only while the robot is in the water, unplugged, nobody is in the pool and the app is closed. |

## Water temperature

`sensor.aiper_ble_water_temperature` holds the temperature read in the most
recent polling cycle in which the robot reported working, when it is certainly
in the water. The ordinary temperature sensor updates every cycle, including on
the charger. This one keeps its value between cleaning sessions and across
restarts, with `measured_at` in local time. The probe's physical location is
still unverified, so treat it as "temperature seen by the robot while
cleaning" rather than a calibrated pool thermometer.

## Vacuum entity

`vacuum.aiper_surfer_s1` shows the robot's INFO-derived state as a Home
Assistant vacuum: `cleaning` for working or sunward, `idle` for standby, and
`docked` while on its wall charger (this robot has no dock; Home Assistant has
no charging activity), and `error` whenever the raw warning code is non-zero.
It is unavailable until a verified polling cycle, or a verified control
readback, has produced a state. It never guesses. Its attributes carry the
battery, temperature, raw warning code, the INFO minute counter (raw, observed
to count minutes since power-on), the last successful poll, polling status and
failure count, the backend and signal of the last query, and the last control's
action and outcome.

Its start and stop buttons are disabled until the option **Enable vacuum
entity start/stop** is turned on in the integration's options. That option is
the standing equivalent of the per-call confirmations the `start_cleaning` and
`stop_cleaning` actions take: turn it on only while the robot is in the water,
unplugged, nobody is in the pool and the app is closed, and turn it off
otherwise. With it on, a button press runs exactly the same guarded sequence as
the action (preflight, one setter, one INFO readback, no retry) and the entity
shows the readback state until a full polling cycle confirms it. That cycle
runs about 20 seconds after the control finishes, instead of a whole polling
interval later; the normal interval resumes from there. An unconfirmed command
raises an error and leaves the entity unavailable until the next poll.

## Buttons

`button.aiper_ble_poll_now` runs one full polling cycle at once, exactly like
the `poll_now` action, and reports the cycle's error if it fails.
`button.aiper_ble_start_cleaning` and `button.aiper_ble_stop_cleaning` run the
same guarded control sequence as the vacuum entity and are refused until
**Enable vacuum entity start/stop** is turned on in the options. The buttons
are unavailable while polling is disabled or suspended.

## Signal strength

`sensor.aiper_ble_signal_strength` is the RSSI, in dBm, of the robot's
advertisement as seen by the proxy or adapter the last cycle connected
through, read just before that connection. It is the observer's reading, not
the robot's, so it changes with the route Home Assistant selects. It is
unavailable after a cycle that selected no route, such as when the robot is
off. The `backend` and `scanner_type` attributes say which kind of route it
was.

## Poll now

```yaml
action: aiper_ble.poll_now
data:
  entry_id: YOUR_AIPER_BLE_CONFIG_ENTRY_ID
  confirm_app_closed: true
```

Choose the config entry with the selector in Developer Tools > Actions.
Polling must already be enabled and authorised. The action runs immediately:
it clears offline failure backoff and does not wait for the configured normal
interval. It cannot bypass an active query, suspension or identity checks; a
request while a cycle is running is rejected rather than queued. The next
automatic cycle is scheduled one normal interval after the manual one finishes.

With a response requested, the action returns `status` and
`last_successful_poll`. It updates the same sensors as automatic polling.

## Troubleshooting

- **Unavailable optional sensors:** The robot did not return those fields.
  Unavailable does not mean zero.
- **Failed polling with a visible timestamp:** Battery and temperature go
  unavailable on the first failed cycle, while last successful poll keeps showing
  when the readings were last fresh. Diagnostics still report the telemetry as
  not current.
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
ruff check --config ruff-aiper.toml custom_components/aiper_ble tests/aiper_ble
ruff format --config ruff-aiper.toml --check custom_components/aiper_ble tests/aiper_ble
python -m pytest -q tests/aiper_ble --disable-socket --allow-unix-socket --asyncio-mode=auto
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
