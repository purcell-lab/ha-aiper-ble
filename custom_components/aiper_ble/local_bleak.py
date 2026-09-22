"""Explicit same-radio diagnostic using HA's normal Bleak connection lifecycle.

This version-gated private selector is per client, never a global patch. It is
not a production routing option or an automatic fallback.
"""

import asyncio
from importlib.metadata import version
from typing import Any

from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth.wrappers import HaBleakClientWrapper
from homeassistant.core import HomeAssistant

from .probe import Target, open_bluez
from .protocol import Control, ProtocolError, Query
from .transport_diagnostics import TransportDiagnostics

SUPPORTED_VERSIONS = {
    "habluetooth": "6.26.11",
    "bleak": "3.0.2",
    "bleak-retry-connector": "4.7.0",
}
MAX_ADVERTISEMENT_AGE = 10


def runtime_versions() -> dict[str, str]:
    """Read package metadata in HA's executor, never block its event loop."""
    return {name: version(name) for name in SUPPORTED_VERSIONS}


def assert_local_backend(client: Any, target: Target) -> None:
    """Fail before subscription/write if HA did not retain the expected radio."""
    device = getattr(client, "_connected_device", None)
    scanner = getattr(client, "_connected_scanner", None)
    if (
        not isinstance(getattr(client, "_backend", None), BleakClientBlueZDBus)
        or getattr(scanner, "source", None) != target.adapter_address
        or device is None
        or device.address.upper() != target.address
        or not isinstance(device.details, dict)
        or device.details.get("path") != target.device_path
        or device.details.get("source")
    ):
        raise ProtocolError("local_bleak_route_mismatch")


def pinned_client_class(
    base: type[Any],
    target: Target,
    diagnostics: TransportDiagnostics,
    versions: dict[str, str],
) -> type[Any]:
    """Retain HA slot accounting, callbacks and cleanup, changing selection only."""
    if target.adapter_path is None or target.adapter_address is None:
        raise ProtocolError("local_adapter_required")
    diagnostics.data["runtime_versions"] = versions
    if (
        versions != SUPPORTED_VERSIONS
        or not issubclass(base, HaBleakClientWrapper)
        or not callable(getattr(base, "_async_get_backend_for_ble_device", None))
    ):
        raise ProtocolError("local_bleak_wrapper_unsupported")

    # The base is resolved at call time, so mypy cannot see it as a class.
    class PinnedLocalClient(base):  # type: ignore[misc]
        """One explicitly authorised client restricted to the saved local radio."""

        def _async_get_best_available_backend_and_device(self, manager: Any) -> Any:
            routes = [
                route
                for route in manager.async_scanner_devices_by_address(
                    target.address, True
                )
                if route.scanner.source == target.adapter_address
            ]
            if len(routes) != 1:
                raise ProtocolError("local_bleak_route_unavailable")
            route = routes[0]
            scanner, device = route.scanner, route.ble_device
            details = device.details
            if (
                device.address.upper() != target.address
                or (route.advertisement.local_name or device.name) != target.name
                or not isinstance(details, dict)
                or details.get("source")
                or details.get("path") != target.device_path
                or details.get("props", {}).get("Adapter") != target.adapter_path
                or scanner.connector is not None
                or not scanner.connectable
                or manager.async_scanner_by_source(scanner.source) is not scanner
            ):
                raise ProtocolError("local_bleak_route_mismatch")
            props = details["props"]
            if any(
                props.get(key)
                for key in ("Paired", "Bonded", "Trusted", "Blocked", "Connected")
            ):
                raise ProtocolError("local_bleak_endpoint_busy_or_secured")
            timestamp = scanner.discovered_device_timestamps.get(target.address)
            if type(timestamp) not in (int, float) or not (
                0 <= monotonic_time_coarse() - timestamp <= MAX_ADVERTISEMENT_AGE
            ):
                raise ProtocolError("local_bleak_advertisement_stale")
            allocations = scanner.get_allocations()
            if (
                allocations is None
                or allocations.free < 1
                or scanner.connections_in_progress()
            ):
                raise ProtocolError("local_bleak_slot_unavailable")
            # The inherited helper allocates the local slot. Normal HA connect()
            # then owns its lifecycle, including release on failure/cancellation.
            backend = self._async_get_backend_for_ble_device(manager, scanner, device)
            if backend is None:
                raise ProtocolError("local_bleak_slot_unavailable")
            if (
                backend.source
                or backend.scanner is not scanner
                or backend.device is not device
                or backend.client is not BleakClientBlueZDBus
            ):
                manager.async_release_connection_slot(device)
                raise ProtocolError("local_bleak_route_mismatch")
            diagnostics.data.update(
                selected_route=diagnostics.route_id(scanner),
                backend="bleak_bluez",
                local_route_asserted=True,
            )
            return backend

    return PinnedLocalClient


async def query_once(
    hass: HomeAssistant,
    target: Target,
    report: dict[str, Any],
    query: Query | Control,
) -> None:
    """Use the common query lifecycle, not a second transport implementation."""
    from .bluetooth_transport import query_once as managed_query

    try:
        await managed_query(hass, target, report, query, pin_local=True)
    finally:
        diagnostic = report.get("transport_diagnostics", {})
        if diagnostic.get("connect_calls_observed", 0):
            # HA clears its backend after a failed connect. A false
            # client.is_connected alone is therefore not independent evidence.
            previous_status = report.get("status")
            previous_cleanup = report.get("cleanup")
            report.update(
                status="cleanup_requires_review", cleanup="disconnect_unconfirmed"
            )
            diagnostic["independent_local_cleanup"] = "unconfirmed"
            try:
                async with asyncio.timeout(7):
                    async with open_bluez(target) as api:
                        props = await api.device()
                if (
                    props.get("Connected") is not False
                    or props.get("Address") != target.address
                    or props.get("Adapter") != target.adapter_path
                ):
                    raise ProtocolError("local_cleanup_unconfirmed")
                diagnostic["independent_local_cleanup"] = "disconnected_confirmed"
                # Do not erase an existing stop-notify/disconnect failure.
                report.update(status=previous_status, cleanup=previous_cleanup)
            except Exception:  # noqa: BLE001 - no private bus exception prose
                report.setdefault("failure_stage", "independent_local_cleanup")
