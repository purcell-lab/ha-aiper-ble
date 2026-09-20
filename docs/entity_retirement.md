# Retiring speculative OpInfo entities

This is an entity-model cleanup, separate from transport diagnostics and the
proxy investigation. The recorded S1 response does not contain the speculative
direct OpInfo or nested Machine fields. Battery, operating status and mode
come from the separate INFO query; warnings come from WARN.
See the [validation matrix](info_dp_validation.md) for the evidence and limits.

## Exact removal scope

Config-entry minor version 3 stops registering and removes these 14 sensor
unique-ID suffixes:

| Retired suffix | Previous meaning |
| --- | --- |
| `opinfo_bat_raw` | Speculative OpInfo battery |
| `opinfo_status_raw` | Speculative OpInfo status |
| `opinfo_link_raw` | Speculative OpInfo link |
| `opinfo_machine_cap_raw` | Speculative nested battery capacity |
| `opinfo_machine_mode_raw` | Speculative nested mode |
| `opinfo_machine_solar_status_raw` | Speculative nested solar state |
| `opinfo_machine_status_raw` | Speculative nested status |
| `opinfo_machine_temp_raw` | Speculative nested temperature |
| `opinfo_machine_warn_raw` | Speculative nested warning |
| `opinfo_machine_warn_code_raw` | Speculative nested warning code |
| `opinfo_machine_in_water_raw` | Speculative nested in-water state |
| `opinfo_machine_link_raw` | Speculative nested link |
| `opinfo_machine_light_raw` | Speculative nested light |
| `opinfo_machine_visual_raw` | Speculative nested visual field |

Matching uses the exact `<config_entry_id>_<suffix>` unique ID, sensor domain,
integration platform and owning config entry. It does not match entity-ID
prefixes, current availability or entity names. Renamed, hidden and disabled
copies of these exact retired sensors are removed too. Unrelated integrations,
other config entries and similarly named entities are untouched.

## What remains

Thirteen entities remain: nine default-enabled operational/health sensors and
four optional diagnostics. Temperature, INFO battery/status/mode, WARN code,
solar state, last successful poll, polling status and manual discovery result
keep their existing IDs. Raw temperature, time zone, Wi-Fi RSSI and Wi-Fi network
remain disabled by default for fresh installations, with existing user choices
preserved.

The optional network-name sensor is retained because it has app-derived
support, although absent from the observed reply. RSSI is retained as observed
raw data without interpreting its sentinel. Unavailable is not, by itself, a
reason to retire an entity.

Bounded parsing of historical candidate fields remains for investigation and
isolated query results; those values are no longer registered as entities.
This change neither adds nor removes BLE queries and does not change transport,
cooldown, polling, CRC validation or cleanup.

## Upgrade and rollback considerations

This is a breaking removal of the listed entities, not just another hiding
migration. Before approving deployment, review dashboards, automations, scripts
and templates that might reference any retired entity, including custom names.
The PR does not establish that every live user reference is absent.

The migration runs once when HA loads an older config entry with this code,
including direct upgrades from minor version 1. Reloads do not recreate retired
entities. It does not remove the HA device or config entry, reset options,
rename retained entities or invoke recorder-history purging. Removed entity
customisations are not backed up by this migration; take a Home Assistant backup
before deployment. Historical records may remain subject to HA retention, but
normal access through removed entities is not guaranteed.

Restoring older code alone does not restore deleted registry customisations.
Use a pre-deployment backup for a complete rollback, and account for the config
entry minor-version change. This PR itself performs no live registry deletion,
deployment, restart or BLE test.
