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


async def test_local_full_cycle_updates_entities(hass, local_radio):
    entry = await setup(hass, {**OPTIONS, "use_local_adapter": True})
    assert len(local_radio[0]) == 4
    assert entry.runtime_data.coordinator.status == "ok"
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    assert hass.states.get("sensor.aiper_ble_battery").state == "73"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["polling"]["transport"] == "local_bluez"
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


@pytest.mark.parametrize("failure", ["Connect", "StopNotify", "Disconnect"])
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
