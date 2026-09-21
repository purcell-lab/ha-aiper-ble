"""Offline coordinator, entity, options and real bounded-transport tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aiper_ble_diagnostics import Runtime
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import (
    AiperCoordinator,
    verified_values,
)
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.probe import CHAR_IF, DEVICE_IF, probe
from custom_components.aiper_ble_diagnostics.protocol import (
    ProtocolError,
    Query,
    crc16,
    query_frame,
)
from custom_components.aiper_ble_diagnostics.telemetry import QueryBluez

from .helpers import TARGET
from .test_protocol import frame
from .test_query import CHAR, Bus, add_key_exchange, members

PATH = "custom_components.aiper_ble_diagnostics"
OPTIONS = {
    "polling_enabled": True,
    "confirm_exclusive_access": True,
    "poll_interval": 300,
    "allow_missing_advertisement": False,
}
S1 = Query("omit_empty_crc", "request", query_type="S1_INFO")
OP = Query("omit_empty_crc", "request")
INFO = Query("omit_empty_crc", "request", query_type="INFO")
WARN = Query("omit_empty_crc", "request", query_type="WARN")


def response(query_type="S1_INFO", data=None):
    if data is None:
        data = (
            {"sn": "PRIVATE_SERIAL", "timeZone": "UTC+10", "ack": "+S1_INFO:215,0\r\n"}
            if query_type == "S1_INFO"
            else {"ack": "+INFO:2,1,73\r\n"}
            if query_type == "INFO"
            else {"ack": "+WARN:0\r\n"}
            if query_type == "WARN"
            else {"wifi_rssi": -127}
        )
    return {
        "type": "OpInfo" if query_type == "OpInfo" else "Machine",
        "res": 0,
        "data": data,
        "chksum": crc16(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        ),
    }


@pytest.mark.parametrize(
    "raw, expected", [("215", 21.5), ("0", 0), ("-55", -5.5), ("123.5", 12.35)]
)
def test_temperature_scaling_and_private_fields(raw, expected):
    value = response(data={"ack": f"+S1_INFO:{raw},0\r\n", "sn": "PRIVATE_SERIAL"})
    assert verified_values(value, S1) == {
        "temperature": expected,
        "temperature_raw": float(raw),
        "solar_status_raw": 0,
        "s1_timezone": None,
    }


def test_known_opinfo_wire_checksum():
    value = response("OpInfo")
    assert value["chksum"] == 52546
    assert verified_values(value, OP)["wifi_rssi_raw"] == -127


@pytest.mark.parametrize("value", [None, True, -127.0, "-127", {}, []])
def test_wifi_non_integer_is_not_a_sensor_reading(value):
    assert (
        verified_values(response("OpInfo", {"wifi_rssi": value}), OP)["wifi_rssi_raw"]
        is None
    )


@pytest.mark.parametrize("value", [None, True, "0", 1, -1, 0.0])
def test_res_must_be_integer_zero(value):
    reply = response()
    reply["res"] = value
    with pytest.raises(ProtocolError, match="response_not_successful"):
        verified_values(reply, S1)


@pytest.mark.parametrize("value", [None, True, -1, 65536, "52546", 0])
def test_missing_invalid_or_mismatched_crc_rejected(value):
    reply = response()
    reply["chksum"] = value
    with pytest.raises(ProtocolError, match="response_checksum"):
        verified_values(reply, S1)


def test_wrong_query_and_modified_payload_rejected():
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(response("OpInfo"), S1)
    reply = response()
    reply["data"]["ack"] = "+S1_INFO:999,1\r\n"
    with pytest.raises(ProtocolError, match="response_checksum_mismatch"):
        verified_values(reply, S1)


@pytest.fixture
def transport():
    buses = []
    mutations = []

    @asynccontextmanager
    async def opened(target, *, query):
        assert target == TARGET
        bus = Bus()
        bus.response = frame(response(query.query_type))
        if mutations:
            mutations.pop(0)(bus)
        buses.append(bus)
        yield QueryBluez(bus, target, query)

    async def fake_query(hass, target, report, query):
        # Existing coordinator contract tests retain the proven legacy simulator.
        # The new HA transport is exercised independently in test_bluetooth.py.
        async with opened(target, query=query) as api:
            await probe(api, target, report, connect=True, query=query)

    with patch(f"{PATH}.coordinator.query_once", fake_query):
        yield buses, mutations


async def setup(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=asdict(TARGET),
        unique_id=TARGET.address,
        options=OPTIONS if options is None else options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_device_carries_bluetooth_connection_for_device_page(hass, transport):
    """HA's device page Bluetooth section keys off the registry connection."""
    from homeassistant.helpers import device_registry as dr

    entry = await setup(hass, {})
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, TARGET.address), entry.entry_id
    )
    assert device is not None
    assert (dr.CONNECTION_BLUETOOTH, TARGET.address) in device.connections
    assert device.manufacturer == "Aiper"
    # The address reaches the registry only; integration diagnostics stay redacted.
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert TARGET.address not in json.dumps(diagnostics)


