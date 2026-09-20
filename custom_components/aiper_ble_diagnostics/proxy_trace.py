"""Explicit single-proxy query with a separate, bounded native log connection.

Optional ESPHome imports occur only when this diagnostic is invoked. Never
change HA's shared API subscription, firmware logger levels or production route.
"""

import asyncio
import re
from importlib.metadata import version
from ipaddress import ip_address

from aioesphomeapi import APIClient, LogLevel
from bleak_esphome.backend.client import ESPHomeClient
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth import get_manager
from habluetooth.wrappers import HaBleakClientWrapper
from homeassistant.util import dt as dt_util

from .protocol import ProtocolError

SUPPORTED_VERSIONS = {
    "habluetooth": "6.26.11",
    "bleak": "3.0.2",
    "bleak-retry-connector": "4.7.0",
    "bleak-esphome": "4.0.0",
    "aioesphomeapi": "46.2.0",
}
MAX_EVENTS = 128
MAX_LINES = 2048
MAX_BYTES = 262144
MAX_LINE_BYTES = 2048
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADER = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\])?"
    r"\[[EWIDV]\]\[esp32_ble_client:\d+\]: "
    r"\[\d+\] \[([0-9A-Fa-f:]{17})\] (.*)$"
)
# Exact, source-reviewed messages. No free text, addresses or payloads survive.
EVENTS = (
    (r"0x[0-9a-fA-F]{2} Connecting", "connecting", ()),
    (r"Connection open", "connection_open", ()),
    (r"Connection open error, status=(\d+)", "open_error", ("status",)),
    (r"Searching for services", "services_start", ()),
    (r"ESP_GATTC_(OPEN|CONNECT|SEARCH_CMPL|CLOSE)_EVT", "gatt_event", ("stage",)),
    (r"cfg_mtu status (\d+), mtu (\d+)", "mtu", ("status", "mtu")),
    (r"cfg_mtu failed, mtu (\d+), status (\d+)", "mtu_failed", ("mtu", "status")),
    (r"ESP_GATTC_DISCONNECT_EVT, reason 0x([0-9a-fA-F]{2})", "disconnect", ("reason",)),
    (r"Service discovery complete", "services_complete", ()),
    (r"Disconnecting \(conn_id: \d+\)\.", "disconnecting", ()),
    (r"Remote closed during discovery", "remote_closed_during_discovery", ()),
    (r"Disconnect before connected, disconnect scheduled", "disconnect_scheduled", ()),
    (
        r"Disconnect requested, but already (IDLE|DISCONNECTING)",
        "disconnect_state",
        ("stage",),
    ),
    (r"DISCONNECT_EVT after CLOSE_EVT, already IDLE", "already_idle", ()),
    (
        r"Timeout waiting for CLOSE_EVT after disconnect, forcing IDLE",
        "close_timeout",
        (),
    ),
    (r"esp_ble_gattc_open error, status=(\d+)", "open_error", ("status",)),
)


def runtime_versions():
    """Executor-only runtime package inventory."""
    return {name: version(name) for name in SUPPORTED_VERSIONS}


class EventCapture:
    """Discard raw text immediately; retain a fixed event vocabulary only."""

    def __init__(self, address):
        self.address = address
        self.started = asyncio.get_running_loop().time()
        self.accepting = True
        self.data = {
            "clock": "HA receipt UTC and monotonic offset, not MCU event time",
            "events": [],
            "lines_seen": 0,
            "bytes_seen": 0,
            "capture_limit_reached": False,
            "coverage": "inconclusive_if_events_missing",
        }

    def receive(self, response):
        if not self.accepting:
            return
        raw = response.message
        self.data["lines_seen"] += 1
        self.data["bytes_seen"] += len(raw)
        if (
            self.data["lines_seen"] > MAX_LINES
            or self.data["bytes_seen"] > MAX_BYTES
            or len(self.data["events"]) >= MAX_EVENTS
        ):
            self.data["capture_limit_reached"] = True
            self.accepting = False
            return
        if len(raw) > MAX_LINE_BYTES:
            return
        text = ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
        match = HEADER.fullmatch(text)
        if match is None or match[1].upper() != self.address:
            return
        for pattern, event, fields in EVENTS:
            parsed = re.fullmatch(pattern, match[2])
            if parsed is None:
                continue
            item = {
                "event": event,
                "received_utc": dt_util.utcnow().isoformat(),
                "offset_ms": round(
                    (asyncio.get_running_loop().time() - self.started) * 1000, 3
                ),
            }
            for field, value in zip(fields, parsed.groups(), strict=True):
                item[field] = (
                    value
                    if field == "stage"
                    else int(value, 16 if field == "reason" else 10)
                )
            self.data["events"].append(item)
            return


