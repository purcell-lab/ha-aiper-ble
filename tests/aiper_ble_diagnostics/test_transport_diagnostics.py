"""Passive metadata, bounded export, honest route attribution and privacy."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from bleak.backends import BleakBackend
from bleak.exc import BleakError

from custom_components.aiper_ble_diagnostics import transport_diagnostics as observer
from custom_components.aiper_ble_diagnostics.bluetooth_transport import (
    single_attempt_client_class,
)
from custom_components.aiper_ble_diagnostics.protocol import ProtocolError

PRIVATE = "PRIVATE_NAME_MAC_PATH_SSID_SERIAL"


def scanner(source=PRIVATE):
    return SimpleNamespace(
        source=source,
        details=SimpleNamespace(scanner_type=SimpleNamespace(value="remote")),
        discovered_device_timestamps={PRIVATE: 90.0},
        get_allocations=lambda: SimpleNamespace(
            slots=3, free=2, allocated=[PRIVATE], source=PRIVATE
        ),
        connection_failures=lambda address: 2,
        connections_in_progress=lambda: 1,
    )


def route(item):
    return SimpleNamespace(
        scanner=item,
        advertisement=SimpleNamespace(rssi=-58, local_name=PRIVATE),
        ble_device=SimpleNamespace(address=PRIVATE, name=PRIVATE),
    )


def snapshot(diag, routes, stage="before_connect"):
    with (
        patch.object(
            observer.bluetooth, "async_scanner_devices_by_address", return_value=routes
        ),
        patch.object(observer, "monotonic_time_coarse", return_value=100.0),
    ):
        diag.snapshot(None, PRIVATE, stage)
    return diag.data["route_snapshots"][stage]


def test_route_snapshot_exports_only_allowlisted_metadata():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    item = scanner()
    result = snapshot(diag, [route(item)])
    assert result == {
        "available": True,
        "truncated": False,
        "routes": [
            {
                "route_id": "route_1",
                "scanner_type": "remote",
                "rssi_dbm": -58,
                "advertisement_age_seconds": 10.0,
                "slots": 3,
                "free_slots": 2,
                "connection_failures": 2,
                "connections_in_progress": 1,
            }
        ],
    }
    assert diag.data["selected_route"] is None
    assert diag.data["backend"] == "unknown"
    diag.client(SimpleNamespace(_connected_scanner=item, backend_id="partial"))
    assert diag.data["selected_route"] == "route_1"
    assert diag.data["backend"] == "unknown"
    assert PRIVATE not in json.dumps(diag.data)


def test_route_ids_stable_within_query_not_candidate_order():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    first, second = route(scanner("private_first")), route(scanner("private_second"))
    snapshot(diag, [first, second])
    result = snapshot(diag, [second, first], "after_cleanup")
    assert [r["route_id"] for r in result["routes"]] == ["route_2", "route_1"]
    other = observer.TransportDiagnostics({}, "ha_bluetooth")
    assert snapshot(other, [second])["routes"][0]["route_id"] == "route_1"


def test_route_snapshots_and_ids_are_bounded():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    rows = [route(scanner(str(i))) for i in range(100)]
    result = snapshot(diag, rows)
    assert result["truncated"]
    assert len(result["routes"]) == observer.MAX_ROUTES
    assert diag.route_id(scanner("another")) is None
    assert len(diag.sources) == observer.MAX_ROUTES


class BrokenMetadata:
    def __getattr__(self, name):
        raise RuntimeError(PRIVATE)


def test_optional_metadata_failure_is_unknown_not_transport_failure():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    result = snapshot(diag, [BrokenMetadata()])
    row = result["routes"][0]
    assert row["route_id"] is None
    assert row["scanner_type"] == "unknown"
    assert all(value is None for key, value in row.items() if key != "scanner_type")
    diag.client(BrokenMetadata())
    with patch.object(
        observer.bluetooth,
        "async_scanner_devices_by_address",
        side_effect=RuntimeError(PRIVATE),
    ):
        diag.snapshot(None, PRIVATE, "after_cleanup")
    assert diag.data["route_snapshots"]["after_cleanup"] == {"available": False}
    assert PRIVATE not in json.dumps(diag.data)


@pytest.mark.parametrize(
    "timestamp", [None, float("nan"), float("inf"), 101, -1, PRIVATE]
)
def test_unavailable_or_future_advertisement_age_is_not_fresh(timestamp):
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    item = scanner()
    item.discovered_device_timestamps[PRIVATE] = timestamp
    assert (
        snapshot(diag, [route(item)])["routes"][0]["advertisement_age_seconds"] is None
    )


@pytest.mark.parametrize(
    "value", [True, PRIVATE, float("nan"), float("inf"), -1, {}, 10**1000]
)
def test_numeric_fields_reject_invalid_metadata(value):
    assert observer.number(value) is None


def test_backend_hint_allowlist_and_cleared_backend():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    diag.client(SimpleNamespace(backend_id=PRIVATE, _backend=None))
    assert diag.data["backend"] == "unknown"
    diag.client(SimpleNamespace(backend_id=BleakBackend.BLUEZ_DBUS))
    assert diag.data["backend"] == "bleak_bluez"
    remote = type(PRIVATE, (), {"__module__": "bleak_esphome.backend.client"})()
    diag.client(SimpleNamespace(backend_id="partial", _backend=remote))
    assert diag.data["backend"] == "bleak_esphome"
    assert PRIVATE not in json.dumps(diag.data)


def test_exception_chains_bounded_and_no_custom_class_names_or_prose():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    error = type(PRIVATE, (BleakError,), {})(PRIVATE)
    error.__cause__ = TimeoutError(PRIVATE)
    error.__cause__.__cause__ = error  # Deliberate cycle must stay bounded.
    diag.error(error, "client_connect")
    assert diag.data["errors"]["client_connect"] == ["bleak", "timeout", "bleak"]
    assert PRIVATE not in json.dumps(diag.data)


def test_timings_aggregate_repeated_phases_without_growing_event_list():
    with patch.object(observer, "monotonic", side_effect=range(8)):
        diag = observer.TransportDiagnostics({}, "ha_bluetooth")
        diag.phase("connect")
        diag.phase("decode")
        diag.phase("decode")
        diag.finish()
    assert diag.data["phase_ms"] == {"connect": 1000, "decode": 2000}
    assert diag.data["elapsed_ms"] == 5000


async def test_observer_keeps_single_attempt_guard_and_records_inner_timeout():
    diag = observer.TransportDiagnostics({}, "ha_bluetooth")
    base = Mock()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def connect(self, **kwargs):
            base(**kwargs)
            raise TimeoutError(PRIVATE)

    with patch(
        "bleak_retry_connector.BleakClientWithServiceCache",
        Client,
    ):
        client = single_attempt_client_class(diag)(owners=[])
        with pytest.raises(TimeoutError):
            await client.connect(timeout=20.0)
        with pytest.raises(ProtocolError, match="connection_retry_refused"):
            await client.connect(timeout=20.0)
    base.assert_called_once_with(timeout=20.0)
    assert diag.data["connect_calls_observed"] == 1
    assert diag.data["retry_calls_refused"] == 1
    assert diag.data["inner_connect_timeout_seconds"] == 20.0
    assert diag.data["errors"]["client_connect"] == ["timeout"]
    assert PRIVATE not in json.dumps(diag.data)