@pytest.mark.parametrize(
    "options", [{}, {"polling_enabled": True}, {"confirm_exclusive_access": True}]
)
async def test_disabled_unless_both_authorisations(hass, transport, options):
    entry = await setup(hass, options)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    assert transport[0] == []
    assert coordinator.update_interval is None
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "disabled"
    assert hass.states.get("sensor.aiper_ble_discovery_result").state == "never_run"


async def test_full_cycle_entities_fixed_requests_and_privacy(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    buses, _ = transport
    assert len(buses) == 4
    assert bytes(buses[0].written) == query_frame(S1)
    assert bytes(buses[1].written) == query_frame(OP)
    assert bytes(buses[2].written) == query_frame(INFO)
    assert bytes(buses[3].written) == query_frame(WARN)
    assert hass.states.get("sensor.aiper_ble_battery").state == "73"
    assert hass.states.get("sensor.aiper_ble_operating_status_raw").state == "2"
    assert hass.states.get("sensor.aiper_ble_operating_mode_raw").state == "1"
    for bus in buses:
        assert members(bus).count("Connect") == 1
        assert members(bus).count("Disconnect") == 1
        assert members(bus).count("StartNotify") == 1
        assert members(bus).count("StopNotify") == 1
        assert "ReadValue" not in members(bus)
        assert "Pair" not in members(bus)
        assert "StartDiscovery" not in members(bus)
        assert bus.handlers == []
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    assert (
        hass.states.get("sensor.aiper_ble_temperature").attributes[
            "unit_of_measurement"
        ]
        == "°C"
    )
    solar = hass.states.get("sensor.aiper_ble_solar_status_raw")
    wifi = hass.states.get("sensor.aiper_ble_wi_fi_rssi_raw")
    assert solar.state == "0"
    assert wifi is None  # Raw/sentinel diagnostic is opt-in, not default UI.
    assert coordinator.data["wifi_rssi_raw"] == -127
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "ok"
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    for private in (
        "PRIVATE_SERIAL",
        TARGET.address,
        TARGET.name,
        TARGET.adapter_address,
    ):
        assert private not in str(coordinator.data)
        assert private not in str(hass.states.async_all())
        assert private not in str(diagnostic)
    assert len(buses) == 4  # Diagnostics are cached.
    before = coordinator.data
    await coordinator.async_refresh()
    assert len(buses) == 4  # Manual refresh cannot bypass cooldown.
    assert coordinator.data == before


async def test_scheduler_repeats_at_interval_and_stops_on_unload(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.next_attempt = 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    assert len(transport[0]) == 8
    assert await hass.config_entries.async_unload(entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
    await hass.async_block_till_done()
    assert len(transport[0]) == 8


async def test_atomic_failure_unavailability_backoff_and_recovery(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    old = coordinator.data
    buses, mutations = transport
    for expected_delay in (600, 1200, 2400, 3600, 3600):

        def corrupt(bus):
            value = response("OpInfo")
            value["chksum"] = 0
            bus.response = frame(value)

        mutations.extend([lambda bus: None, corrupt])
        coordinator.next_attempt = 0
        await coordinator.async_refresh()
        assert coordinator.data == old  # Never mix two different cycles.
        assert coordinator.update_interval.total_seconds() == expected_delay
        assert coordinator.error_code == "response_checksum_mismatch"
        assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
        assert (
            hass.states.get("sensor.aiper_ble_polling_status").attributes[
                "consecutive_failures"
            ]
            == coordinator.failures
        )
        count = len(buses)
        await coordinator.async_refresh()
        assert len(buses) == count
        assert not coordinator.last_update_success
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.failures == 0
    assert coordinator.update_interval.total_seconds() == 300
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"


@pytest.mark.parametrize("member", ["StopNotify", "Disconnect", "RemoveMatch"])
async def test_cleanup_failure_suspends_without_query_two_or_retry(
    hass, transport, member
):
    transport[1].append(lambda bus: setattr(bus, "failure", member))
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert len(transport[0]) == 1
    assert coordinator.suspended
    assert coordinator.update_interval is None
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "suspended"
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert len(transport[0]) == 1


@pytest.mark.parametrize("kind", ["ecdh", "key_exchange", "malformed", "security"])
async def test_unsafe_protocol_suspends(hass, transport, kind):
    def mutate(bus):
        if kind == "ecdh":
            bus.data[TARGET.device_path][DEVICE_IF]["ManufacturerData"] = {0: [1]}
        elif kind == "malformed":
            bus.data[TARGET.device_path][DEVICE_IF]["ManufacturerData"] = {0: "bad"}
        elif kind == "key_exchange":
            add_key_exchange(bus.data)
        else:
            bus.data[CHAR][CHAR_IF]["Flags"].append("secure-write")

    transport[1].append(mutate)
    entry = await setup(hass)
    assert entry.runtime_data.coordinator.suspended
    assert "WriteValue" not in members(transport[0][0])


@pytest.mark.parametrize("override", [False, True])
async def test_missing_advertisement_requires_persistent_exception(
    hass, transport, override
):
    def remove_ad(bus):
        bus.data[TARGET.device_path][DEVICE_IF].pop("ManufacturerData")

    transport[1].extend([remove_ad, remove_ad])
    entry = await setup(hass, {**OPTIONS, "allow_missing_advertisement": override})
    assert entry.runtime_data.coordinator.last_update_success is override
    if not override:
        assert "Connect" not in members(transport[0][0])


async def test_busy_robot_not_disconnected(hass, transport):
    transport[1].append(
        lambda bus: bus.data[TARGET.device_path][DEVICE_IF].update(Connected=True)
    )
    entry = await setup(hass)
    assert not entry.runtime_data.coordinator.last_update_success
    assert "Connect" not in members(transport[0][0])
    assert "Disconnect" not in members(transport[0][0])


async def test_manual_action_excludes_poll(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.next_attempt = 0
    other = asyncio.create_task(asyncio.Event().wait())
    entry.runtime_data.task = other
    await coordinator.async_refresh()
    assert len(transport[0]) == 4
    assert coordinator.status == "busy"
    other.cancel()
    with pytest.raises(asyncio.CancelledError):
        await other
    entry.runtime_data.task = None


async def test_poll_excludes_manual_action_and_unload_cleans_up(hass, transport):
    entry = await setup(hass)
    runtime = entry.runtime_data
    coordinator = entry.runtime_data.coordinator
    coordinator.next_attempt = 0
    started = asyncio.Event()

    def stall(bus):
        bus.stall = "WriteValue"
        bus.signal_started = started

    transport[1].append(stall)
    task = asyncio.create_task(coordinator.async_refresh())
    await started.wait()
    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call(
            DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(transport[0]) == 5
    assert members(transport[0][-1]).count("StopNotify") == 1
    assert members(transport[0][-1]).count("Disconnect") == 1
    assert transport[0][-1].handlers == []
    assert runtime.task is None


async def test_cycle_deadline_cleans_up_without_retry(hass, transport):
    transport[1].append(lambda bus: setattr(bus, "stall", "WriteValue"))
    with patch(f"{PATH}.coordinator.POLL_SECONDS", 0.03):
        entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.error_code == "timeout"
    assert not coordinator.last_update_success
    assert len(transport[0]) == 1
    assert members(transport[0][0]).count("StopNotify") == 1
    assert members(transport[0][0]).count("Disconnect") == 1


async def test_options_require_consent_and_reload_disables_polling(hass, transport):
    entry = await setup(hass, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "confirm_exclusive_access": False}
    )
    assert result["errors"] == {"base": "exclusive_access_required"}
    assert transport[0] == []
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], OPTIONS
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(transport[0]) == 4
    old = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "polling_enabled": False}
    )
    await hass.async_block_till_done()
    assert old.closing
    assert not entry.runtime_data.coordinator.enabled
    assert len(transport[0]) == 4
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "disabled"


@pytest.mark.parametrize("value", [False, -1, 299, 3601, "abc"])
def test_invalid_stored_interval_falls_back_to_safe_default(hass, value):
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS, "poll_interval": value})
    coordinator = AiperCoordinator(hass, entry, Runtime(TARGET))
    assert coordinator.interval == 300


