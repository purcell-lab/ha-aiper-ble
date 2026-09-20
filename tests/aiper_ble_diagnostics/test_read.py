"""One-shot read boundary, payload limits and failure cleanup without hardware."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_fast import MessageType

from custom_components.aiper_ble_diagnostics import probe as p

from .helpers import TARGET, Fake, objects

SERVICE = TARGET.device_path + "/service001"
CHAR = SERVICE + "/char002"


def connected_objects():
    data = objects()
    data[TARGET.device_path][p.DEVICE_IF].update(Connected=True, ServicesResolved=True)
    return data


async def test_exact_dbus_read_and_single_use():
    bus = SimpleNamespace(
        call=AsyncMock(
            return_value=SimpleNamespace(
                message_type=MessageType.METHOD_RETURN, body=[bytes([1, 2, 3])]
            )
        )
    )
    api = p.Bluez(bus, TARGET, allow_read=True)
    api.objects = AsyncMock(return_value=connected_objects())
    assert await api.read_sample() == bytes([1, 2, 3])
    message = bus.call.call_args.args[0]
    assert (message.destination, message.path, message.interface, message.member) == (
        "org.bluez",
        CHAR,
        p.CHAR_IF,
        "ReadValue",
    )
    assert message.signature == "a{sv}"
    assert message.body == [{}]
    with pytest.raises(p.ProbeError):
        await api.read_sample()
    with pytest.raises(p.ProbeError):
        await api.call(CHAR, p.CHAR_IF, "ReadValue", "a{sv}", [{}])
    assert bus.call.await_count == 1


async def test_discovery_transport_cannot_read():
    api = p.Bluez(None, TARGET)
    with pytest.raises(p.ProbeError):
        await api.read_sample()
    with pytest.raises(p.ProbeError):
        await api.call(CHAR, p.CHAR_IF, "ReadValue", "a{sv}", [{}])


@pytest.mark.parametrize(
    "flag", ["encrypt-read", "encrypt-authenticated-read", "secure-read", "authorize"]
)
async def test_security_gated_characteristic_refused(flag):
    data = connected_objects()
    data[CHAR][p.CHAR_IF]["Flags"].append(flag)
    api = p.Bluez(None, TARGET, allow_read=True)
    api.objects = AsyncMock(return_value=data)
    with pytest.raises(p.ProbeError):
        await api.read_sample()


@pytest.mark.parametrize(
    "path,signature,body",
    [
        ("/other", "a{sv}", [{}]),
        (CHAR, "", []),
        (CHAR, "a{sv}", [{"offset": 1}]),
    ],
)
async def test_read_permit_rejects_wrong_path_or_options(path, signature, body):
    api = p.Bluez(None, TARGET, allow_read=True)
    api._read_path = CHAR
    with pytest.raises(p.ProbeError):
        await api.call(path, p.CHAR_IF, "ReadValue", signature, body)


async def test_cancelled_transport_read_revokes_permit():
    api = p.Bluez(
        SimpleNamespace(call=AsyncMock(side_effect=asyncio.CancelledError)),
        TARGET,
        allow_read=True,
    )
    api.objects = AsyncMock(return_value=connected_objects())
    with pytest.raises(asyncio.CancelledError):
        await api.read_sample()
    assert api._read_path is None
    with pytest.raises(p.ProbeError):
        await api.read_sample()


@pytest.mark.parametrize(
    "member", ["WriteValue", "StartNotify", "StopNotify", "Pair", "Set"]
)
async def test_read_enabled_transport_still_rejects_other_operations(member):
    api = p.Bluez(None, TARGET, allow_read=True)
    with pytest.raises(p.ProbeError):
        await api.call(CHAR, p.CHAR_IF, member)


@pytest.mark.parametrize(
    "change",
    [
        "missing_service",
        "duplicate_service",
        "missing_char",
        "duplicate_char",
        "wrong_device",
        "wrong_parent",
        "no_read",
        "notifying",
        "wrong_path",
        "disconnected",
        "paired",
        "unresolved",
        "adapter_changed",
        "name_changed",
    ],
)
async def test_unsafe_endpoint_never_sends_read(change):
    data = connected_objects()
    if change == "missing_service":
        del data[SERVICE]
    elif change == "duplicate_service":
        data[TARGET.device_path + "/service999"] = copy.deepcopy(data[SERVICE])
    elif change == "missing_char":
        del data[CHAR]
    elif change == "duplicate_char":
        data[SERVICE + "/char999"] = copy.deepcopy(data[CHAR])
    elif change == "wrong_device":
        data[SERVICE][p.SERVICE_IF]["Device"] = "/other"
    elif change == "wrong_parent":
        data[CHAR][p.CHAR_IF]["Service"] = "/other"
    elif change == "no_read":
        data[CHAR][p.CHAR_IF]["Flags"] = ["write", "notify"]
    elif change == "notifying":
        data[CHAR][p.CHAR_IF]["Notifying"] = True
    elif change == "wrong_path":
        data["/other/char"] = data.pop(CHAR)
    elif change == "disconnected":
        data[TARGET.device_path][p.DEVICE_IF]["Connected"] = False
    elif change == "paired":
        data[TARGET.device_path][p.DEVICE_IF]["Paired"] = True
    elif change == "unresolved":
        data[TARGET.device_path][p.DEVICE_IF]["ServicesResolved"] = False
    elif change == "adapter_changed":
        data[TARGET.adapter_path]["org.bluez.Adapter1"]["Address"] = "wrong"
    elif change == "name_changed":
        data[TARGET.device_path][p.DEVICE_IF]["Name"] = "wrong"
    bus = SimpleNamespace(call=AsyncMock())
    api = p.Bluez(bus, TARGET, allow_read=True)
    api.objects = AsyncMock(return_value=data)
    with pytest.raises(p.ProbeError):
        await api.read_sample()
    bus.call.assert_not_awaited()


@pytest.mark.parametrize(
    "error", ["NotAuthorized", "NotPermitted", "InProgress", "NotSupported"]
)
async def test_read_error_consumes_attempt_without_retry(error):
    bus = SimpleNamespace(
        call=AsyncMock(
            return_value=SimpleNamespace(
                message_type=MessageType.ERROR, error_name="org.bluez.Error." + error
            )
        )
    )
    api = p.Bluez(bus, TARGET, allow_read=True)
    api.objects = AsyncMock(return_value=connected_objects())
    with pytest.raises(p.BluezError):
        await api.read_sample()
    with pytest.raises(p.ProbeError):
        await api.read_sample()
    assert bus.call.await_count == 1


@pytest.mark.parametrize("value", [b"", b"\x01\xff", [0, 255], bytes(512)])
async def test_read_sample_unparsed_and_disconnects(value):
    api, report = Fake(), {}
    api.read_sample = AsyncMock(return_value=value)
    await p.probe(api, TARGET, report, True, read=True)
    assert report["status"] == "read_complete"
    assert report["sample_hex"] == bytes(value).hex()
    assert report["sample_bytes"] == len(value)
    assert report["sample_interpretation"] == ("unparsed" if value else "empty")
    assert report["cleanup"] == "disconnected_confirmed"
    api.read_sample.assert_awaited_once()
    assert api.calls.count("connect") == api.calls.count("disconnect") == 1


@pytest.mark.parametrize("value", [bytes(513), [256], [-1], [True], "abc", None])
async def test_invalid_sample_not_cached_and_disconnects(value):
    api, report = Fake(), {}
    api.read_sample = AsyncMock(return_value=value)
    await p.probe(api, TARGET, report, True, read=True)
    assert report["status"] == "failed"
    assert "sample_hex" not in report
    assert report["cleanup"] == "disconnected_confirmed"


async def test_read_timeout_disconnects_without_retry(monkeypatch):
    api, report = Fake(), {}

    async def stalled():
        await asyncio.Event().wait()

    api.read_sample = AsyncMock(side_effect=stalled)
    monkeypatch.setattr(p, "READ_SECONDS", 0.001)
    await p.probe(api, TARGET, report, True, read=True)
    assert report["status"] == "failed"
    assert report["cleanup"] == "disconnected_confirmed"
    assert "sample_hex" not in report
    api.read_sample.assert_awaited_once()


async def test_read_authorization_failure_disconnects():
    api, report = Fake(), {}
    api.read_sample = AsyncMock(
        side_effect=p.BluezError("org.bluez.Error.NotAuthorized")
    )
    await p.probe(api, TARGET, report, True, read=True)
    assert report["status"] == "failed"
    assert report["cleanup"] == "disconnected_confirmed"
    api.read_sample.assert_awaited_once()


async def test_read_cannot_implicitly_connect():
    api, report = Fake(), {}
    await p.probe(api, TARGET, report, read=True)
    assert report["status"] == "failed"
    assert api.calls == []
