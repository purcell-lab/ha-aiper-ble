"""Listen-only transport with fake BlueZ: no application I/O on any path."""

import asyncio
from types import SimpleNamespace

import pytest
from dbus_fast import MessageType

from custom_components.aiper_ble import listener as l
from custom_components.aiper_ble import probe as p
from custom_components.aiper_ble.protocol import (
    Listen,
    ProtocolError,
    query_frame,
)

from .helpers import TARGET
from .test_protocol import frame
from .test_query import CHAR, RESPONSE, Bus, add_key_exchange, members


@pytest.fixture(autouse=True)
def short_window(monkeypatch):
    monkeypatch.setattr(l, "LISTEN_SECONDS", 0.025)
    monkeypatch.setattr(l, "SETUP_SECONDS", 0.1)
    monkeypatch.setattr(l, "NOTIFY_CLEANUP_SECONDS", 0.05)


class ListenBus(Bus):
    def __init__(self, packets=()):
        super().__init__()
        self.packets = packets
        # Listen must not require application read or write capability.
        self.data[CHAR][p.CHAR_IF]["Flags"] = ["notify"]

    async def call(self, message):
        reply = await super().call(message)
        if message.member == "StartNotify":
            for packet in self.packets:
                self.emit(packet)
        return reply


async def run(bus, config=None):
    report = {}
    config = config or Listen()
    api = l.ListenBluez(bus, TARGET, config)
    await p.probe(api, TARGET, report, True, query=config)
    assert not bus.written
    assert "WriteValue" not in members(bus)
    assert "ReadValue" not in members(bus)
    return report


async def test_silent_full_window_is_valid_not_query_timeout():
    bus = ListenBus()
    report = await run(bus)
    assert report["status"] == "listen_complete"
    assert report["notification_count"] == report["received_bytes"] == 0
    assert report["listen_seconds_observed"] >= 0.025
    assert report["writes_completed"] == report["write_attempts"] == 0
    assert report["read_attempts"] == 0
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    for member in ("Connect", "StartNotify", "StopNotify", "Disconnect"):
        assert members(bus).count(member) == 1
    assert members(bus).index("StopNotify") < members(bus).index("Disconnect")
    assert not bus.handlers


async def test_startup_fragmented_and_coalesced_frames_retained():
    payload = frame(RESPONSE)
    bus = ListenBus([payload[:8], payload[8:] + payload])
    report = await run(bus)
    assert report["status"] == "listen_complete"
    assert report["notification_count"] == 2
    assert report["frame_count"] == 2
    assert report["protocol_responses"] == [RESPONSE, RESPONSE]
    assert report["received_bytes"] == len(payload) * 2
    assert len(report["notification_samples"]) == 2


async def test_partial_frame_is_explicit_not_invented_telemetry():
    report = await run(ListenBus([frame(RESPONSE)[:5]]))
    assert report["status"] == "listen_complete"
    assert report["partial_frame_bytes"] == 5
    assert report["frame_count"] == 0


@pytest.mark.parametrize("packets", [[b"not_base64\n"], [b"x" * 8193], [b"x"] * 33])
async def test_bad_or_excess_capture_aborts_and_cleans_up(packets):
    bus = ListenBus(packets)
    report = await run(bus)
    assert report["status"] == "failed"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


@pytest.mark.parametrize("authorised", [False, True])
async def test_missing_advertisement_requires_per_call_opt_in(authorised):
    bus = ListenBus()
    bus.data[TARGET.device_path][p.DEVICE_IF].pop("ManufacturerData")
    report = await run(bus, Listen(authorised))
    assert (report["status"] == "listen_complete") == authorised
    assert ("Connect" in members(bus)) == authorised


@pytest.mark.parametrize(
    "bad", ["ecdh", "malformed", "key_exchange", "identity", "busy"]
)
async def test_negative_evidence_vetoes_before_connection(bad):
    bus = ListenBus()
    device = bus.data[TARGET.device_path][p.DEVICE_IF]
    if bad == "ecdh":
        device["ManufacturerData"] = {0: [1]}
    elif bad == "malformed":
        device["ManufacturerData"] = {"0": [0]}
    elif bad == "key_exchange":
        add_key_exchange(bus.data)
    elif bad == "identity":
        device["Name"] = "Other"
    else:
        device["Connected"] = True
    report = await run(bus, Listen(True))
    assert report["status"] == "failed"
    assert "Connect" not in members(bus)
    assert "Disconnect" not in members(bus)


@pytest.mark.parametrize(
    "bad", ["already_notifying", "no_notify", "secure", "wrong_parent"]
)
async def test_unsafe_endpoint_never_subscribed(bad):
    bus = ListenBus()
    props = bus.data[CHAR][p.CHAR_IF]
    if bad == "already_notifying":
        props["Notifying"] = True
    elif bad == "no_notify":
        props["Flags"] = ["read", "write"]
    elif bad == "secure":
        props["Flags"] = ["notify", "authorize"]
    else:
        props["Service"] = "/wrong"
    report = await run(bus)
    assert report["status"] == "failed"
    assert "StartNotify" not in members(bus)
    assert report["cleanup"] == "disconnected_confirmed"


