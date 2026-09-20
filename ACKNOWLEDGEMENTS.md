# Acknowledgements

This independent integration benefited from the following community projects.
Their roles are distinguished below so that a protocol reference is not mistaken
for code authorship, support for this particular robot, or endorsement.

## Protocol and integration references

- **[knobunc/aiper](https://github.com/knobunc/aiper)** provided the initial
  [Aiper pool robot BLE specification](https://github.com/knobunc/aiper/blob/20b520078bf10392fea3f0fa2678092918f2cd48/docs/AIPER_POOL_ROBOT_BLE_SPEC.md).
  Its protocol map guided service discovery and the first guarded harness.
  Independent inspection of Android app 3.6.1 and subsequent Surfer S1 testing
  refined the legacy checksum seed, empty-data serialization and S1 response
  handling. That reference is not evidence that every Aiper model or firmware
  implements the same protocol.
- **[kmich/ha-aiper](https://github.com/kmich/ha-aiper)** provided an existing
  community Aiper Home Assistant integration for comparison during investigation
  of cloud and local connectivity. This repository is a separate experimental
  BLE implementation, not that project's BLE backend or a replacement for it.
- **[bassrock/hass-wybot](https://github.com/bassrock/hass-wybot)** was used in
  the earlier comparison of pool-robot diagnostics, telemetry and BLE/cloud
  integration approaches. WYBOT wire commands are not treated as Aiper commands.
- **[purcell-lab/ha-config](https://github.com/purcell-lab/ha-config)** was the
  original development and controlled-deployment repository. This standalone
  repository was extracted from the component and tests prepared in
  [PR #242](https://github.com/purcell-lab/ha-config/pull/242), without copying
  household configuration, runtime captures or the original Git history.

## Platform, transport and development tools

- **[Home Assistant Core](https://github.com/home-assistant/core)** supplies
  the integration, config-entry, entity, device-registry, coordinator and shared
  Bluetooth APIs on which this component depends.
- **[Bleak](https://github.com/hbldh/bleak)** and
  **[bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector)**
  provide the asynchronous Bluetooth client and connection infrastructure used
  through Home Assistant's Bluetooth framework. This component deliberately
  restricts each exchange to one actual connection attempt.
- **[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)**
  provides the Home Assistant test fixtures used by the offline test suite.
- **[JADX](https://github.com/skylot/jadx)** was used for offline static inspection
  of the Android app, including fallback instruction output where normal
  decompilation was incomplete. No APK or decompiled application source is
  redistributed here.
- **[Gitleaks](https://github.com/gitleaks/gitleaks)** is used to scan the public
  source tree and Git history for accidentally committed secrets.

See [the discovered BLE specification](docs/AIPER_POOL_ROBOT_BLE_SPEC.md) for
the evidence boundaries and [the APK inspection note](docs/aiper_ble_apk_361_evidence.md)
for package provenance. Dependencies and referenced projects retain their own
licences; acknowledgement does not relicense their work or imply endorsement by
their maintainers or Aiper.
