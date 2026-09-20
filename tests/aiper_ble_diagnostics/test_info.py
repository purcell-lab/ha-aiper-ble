"""App-derived mappings and live-derived INFO text; synthetic CRC envelopes."""

from dataclasses import asdict

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble_diagnostics import async_migrate_entry
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import verified_values
from custom_components.aiper_ble_diagnostics.datapoints import (
    DEFAULT_ENABLED,
    SENSOR_NAMES,
)
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.protocol import (
    ProtocolError,
    Query,
    preview,
    query_frame,
)

from .helpers import TARGET
from .test_polling import INFO, OP, S1, response, setup
from .test_polling import transport as transport
from .test_protocol import frame


@pytest.mark.parametrize("mode", ["omit_empty_crc", "include_empty_crc"])
def test_info_fixed_frame_and_crc(mode):
    query = Query(mode, "request", query_type="INFO")
    # Independent table-driven Modbus calculation, not the production helper.
    assert preview(query)["request_json"] == {
        "type": "Machine",
        "data": {"cmd": "AT+INFO?"},
        "chksum": 10442,
    }
    assert query_frame(query) == (
        b"aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTW3oQNy0WK1Qw"
        b"Vz4TYUE7WigFZkwmBis=\n"
    )
    assert preview(query)["checksum_semantics"] == "nonempty_data_crc_required"


@pytest.mark.parametrize("field", ["ack", "report"])
@pytest.mark.parametrize("battery", [0, 1, 73, 100])
def test_info_mapping_and_battery_boundaries(field, battery):
    assert verified_values(
        response("INFO", {field: f"+INFO:2,1,{battery}\r\n"}), INFO
    ) == {"info_status_raw": 2, "info_mode_raw": 1, "battery": battery}


@pytest.mark.parametrize("field", ["ack", "report"])
def test_live_observed_five_fields_only_publishes_documented_positions(field):
    assert verified_values(
        response("INFO", {field: "+INFO:0,0,93,0,155\r\n"}), INFO
    ) == {"info_status_raw": 0, "info_mode_raw": 0, "battery": 93}


@pytest.mark.parametrize("battery", [-127, -1, 101, 255])
def test_five_field_battery_sentinel_remains_unavailable(battery):
    assert verified_values(
        response("INFO", {"ack": f"+INFO:0,0,{battery},0,155\r\n"}), INFO
    ) == {"info_status_raw": 0, "info_mode_raw": 0, "battery": None}


@pytest.mark.parametrize("battery", [-127, -1, 101, 255, 2147483647])
def test_battery_sentinels_not_clamped_or_published(battery):
    values = verified_values(
        response("INFO", {"ack": f"+INFO:2,1,{battery}\r\n"}), INFO
    )
    assert values == {"info_status_raw": 2, "info_mode_raw": 1, "battery": None}


@pytest.mark.parametrize(
    "text",
    [
        "+INFO:2,1,73",
        "+INFO:2,1,73\n",
        "+INFO:2,1,73\r\nextra",
        "+INFO:2,1\r\n",
        "+INFO:2,1,73,4\r\n",
        "+INFO:2,1,73.0\r\n",
        "+INFO:2,1,true\r\n",
        "+INFO:2,1,NaN\r\n",
        "+INFO:2,1,７３\r\n",
        "+INFO:2147483648,1,73\r\n",
        "+INFO:2,-2147483649,73\r\n",
        "+INFO:2,1,2147483648\r\n",
        "+INFO:2, 1,73\r\n",
        "+INFO:0,0,93,0,155,6\r\n",
        "+INFO:0,0,93,0,2147483648\r\n",
        "+INFO:0,0,93,-2147483649,155\r\n",
        "+INFO:0,0,93,0,NaN\r\n",
        "+INFO:0,0,93,0,155.0\r\n",
        "+INFO:0,0,93,0, 155\r\n",
        "+INFO:0,0,93,0,１５５\r\n",
        "+INFO:0,0,93,0,155\n",
    ],
)
def test_info_malformed_response_fails_closed(text):
    with pytest.raises(ProtocolError, match="invalid_info_response"):
        verified_values(response("INFO", {"ack": text}), INFO)


@pytest.mark.parametrize("query", [S1, OP])
def test_info_cannot_be_used_for_another_query(query):
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(response("INFO"), query)


@pytest.mark.parametrize("query_type", ["S1_INFO", "OpInfo"])
def test_other_queries_cannot_supply_info(query_type):
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(response(query_type), INFO)


def test_info_report_precedence_and_crc_guard():
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(
            response("INFO", {"report": "+OTHER:1\r\n", "ack": "+INFO:2,1,73\r\n"}),
            INFO,
        )
    payload = response("INFO")
    payload["data"]["ack"] = "+INFO:2,1,99\r\n"
    with pytest.raises(ProtocolError, match="response_checksum_mismatch"):
        verified_values(payload, INFO)
    payload = response("INFO")
    payload["res"] = 1
    with pytest.raises(ProtocolError, match="response_not_successful"):
        verified_values(payload, INFO)


