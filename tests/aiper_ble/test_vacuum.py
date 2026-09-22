"""Vacuum entity: verified state only, option-gated controls, no optimism."""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.aiper_ble.protocol import Control
from custom_components.aiper_ble.vacuum import ACTIVITIES

from .test_bluetooth import radio as radio  # noqa: F401 - fixture
from .test_controls import fake_exchange
from .test_polling import OPTIONS, PATH, response, setup
from .test_polling import transport as transport  # noqa: F401 - fixture
from .test_protocol import frame

ENTITY = "vacuum.aiper_surfer_s1"
ENABLED = {**OPTIONS, "confirm_vacuum_controls": True}


async def vacuum_call(hass, service):
    await hass.services.async_call(
        "vacuum", service, {"entity_id": ENTITY}, blocking=True
    )


def test_activity_mapping_covers_every_info_state():
    from custom_components.aiper_ble.s1_states import INFO_STATES

    assert ACTIVITIES["working"] == "cleaning"
    assert ACTIVITIES["sunward"] == "cleaning"
    assert ACTIVITIES["standby"] == "idle"
    assert ACTIVITIES["charging"] == "docked"
    assert ACTIVITIES["fully_charged"] == "docked"
    assert "updating" not in ACTIVITIES and "unknown_code" not in ACTIVITIES
    assert set(ACTIVITIES) < set(INFO_STATES)


async def test_state_follows_verified_polling_cycle(hass, transport):
    await setup(hass)
    state = hass.states.get(ENTITY)
    # The fixture's INFO reply is status 2 with battery 73: charging.
    assert state.state == "docked"
    assert state.attributes["operating_state"] == "charging"
    assert state.attributes["state_source"] == "polling_cycle"
    assert state.attributes["controls_enabled"] is False
    assert state.attributes["supported_features"] == 8192 | 8 | 4096


async def test_attributes_from_verified_cycle_and_five_field_info(hass, transport):
    def info(bus):
        bus.response = frame(response("INFO", {"ack": "+INFO:1,1,64,0,155\r\n"}))

    transport[1].extend([lambda bus: None, lambda bus: None, info])
    entry = await setup(hass)
    state = hass.states.get(ENTITY)
    assert state.state == "cleaning"
    attrs = state.attributes
    assert attrs["battery"] == 64
    assert attrs["temperature_c"] == 21.5
    assert attrs["warning_code_raw"] == 0
    assert attrs["minutes_counter_raw"] == 155
    assert attrs["polling_status"] == "ok"
    assert attrs["consecutive_failures"] == 0
    assert attrs["last_control"] is None
    coordinator = entry.runtime_data.coordinator
    assert (
        attrs["last_successful_poll"]
        == dt_util.as_local(coordinator.data["last_success"]).isoformat()
    )
    assert hass.states.get("sensor.aiper_ble_operating_status_raw").state == "1"
    # Three-field replies leave the counter unset rather than inventing a value.
    await coordinator.async_poll_now()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["minutes_counter_raw"] is None


async def test_nonzero_warning_reports_error_activity(hass, transport):
    def warn(bus):
        bus.response = frame(response("WARN", {"ack": "+WARN:5\r\n"}))

    transport[1].extend([lambda bus: None, lambda bus: None, lambda bus: None, warn])
    await setup(hass)
    state = hass.states.get(ENTITY)
    assert state.state == "error"
    assert state.attributes["warning_code_raw"] == 5
    assert state.attributes["operating_state"] == "charging"


async def test_last_route_attribute_from_ha_transport(hass, radio):
    await setup(hass)
    route = hass.states.get(ENTITY).attributes["last_route"]
    assert route is not None
    assert set(route) >= {"backend"}
    assert "AA:BB" not in str(route)


async def test_unavailable_when_polling_disabled_or_failed(hass, transport):
    entry = await setup(hass, {})
    assert hass.states.get(ENTITY).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    entry = await setup(hass)
    assert not entry.runtime_data.coordinator.last_update_success
    assert hass.states.get(ENTITY).state == "unavailable"