def resolve_proxy(hass, entry_id):
    """Resolve an already-loaded ESPHome proxy; never return credentials."""
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime = getattr(entry, "runtime_data", None)
    if (
        entry is None
        or entry.domain != "esphome"
        or not getattr(runtime, "available", False)
        or not getattr(runtime, "bluetooth_device", None)
        or not getattr(runtime, "device_info", None)
        or not runtime.client.is_connected
    ):
        raise ProtocolError("proxy_entry_unavailable")
    info = runtime.device_info
    source = info.bluetooth_mac_address or info.mac_address
    if (
        not source
        or runtime.bluetooth_device.mac_address != source
        or info.esphome_version != "2026.9.0"
    ):
        raise ProtocolError("proxy_firmware_or_identity_unsupported")
    return entry, runtime, source


def select_route(manager, target, source):
    """Require exactly one fresh, entirely idle, registered proxy route."""
    routes = [
        route
        for route in manager.async_scanner_devices_by_address(target.address, True)
        if route.scanner.source == source
    ]
    if len(routes) != 1:
        raise ProtocolError("proxy_trace_route_unavailable")
    route = routes[0]
    scanner, device = route.scanner, route.ble_device
    if (
        device.address.upper() != target.address
        or (route.advertisement.local_name or device.name) != target.name
        or not isinstance(device.details, dict)
        or device.details.get("source") != source
        or not scanner.connectable
        or not scanner.connector
        or manager.async_scanner_by_source(source) is not scanner
    ):
        raise ProtocolError("proxy_trace_route_mismatch")
    stamp = scanner.discovered_device_timestamps.get(target.address)
    if type(stamp) not in (float, int) or not (
        0 <= monotonic_time_coarse() - stamp <= 10
    ):
        raise ProtocolError("proxy_trace_advertisement_stale")
    slots = scanner.get_allocations()
    if (
        slots is None
        or slots.free < 1
        or slots.free != slots.slots
        or slots.allocated
        or scanner.connections_in_progress()
    ):
        raise ProtocolError("proxy_trace_not_idle")
    return route


def pinned_client_class(base, target, source, diagnostics):
    """Change selection only for this client, preserving HA lifecycle."""
    if not issubclass(base, HaBleakClientWrapper):
        raise ProtocolError("proxy_trace_wrapper_unsupported")

    class PinnedProxyClient(base):
        def _async_get_best_available_backend_and_device(self, manager):
            route = select_route(manager, target, source)
            backend = self._async_get_backend_for_ble_device(
                manager, route.scanner, route.ble_device
            )
            if (
                backend is None
                or backend.source != source
                or backend.scanner is not route.scanner
                or backend.device is not route.ble_device
            ):
                raise ProtocolError("proxy_trace_backend_mismatch")
            diagnostics.data.update(
                selected_route=diagnostics.route_id(route.scanner),
                proxy_route_asserted=True,
            )
            return backend

    return PinnedProxyClient


def assert_proxy_backend(client, target, source):
    """Reject any route drift before notifications and again before writes."""
    backend = getattr(client, "_backend", None)
    device = getattr(client, "_connected_device", None)
    if (
        not isinstance(backend, ESPHomeClient)
        or backend._source != source
        or getattr(getattr(client, "_connected_scanner", None), "source", None)
        != source
        or device is None
        or device.address.upper() != target.address
        or device.details.get("source") != source
    ):
        raise ProtocolError("proxy_trace_backend_mismatch")


