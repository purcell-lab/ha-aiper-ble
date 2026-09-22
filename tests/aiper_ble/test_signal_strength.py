"""Signal strength: the observer's reading of the route the last cycle used."""

from .test_bluetooth import radio as radio  # noqa: F401 - fixture
from .test_polling import setup
from .test_polling import transport as transport  # noqa: F401 - fixture

ENTITY = "sensor.aiper_ble_signal_strength"


def route_cycle(selected, rssi):
    return {
        "query_type": "WARN",
        "transport_diagnostics": {
            "backend": "bleak_esphome",
            "selected_route": selected,
            "route_snapshots": {
                "before_connect": {
                    "routes": [
                        {
                            "route_id": "route_1",
                            "scanner_type": "remote",
                            "rssi_dbm": -95,
                        },
                        {
                            "route_id": "route_2",
                            "scanner_type": "remote",
                            "rssi_dbm": rssi,
                        },
                    ]
                }
            },
        },
    }


async def test_selected_route_signal_and_attributes(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.last_poll_queries = [route_cycle("route_2", -71)]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "-71"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["device_class"] == "signal_strength"
    assert state.attributes["backend"] == "bleak_esphome"
    assert state.attributes["scanner_type"] == "remote"


async def test_unavailable_without_a_selected_route(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.last_poll_queries = [route_cycle(None, -71)]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_local_adapter_uses_its_preconnect_reading(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.last_poll_queries = [
        {
            "query_type": "INFO",
            "preconnect_rssi_dbm": -58,
            "transport_diagnostics": {"backend": "local_bluez", "selected_route": None},
        }
    ]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "-58"
    assert state.attributes["backend"] == "local_bluez"


async def test_unavailable_when_polling_disabled(hass, transport):
    await setup(hass, {})
    assert hass.states.get(ENTITY).state == "unavailable"
