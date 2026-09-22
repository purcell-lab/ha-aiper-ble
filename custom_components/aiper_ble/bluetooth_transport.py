"""Fixed queries/explicit S1 controls over HA routing, including active proxies.

No private scanner, adapter pinning, pairing or command retries.
Remote backends do not expose BlueZ pairing/trust/notification ownership
metadata. Exclusive access remains an explicit operator prerequisite.
"""

import asyncio
from typing import Any

import bleak_retry_connector
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .probe import (
    EXPECTED_CHARACTERISTIC,
    EXPECTED_SERVICE,
    KEY_EXCHANGE_CHARACTERISTIC,
    Target,
)
from .protocol import (
    MAX_TOTAL_BYTES,
    Control,
    Decoder,
    ProtocolError,
    Query,
    chunks,
    protocol_hint,
    query_frame,
    query_telemetry,
    response_evidence,
)
from .transport_diagnostics import TransportDiagnostics, number

CONNECT_SECONDS = 30
EXCHANGE_SECONDS = 15
SECURITY_FLAGS = {
    "encrypt-write",
    "encrypt-authenticated-write",
    "secure-write",
    "authorize",
    "encrypt-read",
    "encrypt-authenticated-read",
    "secure-read",
}


def error_category(error: BaseException) -> str:
    """Fixed categories only: backend exception strings can contain secrets."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, BleakError):
        return "bleak"
    if isinstance(error, ConnectionError):
        return "connection"
    if isinstance(error, OSError):
        return "os"
    return "unexpected"


def single_attempt_client_class(
    diagnostics: TransportDiagnostics | None = None,
) -> type[bleak_retry_connector.BleakClientWithServiceCache]:
    """Resolve HA's replacement at call time, never capture an unwrapped base.

    HA patches bleak_retry_connector during Bluetooth setup. Our component may
    have been imported before that dependency finished loading.
    """

    class SingleAttemptClient(bleak_retry_connector.BleakClientWithServiceCache):
        """Retain failed/cancelled clients and prevent helper connect retries."""

        def __init__(self, *args: Any, owners: list[Any], **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.attempted = False
            owners.append(self)

        async def connect(self, **kwargs: Any) -> Any:
            if self.attempted:
                if diagnostics is not None:
                    diagnostics.data["retry_calls_refused"] += 1
                raise ProtocolError("connection_retry_refused")
            self.attempted = True
            if diagnostics is not None:
                diagnostics.data["connect_calls_observed"] += 1
                diagnostics.data["inner_connect_timeout_seconds"] = number(
                    kwargs.get("timeout")
                )
            try:
                return await super().connect(**kwargs)
            except Exception as exc:
                if diagnostics is not None:
                    diagnostics.error(exc, "client_connect")
                raise
            finally:
                if diagnostics is not None:
                    diagnostics.client(self)

    return SingleAttemptClient


def validate_routes(
    hass: HomeAssistant,
    target: Target,
    query: Query | Control,
    *,
    connected: bool = False,
) -> BLEDevice:
    """Validate all currently selectable routes; HA chooses the actual backend."""
    if not target.name.startswith(("Aiper-Surfer S1-", "Aiper_Surfer S1_")):
        raise ProtocolError("unsupported_model")
    device = bluetooth.async_ble_device_from_address(
        hass, target.address, connectable=True
    )
    routes = bluetooth.async_scanner_devices_by_address(
        hass, target.address, connectable=True
    )
    if device is None or not routes:
        raise ProtocolError("no_connectable_route")
    if device.address.upper() != target.address:
        raise ProtocolError("identity_changed")
    for route in routes:
        candidate, advertisement = route.ble_device, route.advertisement
        name = advertisement.local_name or candidate.name
        if candidate.address.upper() != target.address or not target.matches_name(name):
            raise ProtocolError("identity_changed")
        hint = protocol_hint({"ManufacturerData": advertisement.manufacturer_data})
        if hint == "ecdh":
            raise ProtocolError("ecdh_unsupported")
        if hint == "malformed":
            raise ProtocolError("protocol_malformed")
        if hint == "unknown" and not query.allow_missing_advertisement:
            raise ProtocolError("protocol_unknown")
        # Honour negative local metadata if provided, without requiring private
        # BlueZ paths/properties from a remote proxy.
        details = candidate.details
        props = details.get("props", {}) if isinstance(details, dict) else {}
        if any(props.get(key) for key in ("Paired", "Bonded", "Trusted", "Blocked")):
            raise ProtocolError("security_gated_endpoint")
        if not connected and props.get("Connected"):
            raise ProtocolError("device_already_connected")
    return device


def endpoint(client: BleakClient, query: Query | Control) -> BleakGATTCharacteristic:
    """Require one exact legacy service/characteristic and reject ECDH."""
    services = list(client.services)
    chars = [char for service in services for char in service.characteristics]
    if any(char.uuid.lower() == KEY_EXCHANGE_CHARACTERISTIC for char in chars):
        raise ProtocolError("ecdh_gatt_unsupported")
    service_matches = [s for s in services if s.uuid.lower() == EXPECTED_SERVICE]
    if len(service_matches) != 1:
        raise ProtocolError("ambiguous_service")
    char_matches = [
        char
        for char in service_matches[0].characteristics
        if char.uuid.lower() == EXPECTED_CHARACTERISTIC
    ]
    if len(char_matches) != 1:
        raise ProtocolError("ambiguous_characteristic")
    char = char_matches[0]
    flags = set(char.properties)
    if flags & SECURITY_FLAGS:
        raise ProtocolError("security_gated_endpoint")
    required = {
        "notify",
        "write" if query.write_mode == "request" else "write-without-response",
    }
    if not required <= flags:
        raise ProtocolError("unsupported_write_or_notify_flags")
    return char


async def query_once(
    hass: HomeAssistant,
    target: Target,
    report: dict[str, Any],
    query: Query | Control,
    *,
    pin_local: bool = False,
    proxy_source: Any = None,
    proxy_guard: Any = None,
) -> None:
    """Connect, issue one fixed request, stop notifications, disconnect.

    Publication is the coordinator's responsibility after CRC and cleanup checks.
    Always retain a client reference before connecting so timeout/unload can
    attempt bounded cleanup even if establish_connection never returns.
    """
    owners: list[Any] = []
    char: BleakGATTCharacteristic | None = None
    notify_attempted = False
    queue: asyncio.Queue[bytes | ProtocolError] = asyncio.Queue(maxsize=32)
    decoder = Decoder()
    overflow = False
    accepting = False
    transport = "ha_bluetooth_local" if pin_local else "ha_bluetooth"
    report.update(
        status="running",
        transport=transport,
        write_attempts=0,
        phase="route_validation",
    )
    diagnostics = TransportDiagnostics(report, transport)
    diagnostics.data["timeouts_seconds"] = {
        "outer_connect": CONNECT_SECONDS,
        "exchange": EXCHANGE_SECONDS,
        "write": 5,
        "stop_notify": 5,
        "disconnect": 10,
    }
    connect_timeout: asyncio.Timeout | None = None
    exchange_timeout: asyncio.Timeout | None = None

    def phase(name: str) -> None:
        report["phase"] = name
        diagnostics.phase(name)

    phase("route_validation")
    diagnostics.snapshot(hass, target.address, "before_connect")

    def notified(_char: BleakGATTCharacteristic, data: bytearray) -> None:
        nonlocal overflow
        if not accepting:
            return
        if queue.full():
            overflow = True
            return
        queue.put_nowait(
            ProtocolError("capture_limit")
            if len(data) > MAX_TOTAL_BYTES
            else bytes(data)
        )

    try:
        device = validate_routes(hass, target, query)
        client_class = single_attempt_client_class(diagnostics)
        if pin_local:
            from .local_bleak import (
                assert_local_backend,
                pinned_client_class,
                runtime_versions,
            )

            versions = await hass.async_add_executor_job(runtime_versions)
            client_class = pinned_client_class(
                client_class, target, diagnostics, versions
            )
        if proxy_source is not None:
            from .proxy_trace import assert_proxy_backend
            from .proxy_trace import pinned_client_class as proxy_client_class

            client_class = proxy_client_class(
                client_class, target, proxy_source, diagnostics
            )
            if proxy_guard is None:
                raise ProtocolError("proxy_trace_guard_required")
            proxy_guard()
        phase("connect")
        async with asyncio.timeout(CONNECT_SECONDS) as connect_timeout:
            client = await establish_connection(
                client_class,
                device,
                "Aiper BLE",
                owners=owners,
                max_attempts=1,
                pair=False,
                use_services_cache=False,
            )
        diagnostics.client(client)
        diagnostics.snapshot(hass, target.address, "after_connect")
        async with asyncio.timeout(EXCHANGE_SECONDS) as exchange_timeout:
            phase("connected_route_validation")
            validate_routes(hass, target, query, connected=True)
            if pin_local:
                assert_local_backend(client, target)
            if proxy_source is not None:
                proxy_guard()
                assert_proxy_backend(client, target, proxy_source)
            phase("endpoint_validation")
            char = endpoint(client, query)
            phase("start_notify")
            notify_attempted = True
            await client.start_notify(char, notified)
            phase("prewrite_validation")
            validate_routes(hass, target, query, connected=True)
            if pin_local:
                assert_local_backend(client, target)
            if proxy_source is not None:
                proxy_guard()
                assert_proxy_backend(client, target, proxy_source)
            if endpoint(client, query).handle != char.handle:
                raise ProtocolError("notification_endpoint_changed")
            if not client.is_connected:
                raise ProtocolError("disconnected_before_query")
            accepting = True
            phase("write")
            # Conservative ATT-minimum chunks work across proxy MTU variations.
            for chunk in chunks(query_frame(query)):
                if overflow:
                    raise ProtocolError("notification_queue_overflow")
                report["write_attempts"] += 1
                await asyncio.wait_for(
                    client.write_gatt_char(
                        char, chunk, response=query.write_mode == "request"
                    ),
                    5,
                )
            while True:
                phase("wait_response")
                value = await queue.get()
                phase("decode")
                if overflow:
                    raise ProtocolError("notification_queue_overflow")
                if isinstance(value, ProtocolError):
                    raise value
                for response in decoder.feed(value):
                    evidence = response_evidence(response, query)
                    if evidence:
                        report["response_evidence"] = evidence
                    if query_telemetry(response, query) is not None:
                        report.update(
                            status="query_complete", protocol_response=response
                        )
                        return
    except ProtocolError as exc:
        diagnostics.error(exc, "query")
        report.update(
            status="failed",
            error_code=str(exc),
            failure_stage=report["phase"],
            error_category="protocol",
        )
    except asyncio.CancelledError:
        diagnostics.data["cancelled"] = True
        report.update(
            status="interrupted",
            failure_stage=report["phase"],
            error_category="cancelled",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - no backend identifiers in diagnostics
        diagnostics.error(exc, "query")
        report.update(
            status="failed",
            error_code="bluetooth_transport_error",
            failure_stage=report["phase"],
            error_category=error_category(exc),
        )
    finally:
        accepting = False
        diagnostics.data["outer_connect_expired"] = (
            connect_timeout.expired() if connect_timeout is not None else None
        )
        diagnostics.data["exchange_expired"] = (
            exchange_timeout.expired() if exchange_timeout is not None else None
        )
        cleanup_failed = False
        for client in owners:
            diagnostics.client(client)
            if notify_attempted and char is not None:
                diagnostics.phase("stop_notify")
                try:
                    await asyncio.wait_for(client.stop_notify(char), 5)
                    report["notification_cleanup"] = "stop_confirmed"
                except Exception as exc:  # noqa: BLE001 - still disconnect
                    diagnostics.error(exc, "stop_notify")
                    report["notification_cleanup"] = "stop_unconfirmed"
                    report["notification_cleanup_error_category"] = error_category(exc)
                    report.setdefault("failure_stage", "stop_notify")
                    cleanup_failed = True
            diagnostics.phase("disconnect")
            try:
                await asyncio.wait_for(client.disconnect(), 10)
                if client.is_connected:
                    raise ProtocolError("disconnect_unconfirmed")
                report["cleanup"] = "disconnected_confirmed"
            except Exception as exc:  # noqa: BLE001 - suspend rather than retry
                diagnostics.error(exc, "disconnect")
                report["cleanup"] = "disconnect_unconfirmed"
                report["cleanup_error_category"] = error_category(exc)
                report.setdefault("failure_stage", "disconnect")
                cleanup_failed = True
        report["notification_count"] = decoder.notifications
        report["received_bytes"] = decoder.total_bytes
        if (
            report.get("notification_cleanup") == "stop_unconfirmed"
            and report.get("cleanup") == "disconnected_confirmed"
        ):
            # The HA-managed client owns this subscription alone, so a confirmed
            # disconnect releases it even when the unsubscribe on a dead link
            # failed. Fail the cycle with backoff instead of suspending polling.
            report["notification_cleanup"] = "released_by_disconnect"
            cleanup_failed = False
            if report.get("status") == "query_complete":
                report.update(
                    status="failed",
                    error_code="notification_cleanup_unconfirmed",
                    failure_stage="stop_notify",
                    error_category=report.get(
                        "notification_cleanup_error_category", "transport"
                    ),
                )
        if cleanup_failed:
            report["status"] = "cleanup_requires_review"
        diagnostics.phase("post_cleanup_snapshot")
        diagnostics.snapshot(hass, target.address, "after_cleanup")
        diagnostics.finish()
