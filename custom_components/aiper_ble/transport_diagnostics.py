"""Bounded, passive transport observations; never export backend prose."""

import math
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from bleak.exc import BleakError
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

MAX_ROUTES = 16


def optional(reader: Callable[[], Any]) -> Any:
    """Version-sensitive metadata must not affect transport or safety gates."""
    try:
        return reader()
    except Exception:  # noqa: BLE001 - optional observation only
        return None


def number(
    value: object, minimum: float = 0, maximum: float = 1e9
) -> int | float | None:
    """Accept finite built-in numbers only, never backend string representations."""
    if (
        isinstance(value, (int, float))
        and type(value) in (int, float)
        and minimum <= value <= maximum
        and math.isfinite(value)
    ):
        return value
    return None


def exception_kind(error: BaseException) -> str:
    """Return a fixed family, including for untrusted custom subclasses."""
    for cls, name in (
        (TimeoutError, "timeout"),
        (BleakError, "bleak"),
        (ConnectionError, "connection"),
        (OSError, "os"),
    ):
        if isinstance(error, cls):
            return name
    return "other"


class TransportDiagnostics:
    """One query's observation buffer; no active scans, logs or BLE operations."""

    def __init__(self, report: dict[str, Any], transport: str) -> None:
        self.started = self.phase_started = monotonic()
        self.phase_name: str | None = None
        self.sources: dict[str, str] = {}
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "requested_transport": transport,
            "backend": "local_bluez" if transport == "local_bluez" else "unknown",
            "selected_route": None,
            "phase_ms": {},
            "route_snapshots": {},
            "connect_calls_observed": 0,
            "retry_calls_refused": 0,
        }
        report["transport_diagnostics"] = self.data

    def phase(self, name: str | None) -> None:
        now = monotonic()
        if self.phase_name is not None:
            timings = self.data["phase_ms"]
            timings[self.phase_name] = round(
                timings.get(self.phase_name, 0) + (now - self.phase_started) * 1000, 3
            )
        self.phase_started, self.phase_name = now, name

    def route_id(self, scanner: Any) -> str | None:
        source = optional(lambda: scanner.source)
        if type(source) is not str or not source:
            return None
        if source not in self.sources and len(self.sources) < MAX_ROUTES:
            self.sources[source] = f"route_{len(self.sources) + 1}"
        return self.sources.get(source)

    def snapshot(self, hass: HomeAssistant, address: str, stage: str) -> None:
        """Read cached per-target metadata only; candidate order is not selection."""
        routes = optional(
            lambda: bluetooth.async_scanner_devices_by_address(
                hass, address, connectable=True
            )
        )
        if not isinstance(routes, (list, tuple)):
            self.data["route_snapshots"][stage] = {"available": False}
            return
        rows: list[dict[str, Any]] = []
        for route in routes[:MAX_ROUTES]:
            scanner = optional(lambda: route.scanner)
            timestamp = number(
                optional(lambda: scanner.discovered_device_timestamps.get(address))
            )
            age = (
                number(monotonic_time_coarse() - timestamp)
                if timestamp is not None
                else None
            )
            allocations = optional(lambda: scanner.get_allocations())
            scanner_type = optional(lambda: scanner.details.scanner_type.value)
            address_type = optional(
                lambda: route.ble_device.details.get("address_type")
            )
            rows.append(
                {
                    "route_id": self.route_id(scanner),
                    "scanner_type": (
                        scanner_type
                        if type(scanner_type) is str
                        and scanner_type in {"usb", "uart", "remote", "unknown"}
                        else "unknown"
                    ),
                    "rssi_dbm": number(
                        optional(lambda: route.advertisement.rssi), -127, 20
                    ),
                    "address_type": (
                        address_type
                        if type(address_type) is int and 0 <= address_type <= 3
                        else None
                    ),
                    "advertisement_age_seconds": (
                        round(age, 3) if age is not None else None
                    ),
                    "slots": number(optional(lambda: allocations.slots)),
                    "free_slots": number(optional(lambda: allocations.free)),
                    "connection_failures": number(
                        optional(lambda: scanner.connection_failures(address))
                    ),
                    "connections_in_progress": number(
                        optional(lambda: scanner.connections_in_progress())
                    ),
                }
            )
        self.data["route_snapshots"][stage] = {
            "available": True,
            "truncated": len(routes) > MAX_ROUTES,
            "routes": rows,
        }

    def client(self, client: Any) -> None:
        """Observe a retained client, without guessing after HA clears a backend."""
        backend = optional(lambda: client.backend_id)
        if isinstance(backend, str) and backend == "bluez_dbus":
            self.data["backend"] = "bleak_bluez"
        # HA currently exposes no public selected-scanner API. These optional,
        # read-only hints may disappear; no transport decision depends on them.
        module = optional(lambda: type(client._backend).__module__)
        if type(module) is str and module.startswith("bleak_esphome."):
            self.data["backend"] = "bleak_esphome"
        scanner = optional(lambda: client._connected_scanner)
        selected = self.route_id(scanner)
        if selected is not None:
            self.data["selected_route"] = selected

    def error(self, error: BaseException | None, stage: str) -> None:
        """Record up to three exception families, not messages or class names."""
        chain: list[str] = []
        for _ in range(3):
            if error is None:
                break
            current = error
            chain.append(exception_kind(current))
            error = optional(lambda: current.__cause__ or current.__context__)
        self.data.setdefault("errors", {})[stage] = chain

    def finish(self) -> None:
        self.phase(None)
        self.data["elapsed_ms"] = round((monotonic() - self.started) * 1000, 3)