async def test_protocol_change_during_subscription_aborts():
    bus = ListenBus()
    bus.revalidate = add_key_exchange
    report = await run(bus)
    assert report["status"] == "failed"
    assert report["error_code"] == "ecdh_gatt_unsupported"
    assert report["notification_cleanup"] == "stop_confirmed"


@pytest.mark.parametrize("operation", ["StartNotify", "StopNotify", "RemoveMatch"])
async def test_failed_subscription_cleanup_remains_visible(operation):
    bus = ListenBus()
    bus.failure = operation
    report = await run(bus)
    assert report["status"] == (
        "failed" if operation == "StartNotify" else "cleanup_requires_review"
    )
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


async def test_timeout_start_notify_still_stops_and_disconnects():
    bus = ListenBus()
    bus.stall = "StartNotify"
    report = await run(bus)
    assert report["status"] == "failed"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_cancel_listen_still_cleans_up(monkeypatch):
    monkeypatch.setattr(l, "LISTEN_SECONDS", 60)
    bus = ListenBus()
    report = {}
    api = l.ListenBluez(bus, TARGET, Listen())
    task = asyncio.create_task(p.probe(api, TARGET, report, True, query=Listen()))
    while report.get("stage") != "listen":
        await asyncio.sleep(0)
    task.cancel()
    await task
    assert report["status"] == "interrupted"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_wrong_signal_sender_and_path_are_ignored():
    class WrongBus(ListenBus):
        async def call(self, message):
            reply = await super().call(message)
            if message.member == "StartNotify":
                self.emit(frame(RESPONSE), sender=":1.99")
                self.emit(frame(RESPONSE), path=CHAR + "9")
            return reply

    report = await run(WrongBus())
    assert report["status"] == "listen_complete"
    assert report["notification_count"] == 0


async def test_no_cached_value_treated_as_live_notification():
    bus = ListenBus()
    bus.data[CHAR][p.CHAR_IF]["Value"] = frame(RESPONSE)
    report = await run(bus)
    assert report["notification_count"] == 0


async def test_application_io_denied_even_with_forged_permit():
    bus = ListenBus()
    api = l.ListenBluez(bus, TARGET, Listen())
    api._query_path = CHAR
    for member in ("ReadValue", "WriteValue"):
        api._permit = (CHAR, p.CHAR_IF, member, "", [])
        with pytest.raises(p.ProbeError):
            await api.call(CHAR, p.CHAR_IF, member)
        with pytest.raises(p.ProbeError):
            await api._exact(CHAR, member)
    assert not bus.calls
    with pytest.raises(ProtocolError):
        query_frame(Listen())


async def test_one_shot_transport_cannot_be_reused():
    bus = ListenBus()
    api = l.ListenBluez(bus, TARGET, Listen())
    await p.probe(api, TARGET, {}, True, query=Listen())
    with pytest.raises(ProtocolError, match="listen_already_attempted"):
        await api.query_once({})


@pytest.mark.parametrize("error", ["InProgress", "AlreadyExists"])
async def test_ambiguous_notification_ownership_is_not_stopped(error):
    class BusyBus(ListenBus):
        async def call(self, message):
            if message.member == "StartNotify":
                self.calls.append(message)
                return SimpleNamespace(
                    message_type=MessageType.ERROR,
                    error_name="org.bluez.Error." + error,
                )
            return await super().call(message)

    bus = BusyBus()
    report = await run(bus)
    assert report["status"] == "cleanup_requires_review"
    assert report["notification_cleanup"] == "ownership_uncertain"
    assert "StopNotify" not in members(bus)
    assert report["cleanup"] == "disconnected_confirmed"


async def test_disconnect_during_silent_window_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(l, "LISTEN_SECONDS", 2)

    class DroppedBus(ListenBus):
        async def call(self, message):
            if message.member == "GetAll" and self.data[CHAR][p.CHAR_IF].get(
                "Notifying"
            ):
                self.data[TARGET.device_path][p.DEVICE_IF]["Connected"] = False
            return await super().call(message)

    report = await run(DroppedBus())
    assert report["status"] == "failed"
    assert report["error_code"] == "connection_lost_while_listening"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_open_bluez_selects_listener_and_closes_bus(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    bus = Mock()
    bus.connect = AsyncMock()
    monkeypatch.setattr(p, "MessageBus", lambda **kwargs: bus)
    async with p.open_bluez(TARGET, query=Listen()) as api:
        assert isinstance(api, l.ListenBluez)
    bus.connect.assert_awaited_once()
    bus.disconnect.assert_called_once()