@pytest.mark.parametrize("service", ["start", "stop"])
async def test_controls_refused_until_option_enabled(hass, transport, service):
    entry = await setup(hass)
    with pytest.raises(HomeAssistantError, match="Vacuum controls are disabled"):
        await vacuum_call(hass, service)
    assert len(transport[0]) == 4
    assert entry.runtime_data.coordinator.last_control_result["status"] == "never_run"
    assert hass.states.get(ENTITY).state == "docked"


@pytest.mark.parametrize(
    "service,action,after,expected_calls,expected_state",
    [
        (
            "start",
            "start_cleaning",
            "1,1,80",
            ["INFO", "WARN", "MODE", "INFO"],
            "cleaning",
        ),
        ("stop", "stop_cleaning", "0,0,80", ["MODE", "INFO"], "idle"),
    ],
)
async def test_option_enabled_controls_use_guarded_path_and_readback(
    hass, transport, service, action, after, expected_calls, expected_state
):
    entry = await setup(hass, ENABLED)
    coordinator = entry.runtime_data.coordinator
    calls = []
    with patch(f"{PATH}.coordinator.query_once", fake_exchange(calls, after=after)):
        await vacuum_call(hass, service)
    assert [x.query_type for x in calls] == expected_calls
    assert sum(isinstance(x, Control) for x in calls) == 1
    assert coordinator.last_control_result["action"] == action
    assert coordinator.last_control_result["state_verified"]
    # Sensors are invalidated after a control; the entity shows the readback.
    assert not coordinator.last_update_success
    state = hass.states.get(ENTITY)
    assert state.state == expected_state
    assert state.attributes["state_source"] == "control_readback"
    assert state.attributes["controls_enabled"] is True
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    # The next verified cycle takes over again (the automatic one waits a full
    # interval after the control; an explicit poll runs at once).
    await coordinator.async_poll_now()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "docked"
    assert state.attributes["state_source"] == "polling_cycle"


async def test_unconfirmed_control_raises_and_leaves_no_trusted_state(hass, transport):
    entry = await setup(hass, ENABLED)
    coordinator = entry.runtime_data.coordinator
    calls = []
    # Readback still reports working after a stop: acknowledged, not verified.
    with patch(f"{PATH}.coordinator.query_once", fake_exchange(calls, after="1,1,80")):
        with pytest.raises(HomeAssistantError, match="No automatic retry"):
            await vacuum_call(hass, "stop")
    assert sum(isinstance(x, Control) for x in calls) == 1
    assert coordinator.last_control_result["motion_may_have_changed"]
    assert not coordinator.last_control_result["state_verified"]
    assert coordinator.status == "awaiting_poll_after_control"
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_entity_registered_on_shared_device(hass, transport):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry = await setup(hass)
    registry = er.async_get(hass)
    item = registry.async_get(ENTITY)
    assert item is not None
    assert item.unique_id == f"{entry.entry_id}_vacuum"
    assert item.disabled_by is None
    device = dr.async_get(hass).async_get(item.device_id)
    assert device.name == "Aiper Surfer S1 (BLE)"
    battery = registry.async_get("sensor.aiper_ble_battery")
    assert battery.device_id == item.device_id


async def test_entities_use_device_name_and_translated_names(hass, transport):
    """Bronze has-entity-name: names come from translations under the device."""
    await setup(hass)
    assert hass.states.get(ENTITY).name == "Aiper Surfer S1 (BLE)"
    assert (
        hass.states.get("sensor.aiper_ble_battery").name
        == "Aiper Surfer S1 (BLE) Battery"
    )
    assert (
        hass.states.get("sensor.aiper_ble_polling_status").name
        == "Aiper Surfer S1 (BLE) Polling status"
    )
    assert (
        hass.states.get("sensor.aiper_ble_discovery_result").name
        == "Aiper Surfer S1 (BLE) Discovery result"
    )
    assert (
        hass.states.get("sensor.aiper_ble_operating_state").name
        == "Aiper Surfer S1 (BLE) Operating state"
    )