@pytest.mark.parametrize("command", ["AT+INFO?", "INFO=1", "info", "WARN=1", "RESET"])
def test_info_extension_does_not_enable_arbitrary_commands(command):
    with pytest.raises(ProtocolError, match="invalid_query_type"):
        Query("omit_empty_crc", "request", query_type=command)


async def test_only_useful_defaults_and_private_field_presence(hass, transport):
    entry = await setup(hass)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    enabled = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in entries
        if item.disabled_by is None
    }
    assert enabled == DEFAULT_ENABLED | {"polling_status", "discovery_result"}
    battery = hass.states.get("sensor.aiper_ble_battery")
    assert battery.attributes["device_class"] == "battery"
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.attributes["state_class"] == "measurement"
    for entity_id in (
        "sensor.aiper_ble_operating_mode_raw",
        "sensor.aiper_ble_operating_status_raw",
    ):
        assert "unit_of_measurement" not in hass.states.get(entity_id).attributes
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    evidence = diagnostics["polling"]["field_evidence"]
    assert evidence["current"]
    assert "battery" in evidence["present_in_last_successful_cycle"]
    assert "opinfo_bat_raw" not in evidence["present_in_last_successful_cycle"]
    assert "wifi_name" not in evidence["present_in_last_successful_cycle"]
    assert "PRIVATE_SERIAL" not in str(diagnostics)


@pytest.mark.parametrize("failure", ["crc", "cleanup", "shape"])
async def test_third_query_failure_never_publishes_partial_cycle(
    hass, transport, failure
):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    old = coordinator.data

    def fail(bus):
        if failure == "cleanup":
            bus.failure = "StopNotify"
        else:
            value = response("INFO")
            if failure == "crc":
                value["chksum"] = 0
            else:
                value = response("INFO", {"ack": "+INFO:2,1\r\n"})
            bus.response = frame(value)

    transport[1].extend([lambda bus: None, lambda bus: None, fail])
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert coordinator.data == old
    assert not coordinator.last_update_success
    assert coordinator.last_poll_details["query_type"] == "INFO"
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert len(transport[0]) == 7  # Four initially, three before INFO fails.
    assert coordinator.suspended is (failure == "cleanup")


async def test_invalid_battery_does_not_retain_last_cycle_percentage(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    transport[1].extend(
        [
            lambda bus: None,
            lambda bus: None,
            lambda bus: setattr(
                bus, "response", frame(response("INFO", {"ack": "+INFO:4,3,255\r\n"}))
            ),
        ]
    )
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_operating_mode_raw").state == "3"


async def test_migration_preserves_ids_history_and_user_choices(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), version=1, minor_version=1
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    records = {}
    for key in SENSOR_NAMES:
        records[key] = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=f"original_{key}",
        )
    registry.async_update_entity(
        records["wifi_name"].entity_id, name="My Wi-Fi diagnostic"
    )
    registry.async_update_entity(
        records["opinfo_status_raw"].entity_id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_update_entity(
        records["opinfo_link_raw"].entity_id, hidden_by=er.RegistryEntryHider.USER
    )
    before = {(item.entity_id, item.unique_id) for item in records.values()}
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 2
    assert before == {
        (item.entity_id, item.unique_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    candidate_id = records["opinfo_bat_raw"].entity_id
    assert (
        registry.async_get(candidate_id).hidden_by == er.RegistryEntryHider.INTEGRATION
    )
    assert registry.async_get(candidate_id).disabled_by is None
    assert registry.async_get(records["wifi_name"].entity_id).hidden_by is None
    assert registry.async_get(records["temperature"].entity_id).hidden_by is None
    assert (
        registry.async_get(records["opinfo_status_raw"].entity_id).disabled_by
        == er.RegistryEntryDisabler.USER
    )
    assert (
        registry.async_get(records["opinfo_link_raw"].entity_id).hidden_by
        == er.RegistryEntryHider.USER
    )
    registry.async_update_entity(candidate_id, hidden_by=None)
    assert await async_migrate_entry(hass, entry)
    assert registry.async_get(candidate_id).hidden_by is None
    # Exercise actual platform setup: existing enabled records must not inherit
    # the new disabled-by-default setting, and a user unhide must survive reload.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(candidate_id).disabled_by is None
    assert hass.states.get(candidate_id) is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(candidate_id).hidden_by is None
    assert registry.async_get(candidate_id).disabled_by is None
    assert (
        registry.async_get(records["temperature"].entity_id).unique_id
        == records["temperature"].unique_id
    )
