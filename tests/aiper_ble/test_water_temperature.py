"""Water temperature: captured only in working cycles, held and restored."""

import pytest
from homeassistant.core import State
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from .test_polling import response, setup
from .test_polling import transport as transport  # noqa: F401 - fixture
from .test_protocol import frame

ENTITY = "sensor.aiper_ble_water_temperature"


def cycle(transport, temperature_raw, info):
    def s1(bus):
        bus.response = frame(
            response(
                "S1_INFO",
                {"timeZone": "UTC+10", "ack": f"+S1_INFO:{temperature_raw},0\r\n"},
            )
        )

    def info_reply(bus):
        bus.response = frame(response("INFO", {"ack": f"+INFO:{info}\r\n"}))

    transport[1].extend([s1, lambda bus: None, info_reply, lambda bus: None])


async def test_only_working_cycles_update_and_value_is_held(hass, transport):
    entry = await setup(hass)  # fixture INFO is charging
    assert hass.states.get(ENTITY).state == "unavailable"
    coordinator = entry.runtime_data.coordinator
    cycle(transport, 230, "1,1,64")
    await coordinator.async_poll_now()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "23.0"
    assert state.attributes["device_class"] == "temperature"
    measured = dt_util.parse_datetime(state.attributes["measured_at"])
    assert measured == coordinator.water_temperature_at
    assert measured.utcoffset() == dt_util.now().utcoffset()  # local time
    assert state.attributes["temperature_sensor_location"] == "unverified"
    # Back on the charger with a different reading: general sensor moves on,
    # the water temperature keeps the working-cycle value.
    cycle(transport, 190, "2,0,64")
    await coordinator.async_poll_now()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.aiper_ble_temperature").state == "19.0"
    assert hass.states.get(ENTITY).state == "23.0"
    # A failed cycle leaves it in place too.
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    with pytest.raises(UpdateFailed):
        await coordinator.async_poll_now()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert hass.states.get(ENTITY).state == "23.0"


async def test_restored_after_restart_until_a_working_cycle(hass, transport):
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY, "24.5", {"measured_at": "2026-09-22T11:26:28+10:00"}),
                {"native_value": 24.5, "native_unit_of_measurement": "°C"},
            ),
        ),
    )
    entry = await setup(hass)
    state = hass.states.get(ENTITY)
    assert state.state == "24.5"
    assert dt_util.parse_datetime(
        state.attributes["measured_at"]
    ) == dt_util.parse_datetime("2026-09-22T11:26:28+10:00")
    cycle(transport, 261, "1,1,60")
    await entry.runtime_data.coordinator.async_poll_now()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "26.1"


async def test_unavailable_when_polling_disabled(hass, transport):
    await setup(hass, {})
    assert hass.states.get(ENTITY).state == "unavailable"
