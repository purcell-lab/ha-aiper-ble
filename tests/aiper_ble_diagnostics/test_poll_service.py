"""Operator polling reuses bounded telemetry and stable device registration."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.aiper_ble_diagnostics.const import DOMAIN

from .helpers import TARGET
from .test_polling import PATH, response, setup
from .test_polling import transport as transport


async def call_poll(hass, entry, *, confirm=True, response=True):
    return await hass.services.async_call(
        DOMAIN,
        "poll_now",
        {"entry_id": entry.entry_id, "confirm_app_closed": confirm},
        blocking=True,
        return_response=response,
    )


def expire_cooldown(coordinator):
    coordinator.last_attempt_finished -= coordinator.interval + 1


async def test_poll_now_publishes_and_can_shorten_failure_backoff(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    coordinator.next_attempt = asyncio.get_running_loop().time() + 3600
    result = await call_poll(hass, entry)
    assert result["status"] == "ok"
    assert "last_successful_poll" in result
    assert len(transport[0]) == 8
    assert coordinator.next_attempt > asyncio.get_running_loop().time()
    assert coordinator.update_interval.total_seconds() == 300
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    expire_cooldown(coordinator)
    assert await call_poll(hass, entry, response=False) is None
    assert len(transport[0]) == 12


@pytest.mark.parametrize("gate", ["confirmation", "cooldown", "disabled", "suspended"])
async def test_poll_now_cannot_bypass_gates(hass, transport, gate):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    if gate != "cooldown":
        expire_cooldown(coordinator)
    if gate == "disabled":
        coordinator.enabled = False
    if gate == "suspended":
        coordinator.suspended = True
    before = coordinator.next_attempt
    with pytest.raises(HomeAssistantError):
        await call_poll(hass, entry, confirm=gate != "confirmation")
    assert len(transport[0]) == 4
    assert coordinator.next_attempt == before


async def test_poll_now_failure_invalidates_sensors_and_preserves_backoff(
    hass, transport
):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    result = await call_poll(hass, entry)
    assert result["status"] == "failed"
    assert result["consecutive_failures"] == 1
    assert "error_code" in result
    assert len(transport[0]) == 5
    assert coordinator.failures == 1
    assert coordinator.update_interval.total_seconds() == 600
    assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
    assert hass.states.get("sensor.aiper_ble_polling_status").state == "failed"
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await call_poll(hass, entry)
    assert len(transport[0]) == 5


async def test_poll_now_cleanup_failure_suspends_and_cannot_be_forced(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    transport[1].append(lambda bus: setattr(bus, "failure", "Disconnect"))
    assert (await call_poll(hass, entry))["status"] == "suspended"
    assert coordinator.suspended
    expire_cooldown(coordinator)
    with pytest.raises(HomeAssistantError, match="suspended"):
        await call_poll(hass, entry)
    assert len(transport[0]) == 5


async def test_repeated_manual_failure_publishes_count_and_recovers(hass, transport):
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.failures == 1  # Startup already failed.
    for failures in (2, 3):
        expire_cooldown(coordinator)
        transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
        result = await call_poll(hass, entry)
        assert result["status"] == "failed"
        assert result["consecutive_failures"] == failures
        state = hass.states.get("sensor.aiper_ble_polling_status")
        assert state.attributes["consecutive_failures"] == failures
        assert state.attributes["error_code"] == coordinator.error_code
        assert hass.states.get("sensor.aiper_ble_temperature").state == "unavailable"
        assert len(transport[0]) == failures  # No second query or retry.
        assert coordinator.update_interval.total_seconds() == 300 * 2**failures
    expire_cooldown(coordinator)
    assert (await call_poll(hass, entry))["status"] == "ok"
    assert coordinator.failures == 0
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    assert "error_code" not in coordinator.last_poll_details


async def test_failure_without_response_is_actionable_service_error(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    with pytest.raises(ServiceValidationError, match="See integration diagnostics"):
        await call_poll(hass, entry, response=False)
    assert len(transport[0]) == 5
    assert coordinator.failures == 1


async def test_rest_response_failure_is_structured_not_http_500(
    hass, hass_client, transport
):
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    transport[1].append(lambda bus: setattr(bus, "failure", "Connect"))
    assert await async_setup_component(hass, "api", {})
    client = await hass_client()
    response = await client.post(
        f"/api/services/{DOMAIN}/poll_now?return_response",
        json={"entry_id": entry.entry_id, "confirm_app_closed": True},
    )
    assert response.status == 200
    payload = (await response.json())["service_response"]
    assert payload["status"] == "failed"
    assert payload["consecutive_failures"] == 2
    assert payload["error_code"] == coordinator.error_code
    assert "last_successful_poll" not in payload
    assert len(transport[0]) == 2
    assert (
        hass.states.get("sensor.aiper_ble_polling_status").attributes[
            "consecutive_failures"
        ]
        == 2
    )


async def test_poll_now_mutex_and_unload_cancellation(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def stalled(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with patch(f"{PATH}.coordinator.query_once", stalled):
        running = asyncio.create_task(call_poll(hass, entry))
        await entered.wait()
        for service, data in (
            ("poll_now", {"confirm_app_closed": True}),
            ("preflight", {}),
        ):
            with pytest.raises(HomeAssistantError, match="already running"):
                await hass.services.async_call(
                    DOMAIN,
                    service,
                    {"entry_id": entry.entry_id, **data},
                    blocking=True,
                )
        # An automatic coordinator tick must not start a competing query.
        coordinator.next_attempt = 0
        await coordinator.async_refresh()
        assert len(transport[0]) == 4
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
            await running
        assert cleaned.is_set()
        assert coordinator.runtime.task is None


async def test_poll_now_proxy_only_entry(hass, transport):
    entry = await setup(hass)

    entry.runtime_data.target = replace(TARGET, adapter_path=None, adapter_address=None)
    coordinator = entry.runtime_data.coordinator
    expire_cooldown(coordinator)

    async def succeeded(_hass, target, report, query):
        assert target.adapter_path is None
        report.update(
            status="query_complete",
            cleanup="disconnected_confirmed",
            notification_cleanup="stop_confirmed",
            protocol_response=response(query.query_type),
        )

    with patch(f"{PATH}.coordinator.query_once", succeeded):
        assert (await call_poll(hass, entry))["status"] == "ok"


async def test_one_device_all_entities_stable_after_reload(hass, transport):
    entry = await setup(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    device = devices.async_get_device_by_identifier(
        (DOMAIN, TARGET.address), entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "Aiper"
    assert device.model == "Surfer S1"
    assert device.name == "Aiper Surfer S1 (BLE)"
    assert not device.connections  # No speculative merge with the cloud integration.
    records = er.async_entries_for_config_entry(entities, entry.entry_id)
    assert len(records) == 13
    assert {r.device_id for r in records} == {device.id}
    before = {(r.entity_id, r.unique_id) for r in records}
    assert "sensor.aiper_ble_temperature" in {r.entity_id for r in records}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, TARGET.address), entry.entry_id
        ).id
        == device.id
    )
    assert {
        (r.entity_id, r.unique_id)
        for r in er.async_entries_for_config_entry(entities, entry.entry_id)
    } == before
    entities.async_update_entity(
        "sensor.aiper_ble_temperature", new_entity_id="sensor.pool_robot_temperature"
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pool_robot_temperature").state == "21.5"
    assert entities.async_get("sensor.aiper_ble_temperature") is None
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 13
