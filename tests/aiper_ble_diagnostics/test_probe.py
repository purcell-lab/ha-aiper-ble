"""Test the narrow protocol boundary and cleanup."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.aiper_ble_diagnostics import probe as p

from .helpers import TARGET, Fake


async def test_preflight_never_connects():
    api, report = Fake(), {}
    await p.probe(api, TARGET, report)
    assert api.calls == ["metadata"]
    assert report["status"] == "preflight_passed_no_connection"


async def test_success_and_cleanup():
    api, report = Fake(), {}
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "discovery_complete"
    assert report["cleanup"] == "disconnected_confirmed"
    assert report["expected_characteristic_found_in_expected_service"]
    assert api.calls.count("connect") == 1
    assert api.calls.count("disconnect") == 1
    assert "Value" not in str(report["services"])
    assert "private" not in str(report["services"])


@pytest.mark.parametrize(
    "property,value",
    [
        ("Connected", True),
        ("Paired", True),
        ("Trusted", True),
        ("Blocked", True),
        ("Address", "wrong"),
        ("Name", "different"),
    ],
)
async def test_refuses_changed_or_busy_device(property, value):
    api, report = Fake(), {}
    api.data[TARGET.device_path][p.DEVICE_IF][property] = value
    await p.probe(api, TARGET, report, True)
    assert api.calls == ["metadata"]
    assert report["status"] == "failed"


async def test_refuses_wrong_adapter():
    api, report = Fake(), {}
    api.data[TARGET.adapter_path]["org.bluez.Adapter1"]["Address"] = "wrong"
    await p.probe(api, TARGET, report, True)
    assert api.calls == ["metadata"]


async def test_connect_timeout_cleans_pending_connection():
    api, report = Fake(), {}
    api.connect = AsyncMock(side_effect=TimeoutError)
    await p.probe(api, TARGET, report, True)
    assert "disconnect" in api.calls
    assert report["status"] == "failed"


@pytest.mark.parametrize("error", ["InProgress", "AlreadyConnected"])
async def test_competing_connection_not_disconnected(error):
    api, report = Fake(), {}
    api.connect = AsyncMock(side_effect=p.BluezError("org.bluez.Error." + error))
    await p.probe(api, TARGET, report, True)
    assert "disconnect" not in api.calls
    assert report["cleanup"] == "not_touched_possible_other_client"


async def test_inventory_failure_still_disconnects():
    api, report = Fake(), {}
    api.objects = AsyncMock(side_effect=[api.data, api.data, RuntimeError("test")])
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "failed"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_disconnect_failure_visible():
    api, report = Fake(), {}
    api.disconnect = AsyncMock(side_effect=RuntimeError("test"))
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "cleanup_requires_review"


async def test_cancel_still_cleans_up():
    api, report = Fake(), {}
    api.connect = AsyncMock(side_effect=asyncio.CancelledError)
    await p.probe(api, TARGET, report, True)
    assert "disconnect" in api.calls
    assert report["status"] == "interrupted"


async def test_service_resolution_timeout_disconnects(monkeypatch):
    api, report = Fake(), {}

    async def connect_without_resolution():
        api.calls.append("connect")
        api.data[TARGET.device_path][p.DEVICE_IF]["Connected"] = True

    api.connect = connect_without_resolution
    monkeypatch.setattr(p, "RESOLVE_SECONDS", 0.001)
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "failed"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_unconfirmed_disconnect_is_not_reported_as_success():
    api, report = Fake(), {}
    api.disconnect = AsyncMock()
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "cleanup_requires_review"


async def test_pairing_change_is_flagged():
    api, report = Fake(), {}
    normal_disconnect = api.disconnect

    async def changed_pairing():
        await normal_disconnect()
        api.data[TARGET.device_path][p.DEVICE_IF]["Paired"] = True

    api.disconnect = changed_pairing
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "cleanup_requires_review"


@pytest.mark.parametrize(
    "operation",
    [
        "ReadValue",
        "WriteValue",
        "StartNotify",
        "StopNotify",
        "Pair",
        "Set",
        "StartDiscovery",
        "RemoveDevice",
    ],
)
async def test_allowlist_blocks_out_of_scope_operations(operation):
    api = p.Bluez(None, TARGET)
    with pytest.raises(p.ProbeError):
        await api.call(TARGET.device_path, p.CHAR_IF, operation)


async def test_allowlist_rejects_other_target():
    api = p.Bluez(None, TARGET)
    with pytest.raises(p.ProbeError):
        await api.call("/org/bluez/hci0/dev_OTHER", p.DEVICE_IF, "Connect")


async def test_missing_expected_service_is_not_supported_protocol_claim():
    api, report = Fake(), {}
    for interfaces in api.data.values():
        if p.SERVICE_IF in interfaces:
            interfaces[p.SERVICE_IF]["UUID"] = "other-service"
    await p.probe(api, TARGET, report, True)
    assert report["status"] == "discovery_complete"
    assert not report["expected_service_found"]
    assert not report["expected_characteristic_found_in_expected_service"]