async def query_once(hass, target, report, query, entry_id):
    """One fixed OpInfo query, with logging on a separate API connection."""
    from .bluetooth_transport import query_once as managed_query

    versions = await hass.async_add_executor_job(runtime_versions)
    if versions != SUPPORTED_VERSIONS:
        raise ProtocolError("proxy_trace_versions_unsupported")
    entry, runtime, source = resolve_proxy(hass, entry_id)
    manager = get_manager()
    route = select_route(manager, target, source)
    info = runtime.device_info
    # Reuse the address of the verified live API connection. Requiring a
    # numeric address avoids creating a private mDNS scanner/socket lifecycle.
    try:
        address = str(ip_address(runtime.client.connected_address))
    except TypeError, ValueError:
        raise ProtocolError("proxy_trace_address_unavailable") from None
    # Use a separate connection: subscription levels are per API connection.
    # Secrets stay inside HA; no service input/output, logs or copied config.
    client = APIClient(
        address,
        entry.data["port"],
        entry.data.get("password"),
        noise_psk=entry.data.get("noise_psk"),
        expected_name=info.name,
        expected_mac=info.mac_address.replace(":", "").lower(),
        client_info="Aiper bounded diagnostic",
        provide_time=False,
    )
    client.set_debug(False)
    capture = EventCapture(target.address)
    trace = report["proxy_trace"] = capture.data
    trace.update(runtime_versions=versions, log_cleanup="not_started")
    unsubscribe = None
    subscription_attempted = False

    def guard():
        current_entry, current_runtime, current_source = resolve_proxy(hass, entry_id)
        if (
            not client.is_connected
            or current_entry is not entry
            or current_runtime is not runtime
            or current_source != source
        ):
            raise ProtocolError("proxy_trace_session_changed")
        if capture.data["capture_limit_reached"]:
            raise ProtocolError("proxy_trace_capture_limit")

    try:
        async with asyncio.timeout(12):
            await client.connect(login=True, log_errors=False)
            observed = await client.device_info()
        if (
            observed.mac_address != info.mac_address
            or (observed.bluetooth_mac_address or observed.mac_address) != source
            or observed.esphome_version != info.esphome_version
        ):
            raise ProtocolError("proxy_trace_log_identity_mismatch")
        # No dump_config, log-level service or firmware mutation.
        subscription_attempted = True
        unsubscribe = client.subscribe_logs(
            capture.receive, log_level=LogLevel.LOG_LEVEL_DEBUG, dump_config=False
        )
        trace["log_cleanup"] = "pending"
        # Same-connection round trip orders the subscription before BLE starts.
        # It is not an acknowledgement that DEBUG events are available.
        async with asyncio.timeout(3):
            await client.device_info()
        guard()
        trace["query_started_utc"] = dt_util.utcnow().isoformat()
        await managed_query(
            hass, target, report, query, proxy_source=source, proxy_guard=guard
        )
        # A short, bounded tail catches close/slot events after HA cleanup.
        await asyncio.sleep(2)
    finally:
        capture.accepting = False
        # Always disconnect our own socket, even if subscription setup failed.
        # LOG_LEVEL_NONE is sent only on this dedicated connection.
        trace["log_disable_sent"] = False
        try:
            if subscription_attempted and client.is_connected:
                client.subscribe_logs(
                    lambda _: None,
                    log_level=LogLevel.LOG_LEVEL_NONE,
                    dump_config=False,
                )()
                trace["log_disable_sent"] = True
        except Exception:  # noqa: BLE001 - socket teardown below is authoritative
            pass
        finally:
            if unsubscribe:
                try:
                    unsubscribe()
                except Exception:  # noqa: BLE001 - still close our socket
                    pass
            try:
                async with asyncio.timeout(5):
                    await client.disconnect(force=True)
                if client.is_connected:
                    raise ProtocolError("proxy_trace_log_disconnect_unconfirmed")
                trace["log_cleanup"] = "dedicated_connection_closed"
            except Exception:  # noqa: BLE001 - never expose network/credential prose
                trace["log_cleanup"] = "unconfirmed"
                report.update(
                    status="cleanup_requires_review",
                    error_code="cleanup_requires_review",
                )
        trace["finished_utc"] = dt_util.utcnow().isoformat()
        # Snapshot, not proof of an acknowledged device-side disconnect.
        try:
            slots = route.scanner.get_allocations()
            in_progress = route.scanner.connections_in_progress()
        except Exception:  # noqa: BLE001 - unknown slot state cannot prove cleanup
            slots = None
            in_progress = None
        trace["proxy_slots_after"] = (
            {
                "free": slots.free,
                "total": slots.slots,
                "allocated_count": len(slots.allocated),
            }
            if slots is not None
            else None
        )
        if report.get("transport_diagnostics", {}).get(
            "connect_calls_observed", 0
        ) and (
            not runtime.available
            or not runtime.client.is_connected
            or slots is None
            or slots.allocated
            or slots.free != slots.slots
            or in_progress is None
            or in_progress
        ):
            report.update(
                status="cleanup_requires_review", error_code="cleanup_requires_review"
            )
