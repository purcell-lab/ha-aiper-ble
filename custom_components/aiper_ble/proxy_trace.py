"""Explicit single-proxy query with a separate, bounded native log connection.

Optional ESPHome imports occur only when this diagnostic is invoked. Never
change HA's shared API subscription, firmware logger levels or production route.
"""

import asyncio
import re
from importlib.metadata import version
from ipaddress import ip_address
from typing import Any

from aioesphomeapi import APIClient, LogLevel
from bleak_esphome.backend.client import ESPHomeClient
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth import get_manager
from habluetooth.wrappers import HaBleakClientWrapper
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .probe import Target
from .protocol import Control, ProtocolError, Query
from .transport_diagnostics import TransportDiagnostics

SUPPORTED_VERSIONS = {
    "habluetooth": "6.26.11",
    "bleak": "3.0.2",
    "bleak-retry-connector": "4.7.0",
    "bleak-esphome": "4.0.0",
    "aioesphomeapi": "46.2.0",
}
# Exact firmware strings whose native log formats were source-reviewed: the
# 2026.9.0 bluetooth_connection backend, and the pre-2026.7 esp32_ble_client
# format used for the issue #7 regression comparison. Not a range.
SUPPORTED_FIRMWARE = ("2026.9.0", "2026.5.3", "2026.5.1")
MAX_EVENTS = 128
MAX_LINES = 2048
MAX_BYTES = 262144
MAX_LINE_BYTES = 2048
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADER = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\])?"
    r"\[(?:[EWICDV]|VV)\]\[esp32_ble_client:\d+\]"
    r"(?:\[[^\[\]\r\n\x00-\x1f]{1,32}\])?: "
    r"\[\d+\] \[([0-9A-Fa-f:]{17})\] (.*)$"
)
MODERN_HEADER = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\])?"
    r"\[(?:[EWICDV]|VV)\]\[(bluetooth_proxy|bluetooth_connection):\d+\]"
    r"(?:\[[^\[\]\r\n\x00-\x1f]{1,32}\])?: "
    r"\[(\d{1,2})\] (?:\[([0-9A-Fa-f:]{17})\] )?(.*)$"
)
# ESPHome 2026.9.0 proxy backend. Addressless messages require a preceding
# target-addressed connection request on this same slot; never infer ownership.
MODERN_EVENTS = (
    (r"0x(0[0-3]) Connecting", "connecting", ("address_type",)),
    (r"Connection open", "connection_open", ()),
    (r"Connection open failed, status=(\d+)", "open_error", ("status",)),
    (r"MTU exchange failed, status=(\d+)", "mtu_failed", ("status",)),
    (
        r"esp_ble_gattc_send_mtu_req failed, status=(\d+)",
        "mtu_request_failed",
        ("status",),
    ),
    (r"Service discovery complete", "services_complete", ()),
    (r"DISCONNECT_EVT reason=0x([0-9a-fA-F]{2,4})", "disconnect", ("reason",)),
    (r"Remote closed during discovery", "remote_closed_during_discovery", ()),
    (r"Disconnect scheduled", "disconnect_scheduled", ()),
    (r"Disconnecting \(conn_id: \d+\)", "disconnecting", ()),
    (r"Timeout waiting for teardown, forcing IDLE", "close_timeout", ()),
    (r"Connect rejected, slot busy", "slot_busy", ()),
    (r"Connect rejected, GATT app not registered", "gatt_unregistered", ()),
    (r"OPEN_EVT in IDLE state \(status=(\d+)\)", "late_open", ("status",)),
    (r"OPEN_EVT in unexpected state", "unexpected_open", ()),
    (
        r"Discovery finished, sending connected \(mtu=(\d+)\)",
        "connected_report",
        ("mtu",),
    ),
    (
        r"Connected with cached services, sending connected \(mtu=(\d+)\)",
        "cached_connected_report",
        ("mtu",),
    ),
    (
        r"Disconnected, reason=0x([0-9a-fA-F]{2,4}), freeing slot",
        "slot_freed",
        ("reason",),
    ),
    (r"Service discovery failed, err=(-?\d+)", "discovery_failed", ("status",)),
    (r"discover_services failed, err=(-?\d+)", "discovery_request_failed", ("status",)),
    (r"connect failed, err=(-?\d+)", "connect_rejected", ("status",)),
    (r"disconnect while backend idle, err=(-?\d+)", "backend_idle", ("status",)),
    (r"Connected reply deferred, TCP buffer full", "connected_reply_deferred", ()),
)
# Exact, source-reviewed messages. No free text, addresses or payloads survive.
EVENTS = (
    (r"0x(0[0-3]) Connecting", "connecting", ("address_type",)),
    (r"Connection open", "connection_open", ()),
    (r"Connection open error, status=(\d+)", "open_error", ("status",)),
    (r"Searching for services", "services_start", ()),
    (r"ESP_GATTC_(OPEN|CONNECT|SEARCH_CMPL|CLOSE)_EVT", "gatt_event", ("stage",)),
    (r"cfg_mtu status (\d+), mtu (\d+)", "mtu", ("status", "mtu")),
    (r"cfg_mtu failed, mtu (\d+), status (\d+)", "mtu_failed", ("mtu", "status")),
    (
        r"ESP_GATTC_DISCONNECT_EVT, reason 0x([0-9a-fA-F]{2,4})",
        "disconnect",
        ("reason",),
    ),
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
        self.bound_slot = None
        self.started = asyncio.get_running_loop().time()
        self.accepting = True
        self.data = {
            "clock": "HA receipt UTC and monotonic offset, not MCU event time",
            "events": [],
            "lines_seen": 0,
            "bytes_seen": 0,
            "capture_limit_reached": False,
            "coverage": "inconclusive_if_events_missing",
            "format_counts": {
                "oversized": 0,
                "header_unmatched": 0,
                "ble_tag_header_unmatched": 0,
                "other_device": 0,
                "target_header": 0,
                "target_message_unmatched": 0,
                "modern_header": 0,
                "unattributed_slot": 0,
            },
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
            self.data["format_counts"]["oversized"] += 1
            return
        text = ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
        match = HEADER.fullmatch(text)
        counts = self.data["format_counts"]
        if match is None:
            if self._receive_modern(text):
                return
            counts["header_unmatched"] += 1
            if "[esp32_ble_client:" in text:
                counts["ble_tag_header_unmatched"] += 1
            return
        if match[1].upper() != self.address:
            counts["other_device"] += 1
            return
        counts["target_header"] += 1
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
        counts["target_message_unmatched"] += 1

    def _receive_modern(self, text):
        match = MODERN_HEADER.fullmatch(text)
        if match is None:
            return False
        counts = self.data["format_counts"]
        counts["modern_header"] += 1
        tag, slot, address, body = match.groups()
        slot = int(slot)
        if address is not None and address.upper() != self.address:
            counts["other_device"] += 1
            if self.bound_slot == slot:
                self.bound_slot = None
            return True
        if tag == "bluetooth_proxy":
            if address is not None and body in {
                "Connecting v3 with cache",
                "Connecting v3 without cache",
            }:
                self.bound_slot = slot
                self._modern_event("proxy_connect_request", {}, "target_address")
            return True
        if address is None and self.bound_slot != slot:
            counts["unattributed_slot"] += 1
            return True
        counts["target_header"] += 1
        attribution = "target_address" if address is not None else "bound_slot"
        for pattern, event, fields in MODERN_EVENTS:
            parsed = re.fullmatch(pattern, body)
            if parsed is None:
                continue
            values = {
                field: int(value, 16 if field == "reason" else 10)
                for field, value in zip(fields, parsed.groups(), strict=True)
            }
            self._modern_event(event, values, attribution)
            if event in {
                "slot_freed",
                "close_timeout",
                "backend_idle",
                "connect_rejected",
                "open_error",
            }:
                self.bound_slot = None
            return True
        counts["target_message_unmatched"] += 1
        return True

    def _modern_event(self, event, values, attribution):
        self.data["events"].append(
            {
                "event": event,
                "attribution": attribution,
                "received_utc": dt_util.utcnow().isoformat(),
                "offset_ms": round(
                    (asyncio.get_running_loop().time() - self.started) * 1000, 3
                ),
                **values,
            }
        )


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
        or info.esphome_version not in SUPPORTED_FIRMWARE
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


def pinned_client_class(
    base: type[Any],
    target: Target,
    source: str,
    diagnostics: TransportDiagnostics,
) -> type[Any]:
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


def assert_proxy_backend(client: Any, target: Target, source: str) -> None:
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


async def query_once(
    hass: HomeAssistant,
    target: Target,
    report: dict[str, Any],
    query: Query | Control,
    entry_id: str,
) -> None:
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
    trace.update(
        runtime_versions=versions,
        # Allowlisted exact string only; interpretation of legacy versus
        # modern events depends on it.
        proxy_firmware=runtime.device_info.esphome_version,
        log_cleanup="not_started",
    )
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
