"""Same-radio diagnostics retain HA lifecycle without changing global routing."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.backends.device import BLEDevice
from habluetooth.wrappers import HaBleakClientWrapper
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble_diagnostics import local_bleak as local
from custom_components.aiper_ble_diagnostics.bluetooth_transport import (
    query_once,
    single_attempt_client_class,
)
from custom_components.aiper_ble_diagnostics.coordinator import LOCAL_BLEAK_SERVICE
from custom_components.aiper_ble_diagnostics.protocol import ProtocolError
from custom_components.aiper_ble_diagnostics.transport_diagnostics import (
    TransportDiagnostics,
)

from .helpers import TARGET
from .test_bluetooth import radio as radio
from .test_isolated_services import CONFIRMS, call_query
from .test_polling import OP, PATH, response, setup


@pytest.fixture
def selected():
    device = BLEDevice(
        TARGET.address,
        TARGET.name,
        {
            "path": TARGET.device_path,
            "props": {"Adapter": TARGET.adapter_path, "Connected": False},
        },
    )
    scanner = SimpleNamespace(
        source=TARGET.adapter_address,
        connector=None,
        connectable=True,
        discovered_device_timestamps={TARGET.address: 99.5},
        get_allocations=Mock(return_value=SimpleNamespace(free=5)),
        connections_in_progress=Mock(return_value=0),
    )
    route = SimpleNamespace(
        scanner=scanner,
        ble_device=device,
        advertisement=SimpleNamespace(local_name=TARGET.name),
    )
    remote = SimpleNamespace(scanner=SimpleNamespace(source="other_proxy"))
    manager = SimpleNamespace(
        async_scanner_devices_by_address=Mock(return_value=[remote, route]),
        async_scanner_by_source=Mock(return_value=scanner),
        async_allocate_connection_slot=Mock(return_value=True),
        async_release_connection_slot=Mock(),
    )
    diagnostics = TransportDiagnostics({}, "ha_bluetooth_local")
    with patch.object(local, "monotonic_time_coarse", return_value=100.0):
        cls = local.pinned_client_class(
            HaBleakClientWrapper, TARGET, diagnostics, dict(local.SUPPORTED_VERSIONS)
        )
        # Selection does not need constructor state, scanner startup or sockets.
        client = object.__new__(cls)
        yield SimpleNamespace(
            client=client,
            manager=manager,
            route=route,
            device=device,
            scanner=scanner,
            diagnostics=diagnostics,
        )


def choose(selected):
    return selected.client._async_get_best_available_backend_and_device(
        selected.manager
    )


def test_selects_saved_local_not_first_remote_and_retains_ha_lifecycle(selected):
    backend = choose(selected)
    assert backend.client is BleakClientBlueZDBus
    assert backend.device is selected.device
    assert backend.scanner is selected.scanner
    assert not backend.source
    selected.manager.async_allocate_connection_slot.assert_called_once_with(
        selected.device
    )
    assert type(selected.client).connect is HaBleakClientWrapper.connect
    assert type(selected.client).disconnect is HaBleakClientWrapper.disconnect
    assert selected.diagnostics.data["local_route_asserted"] is True
    assert selected.diagnostics.data["backend"] == "bleak_bluez"
    assert selected.diagnostics.data["selected_route"] == "route_1"
    public = json.dumps(selected.diagnostics.data)
    for private in (TARGET.address, TARGET.adapter_address, TARGET.name, "/org/bluez"):
        assert private not in public
    assert HaBleakClientWrapper._async_get_best_available_backend_and_device is not (
        type(selected.client)._async_get_best_available_backend_and_device
    )


@pytest.mark.parametrize(
    "condition",
    [
        "no_route",
        "duplicate",
        "wrong_path",
        "wrong_adapter",
        "wrong_address",
        "wrong_name",
        "remote_source",
        "connector",
        "passive",
        "unregistered",
        "stale",
        "future",
        "no_timestamp",
        "no_slots",
        "in_progress",
        "allocation_refused",
        "Connected",
        "Paired",
        "Bonded",
        "Trusted",
        "Blocked",
    ],
)
def test_invalid_route_fails_before_connect_without_proxy_fallback(selected, condition):
    route, scanner, manager = selected.route, selected.scanner, selected.manager
    if condition == "no_route":
        manager.async_scanner_devices_by_address.return_value = []
    elif condition == "duplicate":
        manager.async_scanner_devices_by_address.return_value = [route, route]
    elif condition == "wrong_path":
        route.ble_device.details["path"] = "/org/bluez/hci1/dev_other"
    elif condition == "wrong_adapter":
        route.ble_device.details["props"]["Adapter"] = "/org/bluez/hci1"
    elif condition == "wrong_address":
        route.ble_device.address = "00:00:00:00:00:00"
    elif condition == "wrong_name":
        route.advertisement.local_name = "different_robot"
    elif condition == "remote_source":
        route.ble_device.details["source"] = "other_proxy"
    elif condition == "connector":
        scanner.connector = object()
    elif condition == "passive":
        scanner.connectable = False
    elif condition == "unregistered":
        manager.async_scanner_by_source.return_value = object()
    elif condition in {"stale", "future", "no_timestamp"}:
        scanner.discovered_device_timestamps[TARGET.address] = {
            "stale": 89,
            "future": 101,
            "no_timestamp": None,
        }[condition]
    elif condition == "no_slots":
        scanner.get_allocations.return_value.free = 0
    elif condition == "in_progress":
        scanner.connections_in_progress.return_value = 1
    elif condition == "allocation_refused":
        manager.async_allocate_connection_slot.return_value = False
    else:
        route.ble_device.details["props"][condition] = True
    with pytest.raises(ProtocolError):
        choose(selected)
    assert not selected.diagnostics.data.get("local_route_asserted")
    if condition != "allocation_refused":
        manager.async_allocate_connection_slot.assert_not_called()


def test_unexpected_backend_releases_newly_allocated_slot(selected):
    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=(object, "unexpected"),
    ):
        with pytest.raises(ProtocolError, match="route_mismatch"):
            choose(selected)
    selected.manager.async_release_connection_slot.assert_called_once_with(
        selected.device
    )


@pytest.mark.parametrize("package", local.SUPPORTED_VERSIONS)
def test_unreviewed_runtime_versions_refused(package):
    versions = dict(local.SUPPORTED_VERSIONS)
    versions[package] = "future"
    with pytest.raises(ProtocolError, match="wrapper_unsupported"):
        local.pinned_client_class(
            HaBleakClientWrapper, TARGET, TransportDiagnostics({}, "test"), versions
        )


def test_unwrapped_client_and_proxy_only_target_refused():
    with pytest.raises(ProtocolError, match="wrapper_unsupported"):
        local.pinned_client_class(
            object, TARGET, TransportDiagnostics({}, "test"), local.SUPPORTED_VERSIONS
        )
    with pytest.raises(ProtocolError, match="local_adapter_required"):
        local.pinned_client_class(
            HaBleakClientWrapper,
            replace(TARGET, adapter_path=None, adapter_address=None),
            TransportDiagnostics({}, "test"),
            local.SUPPORTED_VERSIONS,
        )


def test_assertion_checks_actual_backend_and_connected_route(selected):
    client = SimpleNamespace(
        _backend=object.__new__(BleakClientBlueZDBus),
        _connected_scanner=selected.scanner,
        _connected_device=selected.device,
    )
    local.assert_local_backend(client, TARGET)
    for attr in ("_backend", "_connected_scanner", "_connected_device"):
        original = getattr(client, attr)
        setattr(client, attr, None)
        with pytest.raises(ProtocolError, match="route_mismatch"):
            local.assert_local_backend(client, TARGET)
        setattr(client, attr, original)


async def test_actual_route_mismatch_disconnects_before_notify_or_write(hass, radio):
    # Existing fake client deliberately lacks a local backend.
    with patch.object(local, "pinned_client_class", side_effect=lambda base, *a: base):
        report = {}
        await query_once(hass, TARGET, report, OP, pin_local=True)
    assert report["status"] == "failed"
    assert report["error_code"] == "local_bleak_route_mismatch"
    assert report["write_attempts"] == 0
    assert radio.clients[0].calls == ["connect", "disconnect"]
    assert report["cleanup"] == "disconnected_confirmed"


async def test_route_change_after_subscription_stops_without_writing(hass, radio):
    with (
        patch.object(local, "pinned_client_class", side_effect=lambda base, *a: base),
        patch.object(
            local,
            "assert_local_backend",
            side_effect=[None, ProtocolError("local_bleak_route_mismatch")],
        ),
    ):
        report = {}
        await query_once(hass, TARGET, report, OP, pin_local=True)
    assert report["error_code"] == "local_bleak_route_mismatch"
    assert report["write_attempts"] == 0
    assert radio.clients[0].calls == ["connect", "start", "stop", "disconnect"]
    assert report["cleanup"] == "disconnected_confirmed"
    assert report["notification_cleanup"] == "stop_confirmed"


async def test_same_radio_action_shared_gates_and_no_partial_publication(hass):
    entry = await setup(hass, {"use_local_adapter": True})
    executor = AsyncMock()

    async def execute(hass, target, report, query):
        assert query.query_type == "OpInfo"
        report.update(
            transport="ha_bluetooth_local",
            status="query_complete",
            cleanup="disconnected_confirmed",
            notification_cleanup="stop_confirmed",
            protocol_response=response("OpInfo"),
        )

    executor.side_effect = execute
    with patch(f"{PATH}.coordinator.local_bleak_query_once", executor):
        result = await call_query(hass, entry, LOCAL_BLEAK_SERVICE)
        assert result["status"] == "query_complete"
        assert result["mode"] == "isolated_ha_bluetooth_local_query"
        assert result["values"] == {"wifi_rssi_raw": -127}
        assert not result["updates_entities"]
        assert entry.runtime_data.coordinator.use_local_adapter
        assert entry.runtime_data.coordinator.data is None
        for service in (LOCAL_BLEAK_SERVICE, "query_opinfo"):
            with pytest.raises(HomeAssistantError, match="cooldown"):
                await call_query(hass, entry, service)
        executor.assert_awaited_once()


@pytest.mark.parametrize("field", CONFIRMS)
async def test_same_radio_action_requires_every_confirmation(hass, field):
    entry = await setup(hass, {})
    with patch(f"{PATH}.coordinator.local_bleak_query_once") as execute:
        with pytest.raises(HomeAssistantError, match="Confirm"):
            await call_query(
                hass, entry, LOCAL_BLEAK_SERVICE, confirms={**CONFIRMS, field: False}
            )
        execute.assert_not_called()


@pytest.mark.parametrize("gate", ["enabled", "suspended", "closing"])
async def test_same_radio_action_refuses_unsafe_coordinator_state(hass, gate):
    entry = await setup(hass, {})
    if gate == "closing":
        entry.runtime_data.closing = True
    else:
        setattr(entry.runtime_data.coordinator, gate, True)
    with patch(f"{PATH}.coordinator.local_bleak_query_once") as execute:
        with pytest.raises(HomeAssistantError):
            await call_query(hass, entry, LOCAL_BLEAK_SERVICE)
        execute.assert_not_called()


async def test_same_radio_unload_cancels_and_keeps_cleanup_cooldown(hass):
    entry = await setup(hass, {})
    started = asyncio.Event()

    async def stalled(hass, target, report, query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            report.update(
                cleanup="disconnected_confirmed", notification_cleanup="stop_confirmed"
            )

    with patch(f"{PATH}.coordinator.local_bleak_query_once", stalled):
        running = asyncio.create_task(call_query(hass, entry, LOCAL_BLEAK_SERVICE))
        await started.wait()
        with pytest.raises(HomeAssistantError, match="already running"):
            await call_query(hass, entry, "query_opinfo")
        runtime = entry.runtime_data
        await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((asyncio.CancelledError, HomeAssistantError)):
            await running
        assert runtime.last_result["status"] == "interrupted"
        assert runtime.last_result["cleanup"] == "disconnected_confirmed"
        assert runtime.coordinator.last_attempt_finished is not None


async def test_pinned_subclass_preserves_single_attempt_guard(selected):
    class Wrapper(HaBleakClientWrapper):
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self, **kwargs):
            return None

    with patch(
        f"{PATH}.bluetooth_transport.bleak_retry_connector.BleakClientWithServiceCache",
        Wrapper,
    ):
        base = single_attempt_client_class(selected.diagnostics)
        cls = local.pinned_client_class(
            base, TARGET, selected.diagnostics, local.SUPPORTED_VERSIONS
        )
        owners = []
        client = cls(owners=owners)
        await client.connect(timeout=20.0)
        with pytest.raises(ProtocolError, match="connection_retry_refused"):
            await client.connect(timeout=20.0)
        assert owners == [client]
        assert selected.diagnostics.data["connect_calls_observed"] == 1
        assert selected.diagnostics.data["retry_calls_refused"] == 1
