"""Buttons: immediate poll and the option-gated start/stop controls."""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble.protocol import Control

from .test_bluetooth import radio as radio  # noqa: F401 - fixture
from .test_controls import fake_exchange
from .test_polling import OPTIONS, PATH, setup
from .test_polling import transport as transport  # noqa: F401 - fixture

ENABLED = {**OPTIONS, "confirm_vacuum_controls": True}
KEYS = ("poll_now", "start_cleaning", "stop_cleaning")


async def press(hass, key):
    await hass.services.async_call(
        "button", "press", {"entity_id": f"button.aiper_ble_{key}"}, blocking=True
    )


async def test_buttons_follow_polling_availability(hass, transport):
    entry = await setup(hass, {})
    for key in KEYS:
        assert hass.states.get(f"button.aiper_ble_{key}").state == "unavailable"
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    await setup(hass)
    for key in KEYS:
        assert hass.states.get(f"button.aiper_ble_{key}").state != "unavailable"


async def test_poll_now_button_runs_one_cycle_at_once(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.failures = 3
    before = len(transport[0])
    await press(hass, "poll_now")
    assert len(transport[0]) == before + 4
    assert coordinator.status == "ok"
    assert coordinator.failures == 0


async def test_poll_now_button_reports_a_failed_cycle(hass, transport):
    await setup(hass)
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    with pytest.raises(HomeAssistantError):
        await press(hass, "poll_now")


@pytest.mark.parametrize("key", ["start_cleaning", "stop_cleaning"])
async def test_control_buttons_refused_until_option_enabled(hass, transport, key):
    entry = await setup(hass)
    calls = []
    with patch(f"{PATH}.coordinator.query_once", fake_exchange(calls)):
        with pytest.raises(HomeAssistantError, match="Vacuum controls are disabled"):
            await press(hass, key)
    assert not calls
    assert entry.runtime_data.coordinator.last_control_result["status"] == "never_run"


async def test_stop_button_runs_the_guarded_control(hass, transport):
    entry = await setup(hass, ENABLED)
    coordinator = entry.runtime_data.coordinator
    calls = []
    with patch(f"{PATH}.coordinator.query_once", fake_exchange(calls, after="0,0,80")):
        await press(hass, "stop_cleaning")
    assert [x.query_type for x in calls] == ["MODE", "INFO"]
    assert sum(isinstance(x, Control) for x in calls) == 1
    assert coordinator.last_control_result["action"] == "stop_cleaning"
    assert coordinator.last_control_result["state_verified"]
    assert coordinator.status == "awaiting_poll_after_control"
