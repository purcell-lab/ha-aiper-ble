"""Independent production-transport queries never publish partial cycles."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import ISOLATED_QUERIES
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.protocol import Query, query_frame

from .test_bluetooth import radio as radio
from .test_poll_service import expire_cooldown
from .test_polling import PATH, response, setup
from .test_polling import transport as transport
from .test_protocol import frame

CONFIRMS = {
    "confirm_app_closed": True,
    "confirm_query_write": True,
    "confirm_notifications": True,
}


async def call_query(hass, entry, service="query_info", *, confirms=None, reply=True):
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"entry_id": entry.entry_id, **(CONFIRMS if confirms is None else confirms)},
        blocking=True,
        return_response=reply,
    )


@pytest.mark.parametrize("service,query_type", ISOLATED_QUERIES.items())
async def test_exact_production_proxy_request(hass, radio, service, query_type):
    entry = await setup(hass, {})
    entry.runtime_data.target = replace(
        entry.runtime_data.target, adapter_path=None, adapter_address=None
    )
    radio.mutations.append(
        lambda client: setattr(client, "reply", frame(response(query_type)))
    )
    result = await call_query(hass, entry, service)
    assert result["query_type"] == query_type
    assert result["status"] == "query_complete"
    assert result["phase"] == "verify_response"
    assert result["cleanup"] == "disconnected_confirmed"
    assert result["notification_cleanup"] == "stop_confirmed"
    assert result["values"]
    assert not result["updates_entities"]
    assert len(radio.clients) == 1
    client = radio.clients[0]
    assert bytes(client.written) == query_frame(
        Query("omit_empty_crc", "request", query_type=query_type)
    )
    assert client.calls.count("connect") == 1
    assert client.calls.count("stop") == 1
    assert client.calls.count("disconnect") == 1
    assert not client.is_connected
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
    assert (
        hass.states.get("sensor.aiper_ble_last_successful_poll").state == "unavailable"
    )
    assert entry.runtime_data.coordinator.data is None
    assert entry.runtime_data.coordinator.status == "disabled"
    assert (
        hass.states.get("sensor.aiper_ble_discovery_result").state == "query_complete"
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["last_result"] == result
    assert "PRIVATE_SERIAL" not in json.dumps(result)
    assert "protocol_response" not in result
    assert "sn" not in result["values"]


async def test_cooldown_is_shared_by_all_four_actions_and_poll_now(hass, transport):
    entry = await setup(hass, {})
    coordinator = entry.runtime_data.coordinator
    await call_query(hass, entry, "query_s1_info")
    for service in ISOLATED_QUERIES:
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await call_query(hass, entry, service)
    assert len(transport[0]) == 1
    coordinator.enabled = True
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await coordinator.async_poll_now()
    coordinator.enabled = False
    expire_cooldown(coordinator)
    assert (await call_query(hass, entry, "query_info"))["values"]["battery"] == 73
    assert len(transport[0]) == 2


@pytest.mark.parametrize("field", CONFIRMS)
@pytest.mark.parametrize("missing", [False, True])
async def test_confirmation_required(hass, transport, field, missing):
    entry = await setup(hass, {})
    confirms = dict(CONFIRMS)
    if missing:
        del confirms[field]
    else:
        confirms[field] = False
    with pytest.raises((HomeAssistantError, vol.Invalid)):
        await call_query(hass, entry, confirms=confirms)
    assert not transport[0]


async def test_recurring_polling_must_be_disabled(hass, transport):
    entry = await setup(hass)
    expire_cooldown(entry.runtime_data.coordinator)
    with pytest.raises(HomeAssistantError, match="Disable recurring polling"):
        await call_query(hass, entry)
    assert len(transport[0]) == 4


@pytest.mark.parametrize("gate", ["suspended", "closing"])
async def test_safety_gates(hass, transport, gate):
    entry = await setup(hass, {})
    if gate == "closing":
        entry.runtime_data.closing = True
    else:
        entry.runtime_data.coordinator.suspended = True
    with pytest.raises(HomeAssistantError):
        await call_query(hass, entry)
    assert not transport[0]


async def test_failed_response_preserves_diagnostic_not_partial_values(hass, radio):
    entry = await setup(hass, {})
    bad = response("INFO")
    bad["chksum"] ^= 1
    radio.mutations.append(lambda client: setattr(client, "reply", frame(bad)))
    result = await call_query(hass, entry)
    assert result["status"] == "failed"
    assert result["error_code"] == "response_checksum_mismatch"
    assert result["failure_stage"] == "verify_response"
    assert result["values"] == {}
    assert not entry.runtime_data.coordinator.last_update_success
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await call_query(hass, entry, "query_warn")


@pytest.mark.parametrize("failure", ["connect", "stop", "disconnect"])
async def test_transport_failure_cleanup_and_privacy(hass, radio, failure):
    entry = await setup(hass, {})
    radio.mutations.append(lambda client: setattr(client, "failure", failure))
    # Use first fixture response type; failure never needs INFO parsing.
    result = await call_query(hass, entry, "query_s1_info")
    assert result["values"] == {}
    assert "PRIVATE_BACKEND_IDENTIFIER" not in json.dumps(result)
    assert len(radio.clients) == 1
    coordinator = entry.runtime_data.coordinator
    if failure == "connect":
        assert result["status"] == "failed"
        assert result["failure_stage"] == "connect"
        assert not coordinator.suspended
        assert result["write_attempts"] == 0
    else:
        assert result["status"] == "suspended"
        assert result["error_code"] == "cleanup_requires_review"
        assert coordinator.suspended
        expire_cooldown(coordinator)
        with pytest.raises(HomeAssistantError, match="suspended"):
            await call_query(hass, entry)


async def test_failure_without_response_raises(hass, transport):
    entry = await setup(hass, {})
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    with pytest.raises(HomeAssistantError, match="See integration diagnostics"):
        await call_query(hass, entry, reply=False)
    assert len(transport[0]) == 1
    assert entry.runtime_data.last_result["status"] == "failed"


async def test_mutex_and_unload_cancel_cleanup(hass, transport):
    entry = await setup(hass, {})
    runtime = entry.runtime_data
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def stalled(_hass, target, report, query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            report.update(
                cleanup="disconnected_confirmed", notification_cleanup="stop_confirmed"
            )
            cleaned.set()

    with patch(f"{PATH}.coordinator.query_once", stalled):
        running = asyncio.create_task(call_query(hass, entry))
        await started.wait()
        for service in ISOLATED_QUERIES:
            with pytest.raises(HomeAssistantError, match="already running"):
                await call_query(hass, entry, service)
        with pytest.raises(HomeAssistantError, match="already running"):
            await hass.services.async_call(
                DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
            )
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((asyncio.CancelledError, HomeAssistantError)):
            await running
    assert cleaned.is_set()
    assert runtime.task is None
    assert runtime.last_result["status"] == "interrupted"
    assert runtime.coordinator.last_attempt_finished is not None


async def test_timeout_records_cleanup_and_cooldown(hass, transport):
    entry = await setup(hass, {})

    async def stalled(_hass, target, report, query):
        report["phase"] = "connect"
        try:
            await asyncio.Event().wait()
        finally:
            report["cleanup"] = "disconnected_confirmed"

    with (
        patch(f"{PATH}.coordinator.query_once", stalled),
        patch(f"{PATH}.coordinator.SINGLE_QUERY_SECONDS", 0.01),
    ):
        result = await call_query(hass, entry)
    assert result["status"] == "failed"
    assert result["error_code"] == "timeout"
    assert result["cleanup"] == "disconnected_confirmed"
    assert result["values"] == {}
    assert entry.runtime_data.task is None
    assert entry.runtime_data.coordinator.last_attempt_finished is not None
