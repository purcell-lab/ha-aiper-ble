"""Per-model query sets: the EcoSurfer M2 has no S1_INFO."""

from dataclasses import asdict
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble.const import DOMAIN
from custom_components.aiper_ble.coordinator import (
    HA_POLL_QUERIES,
    LOCAL_POLL_QUERIES,
    MODEL_POLL_QUERIES,
    S1_INFO_KEYS,
)
from custom_components.aiper_ble.datapoints import SENSOR_NAMES
from custom_components.aiper_ble.probe import MODELS, Target

from .helpers import TARGET
from .test_controls import fake_exchange
from .test_polling import OPTIONS, PATH

M2 = Target(TARGET.address, "Aiper-EcoSurfer M2-MS6TEST")


def test_every_model_has_a_query_set_without_s1_info_outside_the_s1():
    assert set(MODEL_POLL_QUERIES) == set(MODELS)
    assert MODEL_POLL_QUERIES["Surfer S1"] == (LOCAL_POLL_QUERIES, HA_POLL_QUERIES)
    local, ha = MODEL_POLL_QUERIES["EcoSurfer M2"]
    assert "S1_INFO" not in local and "S1_INFO" not in ha
    assert ha == ("OpInfo", "INFO", "WARN") and local == ("INFO",)
    assert S1_INFO_KEYS < set(SENSOR_NAMES)


async def test_m2_entry_polls_without_s1_info_and_has_no_s1_only_sensors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(M2), unique_id=M2.address, options=OPTIONS
    )
    entry.add_to_hass(hass)
    calls = []
    # The M2 answers the generic queries exactly as the S1 does (6 Nov 2026).
    with patch(
        f"{PATH}.coordinator.query_once", fake_exchange(calls, before="3,1,100")
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.poll_queries == ("OpInfo", "INFO", "WARN")
    assert [q.query_type for q in calls] == ["OpInfo", "INFO", "WARN"]
    assert coordinator.status == "ok"
    assert coordinator.data["battery"] == 100
    assert "temperature" not in coordinator.data
    keys = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    assert not keys & S1_INFO_KEYS
    assert "water_temperature" not in keys
    assert {
        "battery",
        "polling_status",
        "operating_state",
        "signal_strength",
        "vacuum",
    } <= keys
    assert hass.states.get("sensor.aiper_ecosurfer_m2_ble_battery").state == "100"
    assert (
        hass.states.get("sensor.aiper_ecosurfer_m2_ble_operating_state").state
        == "fully_charged"
    )
    assert hass.states.get("vacuum.aiper_ecosurfer_m2_ble").state == "docked"
