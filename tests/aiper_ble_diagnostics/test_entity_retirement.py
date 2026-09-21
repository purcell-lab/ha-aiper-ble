"""Exact, once-only retirement without deleting useful or unrelated entities."""

from dataclasses import asdict

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble_diagnostics import async_migrate_entry
from custom_components.aiper_ble_diagnostics.config_flow import ConfigFlow
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.datapoints import (
    DEFAULT_ENABLED,
    RETIRED_ENTITY_KEYS,
    SENSOR_NAMES,
)

from .helpers import TARGET


def test_explicit_retirement_scope():
    assert len(RETIRED_ENTITY_KEYS) == 14
    assert not RETIRED_ENTITY_KEYS.intersection(SENSOR_NAMES)
    assert len(SENSOR_NAMES) == 11
    assert set(SENSOR_NAMES) - DEFAULT_ENABLED == {
        "temperature_raw",
        "s1_timezone",
        "wifi_rssi_raw",
        "wifi_name",
    }
    assert ConfigFlow.MINOR_VERSION == 3


@pytest.mark.parametrize("minor_version", [1, 2])
async def test_retire_exact_records_preserve_other_entries_and_reload(
    hass, minor_version
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), version=1, minor_version=minor_version
    )
    other = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), disabled_by=ConfigEntryDisabler.USER
    )
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    retired = []
    for index, key in enumerate(sorted(RETIRED_ENTITY_KEYS)):
        item = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=f"custom_retired_{index}",
        )
        registry.async_update_entity(
            item.entity_id,
            name=f"Custom display {index}" if index % 2 else None,
            hidden_by=er.RegistryEntryHider.USER if index % 2 else None,
            disabled_by=er.RegistryEntryDisabler.USER if index % 3 else None,
        )
        retired.append(item.entity_id)
    keep = []
    for key in SENSOR_NAMES:
        item = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=f"custom_retained_{key}",
        )
        # Customised retained sensors must not be hidden by the older migration.
        item = registry.async_update_entity(
            item.entity_id,
            name=f"My {key}",
            hidden_by=er.RegistryEntryHider.USER,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        keep.append(item)
    retired_uid = f"{entry.entry_id}_opinfo_bat_raw"
    for domain, platform, uid, owner in (
        ("sensor", DOMAIN, f"{other.entry_id}_opinfo_bat_raw", other),
        ("sensor", "other_integration", retired_uid, entry),
        ("binary_sensor", DOMAIN, retired_uid, entry),
        ("sensor", DOMAIN, retired_uid + "_not_retired", entry),
    ):
        keep.append(
            registry.async_get_or_create(
                domain,
                platform,
                uid,
                config_entry=owner,
            )
        )
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 3
    assert all(registry.async_get(entity_id) is None for entity_id in retired)
    assert all(registry.async_get(item.entity_id) == item for item in keep)
    assert await async_migrate_entry(hass, entry)  # Idempotent.
    assert all(registry.async_get(item.entity_id) == item for item in keep)
    # Setup/reload does not re-create retired IDs or perform any BLE queries.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) is None for entity_id in retired)
    for item in keep:
        current = registry.async_get(item.entity_id)
        assert current is not None
        # HA refreshes integration-owned metadata on setup; user choices and
        # identity, not the entire registry dataclass, must remain unchanged.
        for field in (
            "id",
            "unique_id",
            "entity_id",
            "config_entry_id",
            "platform",
            "name",
            "hidden_by",
            "disabled_by",
        ):
            assert getattr(current, field) == getattr(item, field)
    assert not entry.runtime_data.coordinator.enabled


async def test_fresh_install_has_fourteen_entities_and_no_retired_records(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), version=1, minor_version=3
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    items = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    keys = {item.unique_id.removeprefix(f"{entry.entry_id}_") for item in items}
    assert keys == set(SENSOR_NAMES) | {
        "discovery_result",
        "polling_status",
        "operating_state",
    }
    assert len(items) == 14
    assert sum(item.disabled_by is None for item in items) == 10
    assert not keys.intersection(RETIRED_ENTITY_KEYS)


async def test_future_major_version_is_not_modified(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=asdict(TARGET), version=2)
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 2
