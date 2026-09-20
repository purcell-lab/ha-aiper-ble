"""Native trace uses the existing coordinator mutex, cooldown and suspension."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import PROXY_TRACE_SERVICE

from .test_isolated_services import CONFIRMS
from .test_polling import PATH, response, setup

FIELDS = {**CONFIRMS, "confirm_proxy_logging": True, "proxy_entry_id": "test_proxy"}


async def invoke(hass, entry, fields=None):
    return await hass.services.async_call(
        DOMAIN,
        PROXY_TRACE_SERVICE,
        {"entry_id": entry.entry_id, **(FIELDS if fields is None else fields)},
        blocking=True,
        return_response=True,
    )


async def complete(hass, target, report, query, entry_id):
    assert entry_id == "test_proxy"
    assert query.query_type == "OpInfo"
    report.update(
        status="query_complete",
        cleanup="disconnected_confirmed",
        notification_cleanup="stop_confirmed",
        protocol_response=response("OpInfo"),
        proxy_trace={"events": [], "log_cleanup": "dedicated_connection_closed"},
    )


async def test_service_overrides_local_option_without_mutating_or_publishing(hass):
    entry = await setup(hass, {"use_local_adapter": True})
    options = dict(entry.options)
    with patch(f"{PATH}.proxy_trace.query_once", side_effect=complete) as query:
        result = await invoke(hass, entry)
        assert result["status"] == "query_complete"
        assert result["mode"] == "isolated_ha_proxy_trace"
        assert result["updates_entities"] is False
        assert "protocol_response" not in result
        assert not entry.runtime_data.coordinator.data
        assert dict(entry.options) == options
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await invoke(hass, entry)
        assert query.await_count == 1


@pytest.mark.parametrize("key", list(CONFIRMS) + ["confirm_proxy_logging"])
async def test_all_confirmations_required(hass, key):
    entry = await setup(hass, {})
    with patch(f"{PATH}.proxy_trace.query_once") as query:
        with pytest.raises(HomeAssistantError):
            await invoke(hass, entry, {**FIELDS, key: False})
        query.assert_not_called()


@pytest.mark.parametrize("condition", ["enabled", "closing", "suspended", "busy"])
async def test_existing_preflight_guards(hass, condition):
    entry = await setup(hass, {})
    runtime = entry.runtime_data
    if condition == "enabled":
        runtime.coordinator.enabled = True
    elif condition == "closing":
        runtime.closing = True
    elif condition == "suspended":
        runtime.coordinator.suspended = True
    else:
        runtime.task = asyncio.current_task()
    with patch(f"{PATH}.proxy_trace.query_once") as query:
        with pytest.raises(HomeAssistantError):
            await invoke(hass, entry)
        query.assert_not_called()
    runtime.task = None


async def test_uncertain_logging_cleanup_suspends_and_discards_values(hass):
    entry = await setup(hass, {})

    async def uncertain(*args):
        await complete(*args)
        args[2]["proxy_trace"]["log_cleanup"] = "unconfirmed"

    with patch(f"{PATH}.proxy_trace.query_once", side_effect=uncertain):
        result = await invoke(hass, entry)
    assert result["status"] == "suspended"
    assert result["values"] == {}
    assert entry.runtime_data.coordinator.suspended


async def test_unload_cancels_query_and_keeps_cleanup_report(hass):
    entry = await setup(hass, {})
    started = asyncio.Event()

    async def stalled(hass, target, report, query, entry_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            report["proxy_trace"] = {"log_cleanup": "dedicated_connection_closed"}

    with patch(f"{PATH}.proxy_trace.query_once", side_effect=stalled):
        task = asyncio.create_task(invoke(hass, entry))
        await started.wait()
        await entry.runtime_data.async_close()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert entry.runtime_data.last_result["status"] == "interrupted"
    assert (
        entry.runtime_data.last_result["proxy_trace"]["log_cleanup"]
        == "dedicated_connection_closed"
    )
    assert entry.runtime_data.task is None
