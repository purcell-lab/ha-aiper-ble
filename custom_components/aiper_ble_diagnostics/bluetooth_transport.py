"""Fixed queries over HA's shared Bluetooth routing, including active proxies.

No private scanner, adapter pinning, pairing, controls or query retries.
Remote backends do not expose BlueZ pairing/trust/notification ownership
metadata. Exclusive access remains an explicit operator prerequisite.
"""

import asyncio

import bleak_retry_connector
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth

from .probe import (
    EXPECTED_CHARACTERISTIC,
    EXPECTED_SERVICE,
    KEY_EXCHANGE_CHARACTERISTIC,
)
from .protocol import (
    MAX_TOTAL_BYTES,
    Decoder,
    ProtocolError,
    chunks,
    protocol_hint,
    query_frame,
    query_telemetry,
    response_evidence,
)

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


def error_category(error):
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


def single_attempt_client_class():
    """Resolve HA's replacement at call time, never capture an unwrapped base.

    HA patches bleak_retry_connector during Bluetooth setup. Our component may
    have been imported before that dependency finished loading.
    """

    class SingleAttemptClient(bleak_retry_connector.BleakClientWithServiceCache):
        """Retain failed/cancelled clients and prevent helper connect retries."""

        def __init__(self, *args, owners, **kwargs):
            super().__init__(*args, **kwargs)
            self.attempted = False
            owners.append(self)

        async def connect(self, **kwargs):
            if self.attempted:
                raise ProtocolError("connection_retry_refused")
            self.attempted = True
            return await super().connect(**kwargs)

    return SingleAttemptClient


def validate_routes(hass, target, query, *, connected=False):
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
        if candidate.address.upper() != target.address or name != target.name:
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


def endpoint(client, query):
    """Require one exact legacy service/characteristic and reject ECDH."""
    services = list(client.services)
    chars = [char for service in services for char in service.characteristics]
    if any(char.uuid.lower() == KEY_EXCHANGE_CHARACTERISTIC for char in chars):
        raise ProtocolError("ecdh_gatt_unsupported")
    matches = [s for s in services if s.uuid.lower() == EXPECTED_SERVICE]
    if len(matches) != 1:
        raise ProtocolError("ambiguous_service")
    matches = [
        char
        for char in matches[0].characteristics
        if char.uuid.lower() == EXPECTED_CHARACTERISTIC
    ]
    if len(matches) != 1:
        raise ProtocolError("ambiguous_characteristic")
    char = matches[0]
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


async def query_once(hass, target, report, query):
    """Connect, issue one fixed request, stop notifications, disconnect.

    Publication is the coordinator's responsibility after CRC and cleanup checks.
    Always retain a client reference before connecting so timeout/unload can
    attempt bounded cleanup even if establish_connection never returns.
    """
    owners = []
    char = None
    notify_attempted = False
    queue = asyncio.Queue(maxsize=32)
    decoder = Decoder()
    overflow = False
    accepting = False
    report.update(
        status="running",
        transport="ha_bluetooth",
        write_attempts=0,
        phase="route_validation",
    )

    def notified(_char, data):
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
        report["phase"] = "connect"
        async with asyncio.timeout(CONNECT_SECONDS):
            client = await establish_connection(
                single_attempt_client_class(),
                device,
                "Aiper BLE",
                owners=owners,
                max_attempts=1,
                pair=False,
                use_services_cache=False,
            )
        async with asyncio.timeout(EXCHANGE_SECONDS):
            report["phase"] = "connected_route_validation"
            validate_routes(hass, target, query, connected=True)
            report["phase"] = "endpoint_validation"
            char = endpoint(client, query)
            report["phase"] = "start_notify"
            notify_attempted = True
            await client.start_notify(char, notified)
            report["phase"] = "prewrite_validation"
            validate_routes(hass, target, query, connected=True)
            if endpoint(client, query).handle != char.handle:
                raise ProtocolError("notification_endpoint_changed")
            if not client.is_connected:
                raise ProtocolError("disconnected_before_query")
            accepting = True
            report["phase"] = "write"
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
                report["phase"] = "wait_response"
                value = await queue.get()
                report["phase"] = "decode"
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
        report.update(
            status="failed",
            error_code=str(exc),
            failure_stage=report["phase"],
            error_category="protocol",
        )
    except asyncio.CancelledError:
        report.update(
            status="interrupted",
            failure_stage=report["phase"],
            error_category="cancelled",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - no backend identifiers in diagnostics
        report.update(
            status="failed",
            error_code="bluetooth_transport_error",
            failure_stage=report["phase"],
            error_category=error_category(exc),
        )
    finally:
        accepting = False
        cleanup_failed = False
        for client in owners:
            if notify_attempted:
                try:
                    await asyncio.wait_for(client.stop_notify(char), 5)
                    report["notification_cleanup"] = "stop_confirmed"
                except Exception as exc:  # noqa: BLE001 - still disconnect
                    report["notification_cleanup"] = "stop_unconfirmed"
                    report["notification_cleanup_error_category"] = error_category(exc)
                    report.setdefault("failure_stage", "stop_notify")
                    cleanup_failed = True
            try:
                await asyncio.wait_for(client.disconnect(), 10)
                if client.is_connected:
                    raise ProtocolError("disconnect_unconfirmed")
                report["cleanup"] = "disconnected_confirmed"
            except Exception as exc:  # noqa: BLE001 - suspend rather than retry
                report["cleanup"] = "disconnect_unconfirmed"
                report["cleanup_error_category"] = error_category(exc)
                report.setdefault("failure_stage", "disconnect")
                cleanup_failed = True
        report["notification_count"] = decoder.notifications
        report["received_bytes"] = decoder.total_bytes
        if cleanup_failed:
            report["status"] = "cleanup_requires_review"