async def test_failed_robot_does_not_prevent_entry_loading(hass, transport):
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    entry = await setup(hass)
    assert entry.state == config_entries.ConfigEntryState.LOADED
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "failed"
    assert not entry.runtime_data.coordinator.last_update_success


async def test_next_success_changes_entity_states(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    transport[1].extend(
        [
            lambda bus: setattr(
                bus, "response", frame(response(data={"ack": "+S1_INFO:265,1\r\n"}))
            ),
            lambda bus: setattr(
                bus, "response", frame(response("OpInfo", {"wifi_rssi": -64}))
            ),
        ]
    )
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert hass.states.get("sensor.aiper_ble_temperature").state == "26.5"
    assert hass.states.get("sensor.aiper_ble_solar_status_raw").state == "1"
    assert coordinator.data["wifi_rssi_raw"] == -64


async def test_failed_manual_cleanup_suspends_automatic_polling(hass, transport):
    entry = await setup(hass)
    bus = Bus()
    bus.failure = "StopNotify"

    @asynccontextmanager
    async def manual(target, *, query, allow_read):
        yield QueryBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", manual), pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "query_once",
            {
                "entry_id": entry.entry_id,
                "checksum_mode": "omit_empty_crc",
                "write_mode": "request",
                "confirm_app_closed": True,
                "confirm_notifications": True,
                "confirm_query_write": True,
            },
            blocking=True,
        )
    coordinator = entry.runtime_data.coordinator
    assert coordinator.suspended
    assert coordinator.update_interval is None
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "suspended"
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert len(transport[0]) == 4


async def test_timer_jitter_does_not_skip_cycle(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.next_attempt = asyncio.get_running_loop().time() + 0.5
    await coordinator.async_refresh()
    assert len(transport[0]) == 8
