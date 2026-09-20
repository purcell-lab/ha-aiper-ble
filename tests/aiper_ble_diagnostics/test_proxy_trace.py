"""Native trace privacy, dedicated connection lifecycle and exact route guards."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from aioesphomeapi import LogLevel
from bleak.backends.device import BLEDevice
from habluetooth.wrappers import HaBleakClientWrapper

from custom_components.aiper_ble_diagnostics import proxy_trace as trace
from custom_components.aiper_ble_diagnostics.protocol import ProtocolError
from custom_components.aiper_ble_diagnostics.transport_diagnostics import (
    TransportDiagnostics,
)

from .helpers import TARGET
from .test_polling import OP, PATH

SOURCE = "11:22:33:44:55:66"


def log(message, address=TARGET.address, tag="esp32_ble_client", decorated=False):
    text = f"[D][{tag}:123]: [0] [{address}] {message}"
    if decorated:
        text = "\x1b[0;36m[15:00:00]" + text + "\x1b[0m\r\n"
    return SimpleNamespace(message=text.encode())


@pytest.mark.parametrize(
    "message,event,fields",
    [
        ("0x00 Connecting", "connecting", {}),
        ("Connection open", "connection_open", {}),
        ("ESP_GATTC_OPEN_EVT", "gatt_event", {"stage": "OPEN"}),
        ("ESP_GATTC_CONNECT_EVT", "gatt_event", {"stage": "CONNECT"}),
        ("ESP_GATTC_SEARCH_CMPL_EVT", "gatt_event", {"stage": "SEARCH_CMPL"}),
        ("ESP_GATTC_CLOSE_EVT", "gatt_event", {"stage": "CLOSE"}),
        ("cfg_mtu status 0, mtu 23", "mtu", {"status": 0, "mtu": 23}),
        ("cfg_mtu failed, mtu 23, status 13", "mtu_failed", {"status": 13, "mtu": 23}),
        ("ESP_GATTC_DISCONNECT_EVT, reason 0x13", "disconnect", {"reason": 19}),
        ("Service discovery complete", "services_complete", {}),
        ("Searching for services", "services_start", {}),
        ("Connection open error, status=133", "open_error", {"status": 133}),
        ("Disconnecting (conn_id: 2).", "disconnecting", {}),
        ("Remote closed during discovery", "remote_closed_during_discovery", {}),
        (
            "Disconnect before connected, disconnect scheduled",
            "disconnect_scheduled",
            {},
        ),
        (
            "Disconnect requested, but already IDLE",
            "disconnect_state",
            {"stage": "IDLE"},
        ),
        ("DISCONNECT_EVT after CLOSE_EVT, already IDLE", "already_idle", {}),
        (
            "Timeout waiting for CLOSE_EVT after disconnect, forcing IDLE",
            "close_timeout",
            {},
        ),
    ],
)
async def test_exact_events_drop_all_private_text(message, event, fields):
    capture = trace.EventCapture(TARGET.address)
    capture.receive(log(message, decorated=True))
    item = capture.data["events"][0]
    assert item["event"] == event
    assert fields.items() <= item.items()
    assert "received_utc" in item and item["offset_ms"] >= 0
    public = json.dumps(capture.data)
    assert TARGET.address not in public
    assert "esp32_ble_client" not in public


@pytest.mark.parametrize(
    "response",
    [
        log("Connection open", address=SOURCE),
        log("Connection open", tag="wifi"),
        log("Connection open PRIVATE_SECRET"),
        log("cfg_mtu status 0, mtu 23\nPRIVATE_SECRET"),
        log("unknown PRIVATE_SSID PRIVATE_KEY PRIVATE_SERIAL"),
        SimpleNamespace(message=b"x" * 3000),
        SimpleNamespace(message=b"\xff\x00PRIVATE_SECRET"),
    ],
)
async def test_unknown_and_other_device_lines_are_discarded(response):
    capture = trace.EventCapture(TARGET.address)
    capture.receive(response)
    assert not capture.data["events"]
    assert "PRIVATE" not in json.dumps(capture.data)


@pytest.mark.parametrize("cap", ["MAX_EVENTS", "MAX_LINES", "MAX_BYTES"])
async def test_capture_is_bounded(cap):
    capture = trace.EventCapture(TARGET.address)
    with patch.object(trace, cap, 1):
        for _ in range(100):
            capture.receive(log("Connection open"))
    assert capture.data["capture_limit_reached"]
    assert not capture.accepting
    assert len(capture.data["events"]) <= 1
    assert capture.data["lines_seen"] <= 2


@pytest.fixture
def route():
    device = BLEDevice(TARGET.address, TARGET.name, {"source": SOURCE})
    scanner = SimpleNamespace(
        source=SOURCE,
        connector=SimpleNamespace(client=Mock(), can_connect=Mock(return_value=True)),
        connectable=True,
        discovered_device_timestamps={TARGET.address: 99.5},
        get_allocations=Mock(
            return_value=SimpleNamespace(free=3, slots=3, allocated=[])
        ),
        connections_in_progress=Mock(return_value=0),
    )
    route = SimpleNamespace(
        scanner=scanner,
        ble_device=device,
        advertisement=SimpleNamespace(local_name=TARGET.name),
    )
    manager = SimpleNamespace(
        async_scanner_devices_by_address=Mock(return_value=[route]),
        async_scanner_by_source=Mock(return_value=scanner),
    )
    with patch.object(trace, "monotonic_time_coarse", return_value=100):
        yield SimpleNamespace(
            route=route, manager=manager, scanner=scanner, device=device
        )


def test_proxy_pin_retains_ha_lifecycle_and_drops_identifiers(route):
    diagnostic = TransportDiagnostics({}, "ha_bluetooth")
    cls = trace.pinned_client_class(HaBleakClientWrapper, TARGET, SOURCE, diagnostic)
    client = object.__new__(cls)
    backend = client._async_get_best_available_backend_and_device(route.manager)
    assert backend.source == SOURCE
    assert backend.device is route.device
    assert cls.connect is HaBleakClientWrapper.connect
    assert cls.disconnect is HaBleakClientWrapper.disconnect
    assert diagnostic.data["proxy_route_asserted"]
    assert SOURCE not in json.dumps(diagnostic.data)


@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "duplicate",
        "wrong_name",
        "wrong_address",
        "wrong_source",
        "passive",
        "connector",
        "unregistered",
        "stale",
        "future",
        "no_timestamp",
        "no_slots",
        "busy",
        "allocated",
        "in_progress",
    ],
)
def test_preconnect_route_guards(route, condition):
    scanner, device, manager = route.scanner, route.device, route.manager
    if condition == "missing":
        manager.async_scanner_devices_by_address.return_value = []
    elif condition == "duplicate":
        manager.async_scanner_devices_by_address.return_value *= 2
    elif condition == "wrong_name":
        route.route.advertisement.local_name = "other"
    elif condition == "wrong_address":
        device.address = SOURCE
    elif condition == "wrong_source":
        device.details["source"] = "other"
    elif condition == "passive":
        scanner.connectable = False
    elif condition == "connector":
        scanner.connector = None
    elif condition == "unregistered":
        manager.async_scanner_by_source.return_value = object()
    elif condition in {"stale", "future", "no_timestamp"}:
        scanner.discovered_device_timestamps[TARGET.address] = {
            "stale": 80,
            "future": 101,
            "no_timestamp": None,
        }[condition]
    elif condition == "no_slots":
        scanner.get_allocations.return_value = None
    elif condition == "busy":
        scanner.get_allocations.return_value.free = 2
    elif condition == "allocated":
        scanner.get_allocations.return_value.allocated = [TARGET.address]
    else:
        scanner.connections_in_progress.return_value = 1
    with pytest.raises(ProtocolError):
        trace.select_route(manager, TARGET, SOURCE)


@pytest.mark.parametrize("wrong", [False, True])
def test_actual_backend_revalidated(route, wrong):
    backend = Mock(spec=trace.ESPHomeClient)
    backend._source = SOURCE if not wrong else "other"
    client = SimpleNamespace(
        _backend=backend,
        _connected_scanner=route.scanner,
        _connected_device=route.device,
    )
    if wrong:
        with pytest.raises(ProtocolError, match="backend_mismatch"):
            trace.assert_proxy_backend(client, TARGET, SOURCE)
    else:
        trace.assert_proxy_backend(client, TARGET, SOURCE)


@pytest.fixture
def session(route):
    info = SimpleNamespace(
        name="PRIVATE_PROXY",
        mac_address=SOURCE,
        bluetooth_mac_address=SOURCE,
        esphome_version="2026.9.0",
    )
    shared = SimpleNamespace(
        is_connected=True,
        connected_address="192.0.2.5",
        subscribe_logs=Mock(),
        disconnect=Mock(),
    )
    runtime = SimpleNamespace(
        available=True,
        client=shared,
        device_info=info,
        bluetooth_device=SimpleNamespace(mac_address=SOURCE),
    )
    entry = SimpleNamespace(
        domain="esphome",
        runtime_data=runtime,
        data={
            "host": "PRIVATE_HOST",
            "port": 6053,
            "password": "PRIVATE_PASSWORD",
            "noise_psk": "PRIVATE_KEY",
        },
    )
    client = Mock()
    client.is_connected = True
    client.connect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.subscribe_logs.return_value = Mock()

    async def disconnect(**kwargs):
        assert kwargs == {"force": True}
        client.is_connected = False

    client.disconnect = AsyncMock(side_effect=disconnect)
    query = AsyncMock()
    # Patch only this module's sleep through a replacement asyncio facade.
    # Do not globally replace asyncio.sleep and affect HA's own tasks.
    clock = SimpleNamespace(
        get_running_loop=asyncio.get_running_loop,
        timeout=asyncio.timeout,
        sleep=AsyncMock(),
    )
    with (
        patch.object(
            trace, "runtime_versions", return_value=dict(trace.SUPPORTED_VERSIONS)
        ),
        patch.object(trace, "get_manager", return_value=route.manager),
        patch.object(trace, "resolve_proxy", return_value=(entry, runtime, SOURCE)),
        patch.object(trace, "APIClient", return_value=client) as factory,
        patch.object(trace, "asyncio", clock),
        patch(f"{PATH}.bluetooth_transport.query_once", query),
    ):
        yield SimpleNamespace(
            info=info,
            entry=entry,
            runtime=runtime,
            shared=shared,
            client=client,
            query=query,
            factory=factory,
            route=route,
        )


async def test_dedicated_logging_does_not_touch_shared_client(hass, session):
    report = {}
    await trace.query_once(hass, TARGET, report, OP, "proxy_entry")
    session.client.connect.assert_awaited_once_with(login=True, log_errors=False)
    session.query.assert_awaited_once_with(
        hass, TARGET, report, OP, proxy_source=SOURCE, proxy_guard=ANY
    )
    calls = session.client.subscribe_logs.call_args_list
    assert [c.kwargs["log_level"] for c in calls] == [
        LogLevel.LOG_LEVEL_DEBUG,
        LogLevel.LOG_LEVEL_NONE,
    ]
    assert all(c.kwargs["dump_config"] is False for c in calls)
    session.client.disconnect.assert_awaited_once_with(force=True)
    session.shared.subscribe_logs.assert_not_called()
    session.shared.disconnect.assert_not_called()
    assert session.factory.call_args.kwargs["provide_time"] is False
    assert session.factory.call_args.args[0] == "192.0.2.5"
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"
    assert not report["proxy_trace"]["events"]  # Empty does not claim a BLE phase.
    assert "PRIVATE" not in json.dumps(report)
    assert SOURCE not in json.dumps(report)


@pytest.mark.parametrize(
    "failure", ["connect", "identity", "subscribe", "query", "disable"]
)
async def test_every_failure_closes_only_our_socket(hass, session, failure):
    if failure == "connect":
        session.client.connect.side_effect = TimeoutError("PRIVATE_HOST")
    elif failure == "identity":
        session.client.device_info.return_value = SimpleNamespace(
            mac_address="other",
            bluetooth_mac_address="other",
            esphome_version="2026.9.0",
        )
    elif failure == "subscribe":
        session.client.subscribe_logs.side_effect = RuntimeError("PRIVATE_KEY")
    elif failure == "query":
        session.query.side_effect = RuntimeError("PRIVATE_HOST")
    else:
        session.client.subscribe_logs.side_effect = [
            Mock(),
            RuntimeError("PRIVATE_KEY"),
        ]
    report = {}
    if failure == "disable":
        await trace.query_once(hass, TARGET, report, OP, "proxy")
    else:
        with pytest.raises((RuntimeError, TimeoutError, ProtocolError)):
            await trace.query_once(hass, TARGET, report, OP, "proxy")
    session.client.disconnect.assert_awaited_once()
    session.shared.disconnect.assert_not_called()
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"
    if failure in {"connect", "identity", "subscribe"}:
        session.query.assert_not_awaited()


async def test_query_cancellation_still_closes_logging(hass, session):
    started = asyncio.Event()

    async def stalled(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    session.query.side_effect = stalled
    report = {}
    task = asyncio.create_task(trace.query_once(hass, TARGET, report, OP, "proxy"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"
    session.client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("uncertain", ["socket", "slot", "shared", "in_progress"])
async def test_uncertain_cleanup_is_not_reported_as_success(hass, session, uncertain):
    async def queried(*args, **kwargs):
        args[2].update(
            status="query_complete",
            cleanup="disconnected_confirmed",
            transport_diagnostics={"connect_calls_observed": 1},
        )
        if uncertain == "socket":
            session.client.disconnect.side_effect = TimeoutError("PRIVATE_HOST")
        elif uncertain == "slot":
            session.route.scanner.get_allocations.return_value.allocated = [
                TARGET.address
            ]
        elif uncertain == "shared":
            session.shared.is_connected = False
        else:
            session.route.scanner.connections_in_progress.return_value = 1

    session.query.side_effect = queried
    report = {}
    await trace.query_once(hass, TARGET, report, OP, "proxy")
    assert report["status"] == "cleanup_requires_review"
    assert "PRIVATE" not in json.dumps(report)


async def test_unknown_versions_do_not_open_api_or_ble(hass, session):
    with patch.object(trace, "runtime_versions", return_value={}):
        with pytest.raises(ProtocolError, match="versions_unsupported"):
            await trace.query_once(hass, TARGET, {}, OP, "proxy")
    session.factory.assert_not_called()
    session.query.assert_not_awaited()


@pytest.mark.parametrize("changed", ["logger", "runtime", "entry", "source"])
async def test_trace_session_guard_rejects_changes_before_write(hass, session, changed):
    async def query(*args, **kwargs):
        if changed == "logger":
            session.client.is_connected = False
        else:
            values = [session.entry, session.runtime, SOURCE]
            values[{"entry": 0, "runtime": 1, "source": 2}[changed]] = object()
            trace.resolve_proxy.return_value = tuple(values)
        kwargs["proxy_guard"]()

    session.query.side_effect = query
    report = {}
    with pytest.raises(ProtocolError, match="session_changed"):
        await trace.query_once(hass, TARGET, report, OP, "proxy")
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"


@pytest.mark.parametrize(
    "invalid",
    ["domain", "available", "bluetooth", "info", "connected", "firmware", "identity"],
)
async def test_proxy_resolution_fails_closed(hass, session, invalid):
    # Exercise the unpatched function via the mock's wrapped original.
    with patch.object(
        hass.config_entries, "async_get_entry", return_value=session.entry
    ):
        if invalid == "domain":
            session.entry.domain = "bluetooth"
        elif invalid == "available":
            session.runtime.available = False
        elif invalid == "bluetooth":
            session.runtime.bluetooth_device = None
        elif invalid == "info":
            session.runtime.device_info = None
        elif invalid == "connected":
            session.shared.is_connected = False
        elif invalid == "firmware":
            session.info.esphome_version = "2026.8.0"
        else:
            session.runtime.bluetooth_device.mac_address = "other"
        with pytest.raises(ProtocolError):
            REAL_RESOLVE(hass, "proxy")


REAL_RESOLVE = trace.resolve_proxy


@pytest.mark.parametrize("address", [None, "proxy.local", "invalid"])
async def test_no_private_dns_scanner_is_created(hass, session, address):
    session.shared.connected_address = address
    with pytest.raises(ProtocolError, match="address_unavailable"):
        await trace.query_once(hass, TARGET, {}, OP, "proxy")
    session.factory.assert_not_called()
    session.query.assert_not_awaited()


async def test_cancellation_during_api_setup_closes_socket(hass, session):
    started = asyncio.Event()

    async def connect(**kwargs):
        started.set()
        await asyncio.Event().wait()

    session.client.connect.side_effect = connect
    report = {}
    task = asyncio.create_task(trace.query_once(hass, TARGET, report, OP, "proxy"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"
    session.query.assert_not_awaited()


async def test_capture_limit_guard_prevents_unobserved_query(hass, session):
    async def query(*args, **kwargs):
        receive = session.client.subscribe_logs.call_args_list[0].args[0]
        with patch.object(trace, "MAX_LINES", 0):
            receive(log("Connection open"))
        kwargs["proxy_guard"]()

    session.query.side_effect = query
    report = {}
    with pytest.raises(ProtocolError, match="capture_limit"):
        await trace.query_once(hass, TARGET, report, OP, "proxy")
    assert report["proxy_trace"]["capture_limit_reached"]
    assert report["proxy_trace"]["log_cleanup"] == "dedicated_connection_closed"
