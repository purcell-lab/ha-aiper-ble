"""HA-managed route tests: proxy-only devices, fixed writes and bounded cleanup."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble_diagnostics import bluetooth_transport as transport
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import verified_values
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.probe import (
    EXPECTED_CHARACTERISTIC,
    EXPECTED_SERVICE,
    KEY_EXCHANGE_CHARACTERISTIC,
    Target,
)
from custom_components.aiper_ble_diagnostics.protocol import ProtocolError, query_frame

from .helpers import TARGET
from .test_polling import INFO, OP, OPTIONS, S1, WARN, response, setup
from .test_protocol import frame

PATH = "custom_components.aiper_ble_diagnostics"


class Client:
    """No D-Bus adapter or path: simulates the public Bleak proxy contract."""

    def __init__(self, query):
        self.query = query
        self.char = SimpleNamespace(
            uuid=EXPECTED_CHARACTERISTIC, handle=2, properties=["write", "notify"]
        )
        self.services = [
            SimpleNamespace(uuid=EXPECTED_SERVICE, characteristics=[self.char])
        ]
        self.is_connected = True
        self.calls = []
        self.written = bytearray()
        self.reply = frame(response(query.query_type))
        self.failure = None
        self.stall = None
        self.started = asyncio.Event()
        self.startup = None
        self.after_subscribe = None
        self.flood = False

    async def operation(self, name):
        self.calls.append(name)
        if self.stall == name:
            self.started.set()
            await asyncio.Event().wait()
        if self.failure == name:
            raise RuntimeError("PRIVATE_BACKEND_IDENTIFIER")

    async def start_notify(self, char, callback):
        assert char is self.char
        self.callback = callback
        await self.operation("start")
        if self.startup:
            callback(char, self.startup)
        if self.after_subscribe:
            self.after_subscribe()

    async def write_gatt_char(self, char, data, *, response):
        assert char is self.char
        assert response
        assert len(data) <= 20
        await self.operation("write")
        self.written.extend(data)
        if self.written.endswith(b"\n"):
            if self.flood:
                for _ in range(33):
                    self.callback(char, b"x")
            else:
                self.callback(char, self.reply[:9])
                self.callback(char, self.reply[9:])

    async def stop_notify(self, char):
        await self.operation("stop")

    async def disconnect(self):
        await self.operation("disconnect")
        self.is_connected = False


@pytest.fixture
def radio():
    device = BLEDevice(TARGET.address, TARGET.name, {"source": "test_proxy"})
    route = SimpleNamespace(
        ble_device=device,
        advertisement=SimpleNamespace(
            local_name=TARGET.name, manufacturer_data={0: b"\0"}
        ),
    )
    routes = [route]
    clients = []
    mutations = []

    async def establish(cls, actual_device, name, **kwargs):
        assert cls.__name__ == "SingleAttemptClient"
        assert actual_device.address == TARGET.address
        assert kwargs["pair"] is False
        assert kwargs["max_attempts"] == 1
        assert kwargs["use_services_cache"] is False
        query = (S1, OP, INFO, WARN)[len(clients) % 4]
        client = Client(query)
        clients.append(client)
        kwargs["owners"].append(client)
        if mutations:
            mutations.pop(0)(client)
        await client.operation("connect")
        return client

    with (
        patch.object(
            transport.bluetooth, "async_ble_device_from_address", return_value=device
        ) as get_device,
        patch.object(
            transport.bluetooth, "async_scanner_devices_by_address", return_value=routes
        ),
        patch.object(transport, "establish_connection", establish),
    ):
        yield SimpleNamespace(
            routes=routes,
            device=device,
            clients=clients,
            mutations=mutations,
            get_device=get_device,
        )


async def execute(hass, radio):
    report = {}
    await transport.query_once(hass, TARGET, report, S1)
    return report


async def test_proxy_only_fixed_query_and_cleanup(hass, radio):
    report = await execute(hass, radio)
    client = radio.clients[0]
    assert report["status"] == "query_complete"
    assert report["cleanup"] == "disconnected_confirmed"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert verified_values(report["protocol_response"], S1)["temperature"] == 21.5
    assert bytes(client.written) == query_frame(S1)
    assert client.calls == ["connect", "start"] + ["write"] * 5 + ["stop", "disconnect"]
    radio.get_device.assert_called_with(hass, TARGET.address, connectable=True)
    assert "PRIVATE_BACKEND_IDENTIFIER" not in str(report)


async def test_real_transport_wired_to_coordinator_and_sensors(hass, radio):
    entry = await setup(hass)
    assert len(radio.clients) == 4
    assert bytes(radio.clients[1].written) == query_frame(OP)
    assert entry.runtime_data.coordinator.last_update_success
    assert hass.states.get("sensor.aiper_ble_temperature").state == "21.5"
    assert hass.states.get("sensor.aiper_ble_battery").state == "73"
    assert bytes(radio.clients[2].written) == query_frame(INFO)
    assert bytes(radio.clients[3].written) == query_frame(WARN)
    assert entry.runtime_data.coordinator.data["wifi_rssi_raw"] == -127


async def test_all_dp_entities_registered_and_updated_from_full_reply(hass, radio):
    from custom_components.aiper_ble_diagnostics.datapoints import MACHINE_FIELDS

    radio.mutations.extend(
        [
            lambda client: None,
            lambda client: setattr(
                client,
                "reply",
                frame(
                    response(
                        "OpInfo",
                        {
                            "wifi_rssi": -64,
                            "wifi_name": "Test network",
                            "bat": 73,
                            "status": 2,
                            "link": 1,
                            "Machine": {field: 1 for field in MACHINE_FIELDS},
                        },
                    )
                ),
            ),
        ]
    )
    entry = await setup(hass)
    assert entry.runtime_data.coordinator.last_update_success
    states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.aiper_ble_")
    ]
    assert len(states) == 9
    data = entry.runtime_data.coordinator.data
    assert data["temperature_raw"] == 215.0
    assert data["s1_timezone"] == "UTC+10"
    assert data["opinfo_bat_raw"] == 73
    assert data["wifi_name"] == "Test network"
    assert hass.states.get("sensor.aiper_ble_opinfo_battery_raw") is None
    assert all(state.state != "unavailable" for state in states)


@pytest.mark.parametrize(
    "kind",
    [
        "absent",
        "passive_only",
        "wrong_address",
        "wrong_name",
        "ecdh",
        "malformed",
        "unknown",
        "connected",
        "paired",
    ],
)
async def test_route_veto_before_connect(hass, radio, kind):
    route = radio.routes[0]
    if kind == "absent":
        radio.get_device.return_value = None
    elif kind == "passive_only":
        radio.routes.clear()
    elif kind == "wrong_address":
        route.ble_device = BLEDevice("AA:BB:CC:DD:EE:FF", TARGET.name, {})
    elif kind == "wrong_name":
        route.advertisement.local_name = "Aiper-Surfer S1-CHANGED"
    elif kind in {"ecdh", "malformed", "unknown"}:
        route.advertisement.manufacturer_data = {
            "ecdh": {0: b"\1"},
            "malformed": {0: "bad"},
            "unknown": {},
        }[kind]
    else:
        radio.device.details["props"] = {
            "Connected" if kind == "connected" else "Paired": True
        }
    report = await execute(hass, radio)
    assert report["status"] == "failed"
    assert radio.clients == []


async def test_negative_evidence_on_alternate_route_vetoes(hass, radio):
    radio.routes.append(
        SimpleNamespace(
            ble_device=radio.device,
            advertisement=SimpleNamespace(
                local_name=TARGET.name, manufacturer_data={0: b"\1"}
            ),
        )
    )
    report = await execute(hass, radio)
    assert report["error_code"] == "ecdh_unsupported"
    assert not radio.clients


@pytest.mark.parametrize(
    "kind",
    [
        "key_exchange",
        "duplicate_service",
        "duplicate_char",
        "missing_service",
        "no_write",
        "secure",
    ],
)
async def test_resolved_endpoint_veto_no_write(hass, radio, kind):
    def mutate(client):
        if kind == "key_exchange":
            client.services[0].characteristics.append(
                SimpleNamespace(uuid=KEY_EXCHANGE_CHARACTERISTIC)
            )
        elif kind == "duplicate_service":
            client.services *= 2
        elif kind == "duplicate_char":
            client.services[0].characteristics *= 2
        elif kind == "missing_service":
            client.services.clear()
        elif kind == "no_write":
            client.char.properties = ["notify"]
        else:
            client.char.properties.append("encrypt-write")

    radio.mutations.append(mutate)
    report = await execute(hass, radio)
    assert report["status"] == "failed"
    assert radio.clients[0].calls == ["connect", "disconnect"]


async def test_revalidate_before_write(hass, radio):
    def mutate(client):
        client.after_subscribe = lambda: setattr(
            radio.routes[0].advertisement, "manufacturer_data", {0: b"\1"}
        )

    radio.mutations.append(mutate)
    report = await execute(hass, radio)
    assert report["error_code"] == "ecdh_unsupported"
    assert radio.clients[0].calls == ["connect", "start", "stop", "disconnect"]


async def test_startup_notifications_not_a_query_response(hass, radio):
    radio.mutations.append(
        lambda client: setattr(
            client, "startup", frame(response(data={"ack": "+S1_INFO:999,1\r\n"}))
        )
    )
    report = await execute(hass, radio)
    assert verified_values(report["protocol_response"], S1)["temperature"] == 21.5


@pytest.mark.parametrize("kind", ["flood", "oversize", "invalid"])
async def test_bounded_notification_capture(hass, radio, kind):
    def mutate(client):
        if kind == "flood":
            client.flood = True
        else:
            client.reply = b"x" * 10000 if kind == "oversize" else b"bad\n"

    radio.mutations.append(mutate)
    report = await execute(hass, radio)
    assert report["status"] == "failed"
    assert radio.clients[0].calls[-2:] == ["stop", "disconnect"]


@pytest.mark.parametrize("stage", ["connect", "start", "write", "stop", "disconnect"])
async def test_failures_cleanup_and_suspension(hass, radio, stage):
    radio.mutations.append(lambda client: setattr(client, "failure", stage))
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.last_update_success
    assert len(radio.clients) == 1
    assert "disconnect" in radio.clients[0].calls
    assert coordinator.suspended is (stage in {"stop", "disconnect"})
    assert "PRIVATE_BACKEND_IDENTIFIER" not in str(coordinator.error_code)
    details = coordinator.last_poll_details
    assert details["query_type"] == "S1_INFO"
    assert (
        details["failure_stage"]
        == {
            "connect": "connect",
            "start": "start_notify",
            "write": "write",
            "stop": "stop_notify",
            "disconnect": "disconnect",
        }[stage]
    )
    assert details["transport"] == "ha_bluetooth"
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostic["polling"]["last_poll_details"] == details
    assert "PRIVATE_BACKEND_IDENTIFIER" not in str(diagnostic)
    assert "protocol_response" not in str(details)
    assert TARGET.address not in str(diagnostic)
    assert TARGET.name not in str(diagnostic)


@pytest.mark.parametrize("stage", ["connect", "start", "write"])
async def test_cancel_at_every_connection_phase(hass, radio, stage):
    started = asyncio.Event()

    def mutate(client):
        client.stall = stage
        client.started = started

    radio.mutations.append(mutate)
    report = {}
    task = asyncio.create_task(transport.query_once(hass, TARGET, report, S1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert radio.clients[0].calls[-1] == "disconnect"
    if stage != "connect":
        assert "stop" in radio.clients[0].calls
    assert not radio.clients[0].is_connected
    assert report["error_category"] == "cancelled"
    assert (
        report["failure_stage"]
        == {"connect": "connect", "start": "start_notify", "write": "write"}[stage]
    )


async def test_connection_timeout_cleans_tracked_client(hass, radio):
    radio.mutations.append(lambda client: setattr(client, "stall", "connect"))
    with patch.object(transport, "CONNECT_SECONDS", 0.01):
        report = await execute(hass, radio)
    assert report["status"] == "failed"
    assert radio.clients[0].calls == ["connect", "disconnect"]
    assert report["failure_stage"] == "connect"
    assert report["error_category"] == "timeout"


@pytest.mark.parametrize(
    ("exception", "category"),
    [
        (TimeoutError("PRIVATE"), "timeout"),
        (ConnectionError("PRIVATE"), "connection"),
        (OSError("PRIVATE"), "os"),
        (RuntimeError("PRIVATE"), "unexpected"),
        (transport.BleakError("PRIVATE"), "bleak"),
    ],
)
def test_error_categories_never_include_exception_text(exception, category):
    assert transport.error_category(exception) == category


async def test_notification_timeout_has_no_retry_and_cleans_up(hass, radio):
    radio.mutations.append(lambda client: setattr(client, "reply", b""))
    with patch.object(transport, "EXCHANGE_SECONDS", 0.01):
        report = await execute(hass, radio)
    assert report["failure_stage"] == "wait_response"
    assert report["error_category"] == "timeout"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert len(radio.clients) == 1
    assert bytes(radio.clients[0].written) == query_frame(S1)


async def test_helper_cannot_retry_physical_connect():
    client = object.__new__(transport.single_attempt_client_class())
    client.attempted = False
    with patch.object(
        transport.bleak_retry_connector.BleakClientWithServiceCache,
        "connect",
        new_callable=AsyncMock,
    ) as connect:
        await client.connect()
        with pytest.raises(ProtocolError, match="connection_retry_refused"):
            await client.connect()
    connect.assert_awaited_once()


async def test_real_helper_uses_runtime_replacement_and_never_reconnects():
    """Exercise the real connector's separate transient-error retry budget."""
    from bleak.exc import BleakError

    class Replacement:
        def __init__(self, device, **kwargs):
            assert kwargs["pair"] is False
            assert kwargs["_is_retry_client"] is True
            self.count = 0

        async def connect(self, **kwargs):
            self.count += 1
            raise BleakError("le-connection-abort-by-local")

    owners = []
    device = BLEDevice(TARGET.address, TARGET.name, {"source": "test_proxy"})
    with patch.object(
        transport.bleak_retry_connector, "BleakClientWithServiceCache", Replacement
    ):
        cls = transport.single_attempt_client_class()
        assert issubclass(cls, Replacement)
        with pytest.raises(ProtocolError, match="connection_retry_refused"):
            await transport.bleak_retry_connector.establish_connection(
                cls,
                device,
                "Aiper",
                owners=owners,
                max_attempts=1,
                pair=False,
                use_services_cache=False,
            )
    assert len(owners) == 1
    assert owners[0].count == 1


async def test_proxy_entry_flow_and_local_action_rejection(hass, radio):
    info = SimpleNamespace(address=TARGET.address, name=TARGET.name)
    with (
        patch.object(
            transport.bluetooth, "async_discovered_service_info", return_value=[info]
        ),
        patch(f"{PATH}.config_flow.open_bluez") as local,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device": TARGET.address}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == asdict(Target(TARGET.address, TARGET.name))
        await hass.async_block_till_done()
        local.assert_not_called()
    entry = result["result"]
    assert radio.clients == []
    with pytest.raises(HomeAssistantError, match="legacy diagnostic"):
        await hass.services.async_call(
            DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
        )


async def test_proxy_only_entry_polls_without_local_adapter(hass, radio):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=asdict(Target(TARGET.address, TARGET.name)),
        unique_id=TARGET.address,
        options=OPTIONS,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.last_update_success
    assert len(radio.clients) == 4
