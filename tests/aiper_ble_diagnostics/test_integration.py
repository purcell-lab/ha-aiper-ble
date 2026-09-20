"""Real HA config flow, service, sensor, diagnostics and unload tests."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble_diagnostics import Runtime
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiper_ble_diagnostics.telemetry import QueryBluez

from .helpers import TARGET, Fake
from .test_protocol import frame
from .test_query import S1_RESPONSE, Bus

PATH = "custom_components.aiper_ble_diagnostics"


@pytest.fixture
def fake_bluez():
    api = Fake()

    @asynccontextmanager
    async def fake_open(target=None, *, allow_read=False):
        api.allow_read = allow_read
        yield api

    with (
        patch(f"{PATH}.open_bluez", fake_open),
        patch(f"{PATH}.config_flow.open_bluez", fake_open),
    ):
        yield api


@pytest.fixture
async def entry(hass, fake_bluez):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), unique_id=TARGET.address
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_never_queries_or_connects(entry, fake_bluez, hass):
    assert fake_bluez.calls == []
    assert isinstance(entry.runtime_data, Runtime)
    state = hass.states.get("sensor.aiper_ble_discovery_result")
    assert state.state == "never_run"


async def test_manual_flow_uses_metadata_only(hass, fake_bluez):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": TARGET.device_path}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == asdict(TARGET)
    await hass.async_block_till_done()
    assert set(fake_bluez.calls) == {"metadata"}


async def test_duplicate_flow_aborts(hass, entry, fake_bluez):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": TARGET.device_path}
    )
    assert result["reason"] == "already_configured"


async def test_no_devices_aborts_without_scanning(hass, fake_bluez):
    fake_bluez.data = {}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["reason"] == "no_local_aiper"
    assert fake_bluez.calls == ["metadata"]


async def test_preflight_service(entry, fake_bluez, hass):
    result = await hass.services.async_call(
        DOMAIN,
        "preflight",
        {"entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert result["status"] == "preflight_passed_no_connection"
    assert fake_bluez.calls == ["metadata"]


async def test_discover_requires_confirmation(entry, fake_bluez, hass):
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "discover",
            {"entry_id": entry.entry_id, "confirm_app_closed": False},
            blocking=True,
            return_response=True,
        )
    assert fake_bluez.calls == []


async def test_discover_updates_sensor_and_redacts_diagnostics(entry, fake_bluez, hass):
    result = await hass.services.async_call(
        DOMAIN,
        "discover",
        {"entry_id": entry.entry_id, "confirm_app_closed": True},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert result["cleanup"] == "disconnected_confirmed"
    assert (
        hass.states.get("sensor.aiper_ble_discovery_result").state
        == "discovery_complete"
    )
    previous = list(fake_bluez.calls)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    for private in (TARGET.name, TARGET.address, TARGET.adapter_address):
        assert private not in str(diagnostic)
    assert fake_bluez.calls == previous
    assert entry.runtime_data.last_result["before"]["Address"] == TARGET.address
    assert not fake_bluez.allow_read
    assert "read" not in fake_bluez.calls


async def test_action_without_response_still_stores_result(entry, fake_bluez, hass):
    await hass.services.async_call(
        DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
    )
    assert entry.runtime_data.last_result["status"] == "preflight_passed_no_connection"


async def test_concurrency_and_unload_cleanup(entry, fake_bluez, hass):
    started = asyncio.Event()

    async def stalled_connect():
        fake_bluez.calls.append("connect")
        started.set()
        await asyncio.Event().wait()

    fake_bluez.connect = stalled_connect
    running = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "discover",
            {"entry_id": entry.entry_id, "confirm_app_closed": True},
            blocking=True,
            return_response=True,
        )
    )
    await started.wait()
    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call(
            DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
        await running
    assert fake_bluez.calls.count("disconnect") == 1


async def test_unloaded_entry_cannot_start_probe(entry, fake_bluez, hass):
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
        )
    assert fake_bluez.calls == []


@pytest.mark.parametrize("field", ["confirm_app_closed", "confirm_read_only"])
async def test_read_requires_both_confirmations(entry, fake_bluez, hass, field):
    data = {
        "entry_id": entry.entry_id,
        "confirm_app_closed": True,
        "confirm_read_only": True,
        field: False,
    }
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "read_once", data, blocking=True)
    assert fake_bluez.calls == []


async def test_read_raw_response_private_and_diagnostics_cached(
    entry, fake_bluez, hass
):
    result = await hass.services.async_call(
        DOMAIN,
        "read_once",
        {
            "entry_id": entry.entry_id,
            "confirm_app_closed": True,
            "confirm_read_only": True,
        },
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert fake_bluez.allow_read
    assert result["status"] == "read_complete"
    assert result["sample_hex"] == "012345ab"
    assert result["sample_bytes"] == 4
    assert fake_bluez.calls.count("read") == 1
    assert result["cleanup"] == "disconnected_confirmed"
    state = hass.states.get("sensor.aiper_ble_discovery_result")
    assert state.state == "read_complete"
    assert "sample_hex" not in state.attributes
    assert "012345ab" not in str(state)
    previous = list(fake_bluez.calls)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert "012345ab" not in str(diagnostic)
    assert diagnostic["last_result"]["sample_bytes"] == 4
    assert diagnostic["last_result"]["cleanup"] == "disconnected_confirmed"
    assert fake_bluez.calls == previous
    assert entry.runtime_data.last_result["sample_hex"] == "012345ab"


async def test_unload_cancels_read_and_disconnects(entry, fake_bluez, hass):
    started = asyncio.Event()

    async def stalled_read():
        started.set()
        await asyncio.Event().wait()

    fake_bluez.read_sample = stalled_read
    running = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "read_once",
            {
                "entry_id": entry.entry_id,
                "confirm_app_closed": True,
                "confirm_read_only": True,
            },
            blocking=True,
            return_response=True,
        )
    )
    await started.wait()
    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call(
            DOMAIN,
            "discover",
            {"entry_id": entry.entry_id, "confirm_app_closed": True},
            blocking=True,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
        await running
    assert fake_bluez.calls.count("disconnect") == 1


def query_data(entry):
    return {
        "entry_id": entry.entry_id,
        "checksum_mode": "omit_empty_crc",
        "write_mode": "request",
        "confirm_app_closed": True,
        "confirm_query_write": True,
        "confirm_notifications": True,
    }


@pytest.mark.parametrize("query_type", ["OpInfo", "S1_INFO", "INFO", "WARN"])
async def test_protocol_preview_never_opens_bus(entry, fake_bluez, hass, query_type):
    result = await hass.services.async_call(
        DOMAIN,
        "protocol_preview",
        {
            "entry_id": entry.entry_id,
            "query_type": query_type,
            "checksum_mode": "omit_empty_crc",
            "write_mode": "command",
        },
        blocking=True,
        return_response=True,
    )
    assert result["request_json"] == (
        {"type": "OpInfo", "data": {}}
        if query_type == "OpInfo"
        else {
            "type": "Machine",
            "data": {"cmd": f"AT+{query_type}?"},
            "chksum": {"S1_INFO": 49921, "INFO": 10442, "WARN": 10501}[query_type],
        }
    )
    assert fake_bluez.calls == []
    assert entry.runtime_data.last_result["status"] == "never_run"


@pytest.mark.parametrize(
    "field",
    [
        "confirm_app_closed",
        "confirm_query_write",
        "confirm_notifications",
    ],
)
@pytest.mark.parametrize("query_type", ["OpInfo", "S1_INFO", "INFO", "WARN"])
async def test_query_requires_three_confirmations(
    entry, fake_bluez, hass, field, query_type
):
    data = query_data(entry)
    data["query_type"] = query_type
    data[field] = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "query_once", data, blocking=True)
    assert fake_bluez.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("checksum_mode", "auto"),
        ("write_mode", "auto"),
        ("command", "FastTask"),
        ("data", {"mode": "clean"}),
        ("query_type", "Machine"),
        ("query_type", "S1_INFO=1"),
        ("query_type", "AT+S1_INFO?"),
    ],
)
async def test_query_rejects_unsafe_service_input(
    entry, fake_bluez, hass, field, value
):
    import voluptuous as vol

    data = query_data(entry)
    data[field] = value
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "query_once", data, blocking=True)
    assert fake_bluez.calls == []


async def test_protocol_service_privacy_and_real_framework(entry, hass):
    bus = Bus()

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        assert not allow_read
        yield QueryBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", fake_open):
        result = await hass.services.async_call(
            DOMAIN, "query_once", query_data(entry), blocking=True, return_response=True
        )
    assert result["status"] == "query_complete"
    assert result["telemetry_candidates"] == {"cap": 80, "temp": 26.5}
    await hass.async_block_till_done()
    state = hass.states.get("sensor.aiper_ble_discovery_result")
    assert state.state == "query_complete"
    assert "PRIVATE_SERIAL" not in str(state)
    assert "telemetry_candidates" not in state.attributes
    previous = len(bus.calls)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert "PRIVATE_SERIAL" not in str(diagnostic)
    assert diagnostic["last_result"]["protocol_response"] == "**REDACTED**"
    assert diagnostic["last_result"]["telemetry_candidates"]["temp"] == 26.5
    assert len(bus.calls) == previous


async def test_s1_info_service_and_diagnostic_privacy(entry, hass):
    bus = Bus()
    bus.response = frame(
        {**S1_RESPONSE, "data": {**S1_RESPONSE["data"], "serial": "PRIVATE_S1_SERIAL"}}
    )

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        yield QueryBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", fake_open):
        result = await hass.services.async_call(
            DOMAIN,
            "query_once",
            {**query_data(entry), "query_type": "S1_INFO"},
            blocking=True,
            return_response=True,
        )
    assert result["status"] == "query_complete"
    assert result["mode"] == "connect_notify_s1_info_disconnect"
    assert result["telemetry_candidates"]["temperature_celsius"] == 26.5
    await hass.async_block_till_done()
    state = hass.states.get("sensor.aiper_ble_discovery_result")
    assert "telemetry_candidates" not in state.attributes
    assert "PRIVATE_S1_SERIAL" not in str(state)
    assert "+S1_INFO:" not in str(state)
    before = len(bus.calls)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostic["last_result"]["protocol_response"] == "**REDACTED**"
    assert "PRIVATE_S1_SERIAL" not in str(diagnostic)
    assert "+S1_INFO:" not in str(diagnostic)
    assert (
        diagnostic["last_result"]["telemetry_candidates"]["temperature_celsius"] == 26.5
    )
    assert len(bus.calls) == before


@pytest.mark.parametrize("confirmation", [None, False, True])
async def test_legacy_probe_is_explicit_per_service_call(entry, hass, confirmation):
    bus = Bus()
    bus.data[TARGET.device_path]["org.bluez.Device1"].pop("ManufacturerData")

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        yield QueryBluez(bus, target, query)

    data = query_data(entry)
    if confirmation is not None:
        data["confirm_legacy_probe"] = confirmation
    with patch(f"{PATH}.open_bluez", fake_open):
        if confirmation is True:
            result = await hass.services.async_call(
                DOMAIN, "query_once", data, blocking=True, return_response=True
            )
            assert result["legacy_probe_authorised"] is True
            assert result["status"] == "query_complete"
        else:
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    DOMAIN, "query_once", data, blocking=True, return_response=True
                )
            assert "Connect" not in [m.member for m in bus.calls]
    assert "confirm_legacy_probe" not in entry.data
    assert "confirm_legacy_probe" not in entry.options


async def test_safe_error_codes_survive_diagnostics(entry, fake_bluez, hass):
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "query_once", query_data(entry), blocking=True
        )
    # This fixture intentionally does not implement the new query transport;
    # the service still records only a fixed setup code rather than error text.
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostic["last_result"]["error_code"] == "transport_setup_failed"
    assert diagnostic["last_result"]["error"] == "**REDACTED**"


async def test_query_unload_cancellation_and_concurrency(entry, hass):
    bus = Bus()
    bus.stall = "WriteValue"

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        yield QueryBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", fake_open):
        running = asyncio.create_task(
            hass.services.async_call(
                DOMAIN,
                "query_once",
                query_data(entry),
                blocking=True,
                return_response=True,
            )
        )
        await bus.signal_started.wait()
        with pytest.raises(HomeAssistantError, match="already running"):
            await hass.services.async_call(
                DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
            )
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
            await running
    members = [m.member for m in bus.calls]
    assert members.count("StartNotify") == members.count("StopNotify") == 1
    assert members.count("Connect") == members.count("Disconnect") == 1
    assert not bus.handlers


def listen_data(entry):
    return {
        "entry_id": entry.entry_id,
        "confirm_app_closed": True,
        "confirm_notifications": True,
        "confirm_legacy_probe": True,
    }


@pytest.mark.parametrize("field", ["confirm_app_closed", "confirm_notifications"])
async def test_listen_requires_confirmations(entry, fake_bluez, hass, field):
    data = listen_data(entry)
    data[field] = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "listen_once", data, blocking=True)
    assert not fake_bluez.calls


@pytest.mark.parametrize("field", ["query_type", "data", "write_mode", "duration"])
async def test_listen_schema_disallows_commands_and_duration_overrides(
    entry, fake_bluez, hass, field
):
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "listen_once",
            {**listen_data(entry), field: "arbitrary"},
            blocking=True,
        )
    assert not fake_bluez.calls


async def test_listen_service_privacy_and_no_application_io(entry, hass, monkeypatch):
    from custom_components.aiper_ble_diagnostics import listener

    from .test_listener import ListenBus
    from .test_query import RESPONSE

    monkeypatch.setattr(listener, "LISTEN_SECONDS", 0.01)
    bus = ListenBus([frame(RESPONSE)])

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        assert not allow_read
        yield listener.ListenBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", fake_open):
        result = await hass.services.async_call(
            DOMAIN,
            "listen_once",
            listen_data(entry),
            blocking=True,
            return_response=True,
        )
    assert result["status"] == "listen_complete"
    assert result["mode"] == "connect_listen_only_disconnect"
    assert result["frame_count"] == 1
    assert not bus.written
    await hass.async_block_till_done()
    state = hass.states.get("sensor.aiper_ble_discovery_result")
    assert state.state == "listen_complete"
    assert "PRIVATE_SERIAL" not in str(state)
    assert "protocol_responses" not in state.attributes
    before = len(bus.calls)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostic["last_result"]["protocol_responses"] == "**REDACTED**"
    assert diagnostic["last_result"]["notification_samples"] == "**REDACTED**"
    assert "PRIVATE_SERIAL" not in str(diagnostic)
    assert len(bus.calls) == before
    assert "confirm_legacy_probe" not in entry.data


async def test_listen_unload_cancellation_and_concurrency(entry, hass):
    from custom_components.aiper_ble_diagnostics.listener import ListenBluez

    bus = Bus()
    bus.stall = "StartNotify"

    @asynccontextmanager
    async def fake_open(target, *, allow_read=False, query=None):
        yield ListenBluez(bus, target, query)

    with patch(f"{PATH}.open_bluez", fake_open):
        running = asyncio.create_task(
            hass.services.async_call(
                DOMAIN,
                "listen_once",
                listen_data(entry),
                blocking=True,
                return_response=True,
            )
        )
        await bus.signal_started.wait()
        with pytest.raises(HomeAssistantError, match="already running"):
            await hass.services.async_call(
                DOMAIN, "preflight", {"entry_id": entry.entry_id}, blocking=True
            )
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
            await running
    members = [message.member for message in bus.calls]
    assert members.count("StartNotify") == members.count("StopNotify") == 1
    assert members.count("Connect") == members.count("Disconnect") == 1
    assert "WriteValue" not in members and "ReadValue" not in members
    assert not bus.handlers
