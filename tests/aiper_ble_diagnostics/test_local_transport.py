"""Rollback uses real guarded BlueZ exchange code, never the HA/Bleak route."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble_diagnostics.coordinator import ISOLATED_QUERIES
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.local_transport import query_once
from custom_components.aiper_ble_diagnostics.protocol import Query, query_frame
from custom_components.aiper_ble_diagnostics.telemetry import QueryBluez

from .helpers import TARGET
from .test_isolated_services import call_query
from .test_polling import OPTIONS, PATH, response, setup
from .test_protocol import frame
from .test_query import Bus, members


@pytest.fixture
def local_radio():
    buses, mutations = [], []

    @asynccontextmanager
    async def opened(target, *, query):
        assert target == TARGET
        bus = Bus()
        bus.response = frame(response(query.query_type))
        if mutations:
            mutations.pop(0)(bus)
        buses.append(bus)
        yield QueryBluez(bus, target, query)

    with (
        patch(f"{PATH}.local_transport.open_bluez", opened),
        patch(f"{PATH}.coordinator.query_once", new_callable=AsyncMock) as remote,
    ):
        yield buses, mutations, remote
        remote.assert_not_awaited()


@pytest.mark.parametrize("service,query_type", ISOLATED_QUERIES.items())
async def test_local_isolated_query_verified_and_pinned(
    hass, local_radio, service, query_type
):
    entry = await setup(hass, {"use_local_adapter": True})
    result = await call_query(hass, entry, service)
    assert result["transport"] == "local_bluez"
    diagnostic = result["transport_diagnostics"]
    assert diagnostic["backend"] == "local_bluez"
    assert diagnostic["selected_route"] is None
    assert diagnostic["route_snapshots"] == {}
    assert diagnostic["phase_ms"]["local_probe_including_cleanup"] >= 0
    assert result["mode"] == "isolated_local_bluez_query"
    assert result["status"] == "query_complete"
    assert result["phase"] == "verify_response"
    assert result["values"]
    assert result["cleanup"] == "disconnected_confirmed"
    assert result["notification_cleanup"] == "stop_confirmed"
    assert result["updates_entities"] is False
    assert entry.runtime_data.coordinator.data is None
    bus = local_radio[0][0]
    assert len(local_radio[0]) == 1
    assert bytes(bus.written) == query_frame(
        Query("omit_empty_crc", "request", query_type=query_type)
    )
    assert members(bus).count("Connect") == members(bus).count("Disconnect") == 1
    assert bus.handlers == []
    assert "PRIVATE_SERIAL" not in json.dumps(result)
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await call_query(hass, entry, service)


async def test_local_soc_temperature_cycle_updates_entities(hass, local_radio):
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    assert len(local_radio[0]) == 2
    for bus, query_type in zip(local_radio[0], ("S1_INFO", "INFO"), strict=True):
        assert bytes(bus.written) == query_frame(
            Query("omit_empty_crc", "request", query_type=query_type)
        )
        assert members(bus).count("Connect") == 1
        assert members(bus).count("Disconnect") == 1
        assert members(bus).count("StopNotify") == 1
    assert entry.runtime_data.coordinator.status == "ok"
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    assert hass.states.get("sensor.aiper_ble_battery").state == "73"
    assert "wifi_rssi_raw" not in entry.runtime_data.coordinator.data
    assert "warning_code_raw" not in entry.runtime_data.coordinator.data
    assert hass.states.get("sensor.aiper_ble_warning_code_raw").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_polling_status").attributes[
        "configured_queries"
    ] == ["S1_INFO", "INFO"]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["polling"]["transport"] == "local_bluez"
    assert diagnostics["polling"]["configured_queries"] == ["S1_INFO", "INFO"]
    assert diagnostics["polling"]["last_poll_details"]["transport"] == "local_bluez"


async def test_local_corrupt_crc_never_publishes(hass, local_radio):
    bad = response("INFO")
    bad["chksum"] ^= 1
    local_radio[1].append(lambda bus: setattr(bus, "response", frame(bad)))
    entry = await setup(hass, {"use_local_adapter": True})
    result = await call_query(hass, entry)
    assert result["error_code"] == "response_checksum_mismatch"
    assert result["values"] == {}
    assert entry.runtime_data.coordinator.data is None


@pytest.mark.parametrize(
    "failure", ["Connect", "StopNotify", "Disconnect", "RemoveMatch"]
)
async def test_local_failure_no_fallback_and_cleanup(hass, local_radio, failure):
    local_radio[1].append(lambda bus: setattr(bus, "failure", failure))
    entry = await setup(hass, {"use_local_adapter": True})
    result = await call_query(hass, entry)
    assert result["status"] in {"failed", "suspended"}
    assert result["values"] == {}
    assert len(local_radio[0]) == 1
    if failure == "Connect":
        assert result["failure_stage"] == "connect"
        assert result["write_attempts"] == 0
        assert result["cleanup"] == "disconnected_confirmed"
    else:
        assert entry.runtime_data.coordinator.suspended
    assert "error" not in result


async def test_local_live_info_full_cycle_and_bounded_diagnostics(hass, local_radio):
    local_radio[1].extend(
        [
            lambda bus: None,
            lambda bus: setattr(
                bus,
                "response",
                frame(response("INFO", {"ack": "+INFO:0,0,93,0,155\r\n"})),
            ),
        ]
    )
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    assert hass.states.get("sensor.aiper_ble_battery").state == "93"
    assert hass.states.get("sensor.aiper_ble_operating_status_raw").state == "0"
    assert hass.states.get("sensor.aiper_ble_operating_mode_raw").state == "0"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    queries = diagnostics["polling"]["last_poll_queries"]
    assert [item["query_type"] for item in queries] == [
        "S1_INFO",
        "INFO",
    ]
    assert all(item["phase"] == "verify_response" for item in queries)
    assert all(item["cleanup"] == "disconnected_confirmed" for item in queries)
    assert all(item["notification_cleanup"] == "stop_confirmed" for item in queries)
    assert "protocol_response" not in json.dumps(queries)
    assert "PRIVATE_SERIAL" not in json.dumps(queries)
    assert "155" not in str(entry.runtime_data.coordinator.data)


async def test_local_shape_failure_is_protocol_error_then_recovers(hass, local_radio):
    local_radio[1].extend(
        [
            lambda bus: None,
            lambda bus: setattr(
                bus, "response", frame(response("INFO", {"ack": "+INFO:0,0\r\n"}))
            ),
        ]
    )
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_poll_details["error_category"] == "protocol"
    assert coordinator.last_poll_details["query_type"] == "INFO"
    assert len(coordinator.last_poll_queries) == 2
    assert coordinator.data is None
    assert coordinator.failures == 1
    assert coordinator.update_interval.total_seconds() == 600
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    # Simulate the next permitted tick without touching real radios or clocks.
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.failures == 0
    assert coordinator.update_interval.total_seconds() == 300
    assert len(coordinator.last_poll_queries) == 2
    assert len(local_radio[0]) == 4
    assert hass.states.get("sensor.aiper_ble_battery").state == "73"


@pytest.mark.parametrize("failed_query", [0, 1])
@pytest.mark.parametrize("failure", ["Connect", "StopNotify", "Disconnect"])
async def test_minimal_local_cycle_failure_never_publishes_partial_values(
    hass, local_radio, failed_query, failure
):
    local_radio[1].extend([lambda bus: None] * failed_query)
    local_radio[1].append(lambda bus: setattr(bus, "failure", failure))
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    coordinator = entry.runtime_data.coordinator
    assert len(local_radio[0]) == failed_query + 1
    assert coordinator.data is None
    assert not coordinator.last_update_success
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert coordinator.suspended == (failure != "Connect")
    assert (
        coordinator.update_interval is None
        if coordinator.suspended
        else (coordinator.update_interval.total_seconds() == 600)
    )


async def test_minimal_local_poll_now_retains_cooldown(hass, local_radio):
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    coordinator = entry.runtime_data.coordinator
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await coordinator.async_poll_now()
    assert len(local_radio[0]) == 2
    coordinator.last_attempt_finished -= 301
    await coordinator.async_poll_now()
    assert len(local_radio[0]) == 4
    assert [item["query_type"] for item in coordinator.last_poll_queries] == [
        "S1_INFO",
        "INFO",
    ]
    assert coordinator.status == "ok"


async def test_missing_local_adapter_no_bus_or_proxy(hass):
    target = replace(TARGET, adapter_path=None, adapter_address=None)
    report = {}
    with patch(f"{PATH}.local_transport.open_bluez") as opened:
        await query_once(hass, target, report, Query("omit_empty_crc", "request"))
    opened.assert_not_called()
    assert report["error_code"] == "local_adapter_required"
    assert report["write_attempts"] == 0


async def test_options_preserve_local_selection(hass, local_radio):
    entry = await setup(hass, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**OPTIONS, "polling_enabled": False, "use_local_adapter": True},
    )
    await hass.async_block_till_done()
    assert entry.options["use_local_adapter"] is True
    assert entry.runtime_data.coordinator.transport == "local_bluez"
    assert not local_radio[0]


async def test_options_reject_proxy_only_local_selection(hass, local_radio):
    entry = await setup(hass, {})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "adapter_path": None, "adapter_address": None}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**OPTIONS, "polling_enabled": False, "use_local_adapter": True},
    )
    assert result["errors"] == {"base": "local_adapter_required"}
    assert not local_radio[0]
