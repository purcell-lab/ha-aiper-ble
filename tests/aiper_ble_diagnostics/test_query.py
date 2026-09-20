"""Fake D-Bus exchange, exact outgoing frames, and every cleanup failure path."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_fast import Message, MessageType, Variant

from custom_components.aiper_ble_diagnostics import probe as p
from custom_components.aiper_ble_diagnostics import telemetry as t
from custom_components.aiper_ble_diagnostics.protocol import Query, query_frame

from .helpers import TARGET, objects
from .test_protocol import frame

SERVICE = TARGET.device_path + "/service001"
CHAR = SERVICE + "/char002"
QUERY = Query("omit_empty_crc", "request")
LEGACY_PROBE = Query("omit_empty_crc", "request", True)
S1_QUERY = Query("omit_empty_crc", "request", query_type="S1_INFO")
S1_RESPONSE = {"type": "Machine", "data": {"report": "+S1_INFO:265,1\r\n"}}
RESPONSE = {
    "type": "OpInfo",
    "data": {"Machine": {"cap": 80, "temp": 26.5}, "serial": "PRIVATE_SERIAL"},
}


class Bus:
    """Model BlueZ messages; no system bus or real radio is opened."""

    def __init__(self):
        self.data = objects()
        self.data[TARGET.device_path][p.DEVICE_IF]["ManufacturerData"] = {0: [0]}
        self.data[CHAR][p.CHAR_IF]["MTU"] = 23
        self.handlers = []
        self.calls = []
        self.written = bytearray()
        self.response = frame(RESPONSE)
        self.failure = None
        self.stall = None
        self.revalidate = None
        self.signal_started = asyncio.Event()

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    def emit(self, value, *, sender=":1.42", path=CHAR):
        message = Message(
            message_type=MessageType.SIGNAL,
            sender=sender,
            path=path,
            interface=t.PROPERTIES,
            member="PropertiesChanged",
            signature="sa{sv}as",
            body=[p.CHAR_IF, {"Value": Variant("ay", value)}, []],
        )
        for handler in self.handlers:
            handler(message)

    async def call(self, message):
        self.calls.append(message)
        member = message.member
        if member == self.stall:
            self.signal_started.set()
            await asyncio.Event().wait()
        if member == self.failure:
            return SimpleNamespace(
                message_type=MessageType.ERROR,
                error_name="org.bluez.Error.NotPermitted",
            )
        body = []
        if member == "GetManagedObjects":
            if self.revalidate and self.data[CHAR][p.CHAR_IF].get("Notifying"):
                self.revalidate(self.data)
            body = [copy.deepcopy(self.data)]
        elif member == "GetAll":
            body = [copy.deepcopy(self.data[TARGET.device_path][p.DEVICE_IF])]
        elif member == "Connect":
            self.data[TARGET.device_path][p.DEVICE_IF].update(
                Connected=True, ServicesResolved=True
            )
        elif member == "Disconnect":
            self.data[TARGET.device_path][p.DEVICE_IF].update(
                Connected=False, ServicesResolved=False
            )
        elif member == "GetNameOwner":
            body = [":1.42"]
        elif member == "StartNotify":
            self.data[CHAR][p.CHAR_IF]["Notifying"] = True
        elif member == "StopNotify":
            self.data[CHAR][p.CHAR_IF]["Notifying"] = False
        elif member == "WriteValue":
            self.written.extend(message.body[0])
            if self.written.endswith(b"\n") and self.response is not None:
                self.emit(self.response[:8])
                self.emit(self.response[8:])
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=body)


async def run(bus, query=QUERY):
    report = {}
    api = t.QueryBluez(bus, TARGET, query)
    await p.probe(api, TARGET, report, True, query=query)
    return report


def members(bus):
    return [call.member for call in bus.calls]


def add_key_exchange(data, *, device=TARGET.device_path):
    service = device + "/service001"
    data[service + "/char064"] = {
        p.CHAR_IF: {
            "Service": service,
            "UUID": p.KEY_EXCHANGE_CHARACTERISTIC,
            "Flags": ["write", "notify"],
        }
    }


@pytest.mark.parametrize("manufacturer", [{}, {0: []}, {0: b""}, {42: [0]}])
async def test_explicit_probe_requires_resolved_legacy_endpoint(manufacturer):
    bus = Bus()
    bus.data[TARGET.device_path][p.DEVICE_IF]["ManufacturerData"] = manufacturer
    report = await run(bus, LEGACY_PROBE)
    assert report["status"] == "query_complete"
    assert report["protocol_hint"] == "unknown"
    assert report["legacy_probe_authorised"] is True
    assert report["protocol_evidence"] == (
        "explicit_probe_resolved_legacy_gatt_no_advertisement"
    )
    assert report["wire_compatibility"] == "unverified"
    assert bytes(bus.written) == b"aRYiAWJRdEIweyYxfFI5Wj4WMhlmVXRCaUkr\n"
    assert members(bus).count("Connect") == members(bus).count("Disconnect") == 1


@pytest.mark.parametrize("manufacturer", [{0: [1]}, {0: [True]}, {"0": [0]}, []])
async def test_explicit_probe_never_overrides_negative_advertisement(manufacturer):
    bus = Bus()
    bus.data[TARGET.device_path][p.DEVICE_IF]["ManufacturerData"] = manufacturer
    report = await run(bus, LEGACY_PROBE)
    assert report["error_code"] in {"ecdh_unsupported", "protocol_malformed"}
    assert "Connect" not in members(bus)
    assert not bus.written


@pytest.mark.parametrize("query", [QUERY, LEGACY_PROBE])
async def test_cached_key_exchange_profile_vetoes_connection(query):
    bus = Bus()
    add_key_exchange(bus.data)
    report = await run(bus, query)
    assert report["error_code"] == "ecdh_gatt_unsupported"
    assert "Connect" not in members(bus)
    assert not bus.written


@pytest.mark.parametrize("when", ["before_connect", "connected", "notifying"])
async def test_new_key_exchange_profile_vetoes_before_application_write(when):
    class ChangingBus(Bus):
        async def call(self, message):
            if when == "before_connect" and message.member == "GetManagedObjects":
                if any(m.member == "GetManagedObjects" for m in self.calls):
                    add_key_exchange(self.data)
            if message.member == {
                "connected": "Connect",
                "notifying": "StartNotify",
            }.get(when):
                add_key_exchange(self.data)
            return await super().call(message)

    bus = ChangingBus()
    report = await run(bus, LEGACY_PROBE)
    assert report["error_code"] == "ecdh_gatt_unsupported"
    assert "WriteValue" not in members(bus)
    if when == "before_connect":
        assert "Connect" not in members(bus)
    else:
        assert report["cleanup"] == "disconnected_confirmed"
        if when == "notifying":
            assert "StopNotify" in members(bus)
        else:
            assert "StartNotify" not in members(bus)


async def test_other_device_key_exchange_does_not_select_our_protocol():
    bus = Bus()
    add_key_exchange(bus.data, device="/org/bluez/hci0/dev_00_00_00_00_00_01")
    report = await run(bus)
    assert report["status"] == "query_complete"


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_parent", "identity"])
async def test_unknown_advertisement_opt_in_still_rejects_unsafe_endpoint(change):
    bus = Bus()
    bus.data[TARGET.device_path][p.DEVICE_IF].pop("ManufacturerData")
    if change == "missing":
        del bus.data[CHAR]
    elif change == "duplicate":
        bus.data[CHAR + "9"] = copy.deepcopy(bus.data[CHAR])
    elif change == "wrong_parent":
        bus.data[CHAR][p.CHAR_IF]["Service"] = "/other"
    else:
        bus.data[TARGET.device_path][p.DEVICE_IF]["Name"] = "Aiper-Other"
    report = await run(bus, LEGACY_PROBE)
    assert report["status"] == "failed"
    assert "StartNotify" not in members(bus)
    assert not bus.written


async def test_one_bounded_request_exact_chunks_and_cleanup():
    bus = Bus()
    report = await run(bus)
    assert report["status"] == "query_complete"
    assert bytes(bus.written) == query_frame(QUERY)
    writes = [m for m in bus.calls if m.member == "WriteValue"]
    assert len(writes) > 1
    assert all(
        m.path == CHAR
        and m.signature == "aya{sv}"
        and len(m.body[0]) <= 20
        and m.body[1]["type"].value == "request"
        for m in writes
    )
    assert report["telemetry_candidates"] == {"cap": 80, "temp": 26.5}
    assert report["protocol_response"] == RESPONSE
    assert report["response_match"] == "type_only_no_request_id"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert members(bus).count("StartNotify") == members(bus).count("StopNotify") == 1
    assert members(bus).count("Connect") == members(bus).count("Disconnect") == 1
    assert "ReadValue" not in members(bus)
    assert not bus.handlers
    assert members(bus).index("StopNotify") < members(bus).index("Disconnect")


@pytest.mark.parametrize("missing_advertisement", [False, True])
async def test_s1_info_one_request_only_and_fragmented_response(missing_advertisement):
    bus = Bus()
    if missing_advertisement:
        bus.data[TARGET.device_path][p.DEVICE_IF].pop("ManufacturerData")
    query = Query("omit_empty_crc", "request", missing_advertisement, "S1_INFO")
    # Coalesced unrelated OpInfo and other AT replies must not end this query.
    bus.response = (
        frame(RESPONSE)
        + frame({"type": "Machine", "data": {"ack": "+OTHER:1\r\n"}})
        + frame(S1_RESPONSE)
    )
    report = await run(bus, query)
    assert report["status"] == "query_complete"
    assert report["command"] == "S1_INFO"
    assert bytes(bus.written) == query_frame(query)
    assert bytes(bus.written).count(b"\n") == 1
    assert report["telemetry_candidates"] == {
        "temperature_raw": 265.0,
        "temperature_celsius": 26.5,
        "solar_status": 1,
    }
    assert report["response_match"] == "type_and_s1_info_prefix_no_request_id"
    assert report["temperature_interpretation"] == (
        "app_3_6_1_celsius_div10_sensor_location_unverified"
    )
    assert report["response_checksum_validation"] == "not_verified"
    assert members(bus).count("StartNotify") == members(bus).count("StopNotify") == 1
    assert members(bus).count("Connect") == members(bus).count("Disconnect") == 1
    assert "ReadValue" not in members(bus)
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


async def test_s1_info_malformed_response_cleans_up_without_retry():
    bus = Bus()
    bus.response = frame({"type": "Machine", "data": {"ack": "+S1_INFO:NaN,1\r\n"}})
    report = await run(bus, S1_QUERY)
    assert report["error_code"] == "invalid_s1_info_response"
    assert "telemetry_candidates" not in report
    assert bytes(bus.written) == query_frame(S1_QUERY)
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


@pytest.mark.parametrize("evidence", ["missing", "ecdh", "key_exchange", "malformed"])
async def test_s1_info_inherits_preconnect_protocol_guards(evidence):
    bus = Bus()
    if evidence == "key_exchange":
        add_key_exchange(bus.data)
    else:
        bus.data[TARGET.device_path][p.DEVICE_IF]["ManufacturerData"] = {
            "missing": {},
            "ecdh": {0: [1]},
            "malformed": {0: [True]},
        }[evidence]
    report = await run(bus, S1_QUERY)
    assert report["status"] == "failed"
    assert "Connect" not in members(bus)
    assert not bus.written


async def test_app_command_transport_requires_actual_flag():
    bus = Bus()
    report = await run(bus, Query("omit_empty_crc", "command"))
    assert report["error_code"] == "unsupported_write_or_notify_flags"
    assert "StartNotify" not in members(bus)
    assert "WriteValue" not in members(bus)
    assert report["cleanup"] == "disconnected_confirmed"
    bus = Bus()
    bus.data[CHAR][p.CHAR_IF]["Flags"].append("write-without-response")
    report = await run(bus, Query("omit_empty_crc", "command"))
    assert report["status"] == "query_complete"
    assert all(
        m.body[1]["type"].value == "command"
        for m in bus.calls
        if m.member == "WriteValue"
    )


@pytest.mark.parametrize(
    "manufacturer,code", [(None, "protocol_unknown"), ([1], "ecdh_unsupported")]
)
async def test_missing_or_ecdh_protocol_never_connects(manufacturer, code):
    bus = Bus()
    bus.data[TARGET.device_path][p.DEVICE_IF]["ManufacturerData"] = {0: manufacturer}
    report = await run(bus)
    assert report["error_code"] == code
    assert "Connect" not in members(bus)
    assert "WriteValue" not in members(bus)


@pytest.mark.parametrize(
    "change",
    ["notifying", "paired", "no_notify", "security", "duplicate", "wrong_parent"],
)
async def test_unsafe_endpoint_no_application_write(change):
    bus = Bus()
    char = bus.data[CHAR][p.CHAR_IF]
    if change == "notifying":
        char["Notifying"] = True
    elif change == "paired":
        bus.data[TARGET.device_path][p.DEVICE_IF]["Paired"] = True
    elif change == "no_notify":
        char["Flags"] = ["write"]
    elif change == "security":
        char["Flags"].append("encrypt-write")
    elif change == "duplicate":
        bus.data[CHAR + "9"] = copy.deepcopy(bus.data[CHAR])
    else:
        char["Service"] = "/other"
    report = await run(bus)
    assert report["status"] == "failed"
    assert "StartNotify" not in members(bus)
    assert "WriteValue" not in members(bus)


async def test_revalidation_changes_stop_before_write():
    bus = Bus()
    bus.revalidate = lambda data: data[TARGET.device_path][p.DEVICE_IF].update(
        ManufacturerData={0: [1]}
    )
    report = await run(bus)
    assert report["error_code"] == "ecdh_unsupported"
    assert "WriteValue" not in members(bus)
    assert "StopNotify" in members(bus)
    assert report["cleanup"] == "disconnected_confirmed"


@pytest.mark.parametrize(
    "failure", ["StartNotify", "WriteValue", "AddMatch", "GetNameOwner"]
)
async def test_dbus_failure_no_retry_disconnects(failure):
    bus = Bus()
    bus.failure = failure
    report = await run(bus)
    assert report["status"] in {"failed", "cleanup_requires_review"}
    assert report["error_code"] == "bluez_NotPermitted"
    assert members(bus).count(failure) == 1
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


@pytest.mark.parametrize("failure", ["StopNotify", "RemoveMatch", "Disconnect"])
async def test_cleanup_failure_never_reports_success(failure):
    bus = Bus()
    bus.failure = failure
    report = await run(bus)
    assert report["status"] == "cleanup_requires_review"
    assert not bus.handlers
    assert members(bus).count("Disconnect") == 1


@pytest.mark.parametrize("query", [QUERY, S1_QUERY])
async def test_response_timeout_no_retry(monkeypatch, query):
    bus = Bus()
    bus.response = None
    monkeypatch.setattr(t, "EXCHANGE_SECONDS", 0.02)
    report = await run(bus, query)
    assert report["error_code"] == "timeout"
    assert report["failure_stage"] == "query_response"
    assert bytes(bus.written) == query_frame(query)
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


@pytest.mark.parametrize("stall", ["StartNotify", "WriteValue"])
@pytest.mark.parametrize("query", [QUERY, S1_QUERY])
async def test_cancelled_exchange_cleans_up(stall, query):
    bus = Bus()
    bus.stall = stall
    task = asyncio.create_task(run(bus, query))
    await bus.signal_started.wait()
    task.cancel()
    report = await task
    assert report["status"] == "interrupted"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


@pytest.mark.parametrize(
    "response,code",
    [
        (b"%%%\n", "invalid_frame"),
        (b"x" * 4097, "frame_limit"),
        (b"x" * 8200, "capture_limit"),
    ],
)
async def test_malformed_response_cleanup(response, code):
    bus = Bus()
    bus.response = response
    report = await run(bus)
    assert report["error_code"] == code
    assert "protocol_response" not in report
    assert report["cleanup"] == "disconnected_confirmed"


async def test_exact_permits_and_one_shot_even_after_failure():
    bus = Bus()
    api = t.QueryBluez(bus, TARGET, QUERY)
    for member in ("WriteValue", "StartNotify", "StopNotify", "ReadValue", "Pair"):
        with pytest.raises(p.ProbeError):
            await api.call(CHAR, p.CHAR_IF, member)
        with pytest.raises(p.ProbeError):
            await api._exact(CHAR, member)
    api.objects = AsyncMock(side_effect=RuntimeError("private"))
    with pytest.raises(RuntimeError):
        await api.query_once({})
    with pytest.raises(ValueError, match="query_already_attempted"):
        await api.query_once({})
    assert not bus.calls


async def test_unrelated_signals_and_response_types_ignored():
    class NoisyBus(Bus):
        def emit(self, value, **kwargs):
            super().emit(b"bad\n", sender=":1.99")
            super().emit(b"bad\n", path="/unrelated")
            if not getattr(self, "noise_sent", False):
                super().emit(frame({"type": "OpInfoReport", "data": {}}))
                self.noise_sent = True
            super().emit(value)

    bus = NoisyBus()
    bus.response = frame({"type": "opinfo", "data": {}})
    report = await run(bus)
    assert report["status"] == "query_complete"
    assert report["protocol_response"] == {"type": "opinfo", "data": {}}
    assert report["telemetry_candidates"] == {}


async def test_signal_queue_overflow_is_bounded_and_fails_closed():
    class FloodBus(Bus):
        def emit(self, value, **kwargs):
            for _ in range(40):
                super().emit(b"")

    bus = FloodBus()
    report = await run(bus)
    assert report["error_code"] == "notification_queue_overflow"
    assert "protocol_response" not in report
    assert report["cleanup"] == "disconnected_confirmed"
    assert not bus.handlers


async def test_query_does_not_require_read_flag():
    bus = Bus()
    bus.data[CHAR][p.CHAR_IF]["Flags"] = ["write", "notify"]
    report = await run(bus)
    assert report["status"] == "query_complete"
    assert "ReadValue" not in members(bus)


async def test_no_query_without_connect_authorisation_or_with_raw_read():
    for connect, read in ((False, False), (True, True)):
        bus = Bus()
        report = {}
        api = t.QueryBluez(bus, TARGET, QUERY)
        await p.probe(api, TARGET, report, connect, query=QUERY, read=read)
        assert report["status"] == "failed"
        assert not bus.calls


async def test_unknown_model_does_not_connect():
    bus = Bus()
    target = p.Target(
        TARGET.address,
        "Aiper-Scuba X9-TEST",
        TARGET.adapter_path,
        TARGET.adapter_address,
    )
    bus.data[TARGET.device_path][p.DEVICE_IF]["Name"] = target.name
    report = {}
    await p.probe(t.QueryBluez(bus, target, QUERY), target, report, True, query=QUERY)
    assert report["error_code"] == "unsupported_model"
    assert "Connect" not in members(bus)


async def test_stop_notify_timeout_still_disconnects(monkeypatch):
    bus = Bus()
    bus.stall = "StopNotify"
    monkeypatch.setattr(t, "NOTIFY_CLEANUP_SECONDS", 0.01)
    report = await run(bus)
    assert report["status"] == "cleanup_requires_review"
    assert report["notification_cleanup"] == "stop_unconfirmed"
    assert report["cleanup"] == "disconnected_confirmed"


async def test_query_permits_cannot_change_payload_or_replay():
    bus = Bus()
    api = t.QueryBluez(bus, TARGET, QUERY)
    api._query_path = CHAR
    expected = [b"reviewed", {"type": Variant("s", "request")}]
    api._write_plan = [expected]
    with pytest.raises(p.ProbeError):
        await api._exact(
            CHAR,
            "WriteValue",
            "aya{sv}",
            [b"FastTask", {"type": Variant("s", "request")}],
        )
    assert not bus.calls
    await api._exact(CHAR, "WriteValue", "aya{sv}", expected)
    with pytest.raises(p.ProbeError):
        await api._exact(CHAR, "WriteValue", "aya{sv}", expected)
    assert len(bus.calls) == 1
