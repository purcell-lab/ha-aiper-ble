"""All supported DP entities, strict missing-value handling and privacy."""

from dataclasses import asdict

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble_diagnostics import Runtime
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import (
    AiperCoordinator,
    verified_values,
)
from custom_components.aiper_ble_diagnostics.datapoints import (
    MACHINE_FIELDS,
    OPINFO_FIELDS,
    SENSOR_NAMES,
    integer,
    network_name,
    timezone,
)
from custom_components.aiper_ble_diagnostics.sensor import TelemetrySensor

from .helpers import TARGET
from .test_polling import OP, OPTIONS, S1, response


def test_all_s1_fields_are_exposed_without_envelope_or_serial():
    data = verified_values(
        response(
            data={
                "ack": "+S1_INFO:215,0\r\n",
                "sn": "PRIVATE_SERIAL",
                "timeZone": "UTC+10",
            }
        ),
        S1,
    )
    assert data == {
        "temperature": 21.5,
        "temperature_raw": 215.0,
        "solar_status_raw": 0,
        "s1_timezone": "UTC+10",
    }
    assert "PRIVATE_SERIAL" not in str(data)
    assert "chksum" not in data and "ack" not in data


def test_all_app_opinfo_and_machine_fields_are_distinct():
    fields = {
        "wifi_rssi": -127,
        "wifi_name": "Test network",
        "bat": 73,
        "status": 2,
        "link": 1,
    }
    fields["Machine"] = {field: index for index, field in enumerate(MACHINE_FIELDS)}
    fields["Machine"]["warn_code"] = 2**50
    fields.update(
        password="PRIVATE_PASSWORD", token="PRIVATE_TOKEN", sn="PRIVATE_SERIAL"
    )
    data = verified_values(response("OpInfo", fields), OP)
    assert data["wifi_name"] == "Test network"
    assert data["wifi_rssi_raw"] == -127
    for field in OPINFO_FIELDS:
        assert data[f"opinfo_{field}_raw"] == fields[field]
    for field in MACHINE_FIELDS:
        assert data[f"opinfo_machine_{field}_raw"] == fields["Machine"][field]
    assert data["opinfo_status_raw"] != data["opinfo_machine_status_raw"]
    assert "PRIVATE_" not in str(data)


def test_live_opinfo_shape_does_not_invent_battery_or_other_dps():
    data = verified_values(response("OpInfo", {"wifi_rssi": -127}), OP)
    assert data["wifi_rssi_raw"] == -127
    assert all(value is None for key, value in data.items() if key != "wifi_rssi_raw")


@pytest.mark.parametrize(
    "value", [True, False, 1.2, "73", [], {}, None, 2**31, -(2**31) - 1]
)
def test_bad_integer_fields_unavailable(value):
    assert integer(value) is None
    data = verified_values(
        response("OpInfo", {"bat": value, "Machine": {"cap": value}}), OP
    )
    assert data["opinfo_bat_raw"] is None
    assert data["opinfo_machine_cap_raw"] is None


@pytest.mark.parametrize(
    "value", [None, 123, {}, [], "", "a" * 33, "bad\nname", "é" * 17]
)
def test_unsafe_network_names_not_states(value):
    assert network_name(value) is None


@pytest.mark.parametrize("value", [None, {}, 123, "", "a" * 65, "bad\nzone"])
def test_invalid_time_zone_not_a_state(value):
    assert timezone(value) is None


def test_valid_unicode_ssid_and_timezone():
    assert network_name("Pool Café") == "Pool Café"
    assert timezone("Australia/Brisbane") == "Australia/Brisbane"
    assert timezone("UTC+10") == "UTC+10"


async def test_entities_show_only_current_cycle_values(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=asdict(TARGET), options=OPTIONS)
    entry.add_to_hass(hass)
    runtime = Runtime(TARGET)
    runtime.coordinator = AiperCoordinator(hass, entry, runtime)
    entry.runtime_data = runtime
    coordinator = runtime.coordinator
    coordinator.last_update_success = True
    coordinator.data = {
        **verified_values(
            response(data={"ack": "+S1_INFO:215,0\r\n", "timeZone": "UTC+10"}), S1
        ),
        **verified_values(
            response(
                "OpInfo",
                {
                    "wifi_rssi": -127,
                    "wifi_name": "Test network",
                    "bat": 73,
                    "status": 2,
                    "link": 1,
                    "Machine": {field: 1 for field in MACHINE_FIELDS},
                },
            ),
            OP,
        ),
    }
    sensors = {key: TelemetrySensor(entry, key) for key in SENSOR_NAMES}
    for key, sensor in sensors.items():
        if key != "last_success":
            assert sensor.available
            assert sensor.native_value is not None
        if key.endswith("_raw"):
            assert sensor.native_unit_of_measurement is None
            assert sensor.device_class is None
    assert sensors["temperature"].native_unit_of_measurement == "°C"
    # A subsequent valid response missing optional fields must not retain them.
    coordinator.data = verified_values(response("OpInfo", {"wifi_rssi": -127}), OP)
    assert sensors["wifi_rssi_raw"].available
    for key, sensor in sensors.items():
        if key != "wifi_rssi_raw":
            assert not sensor.available
    coordinator.last_update_success = False
    assert not sensors["wifi_rssi_raw"].available
