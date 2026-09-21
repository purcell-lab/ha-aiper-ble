"""Offline control evidence, exact wire plans, HA guards and uncertainty."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aiper_ble_diagnostics import bluetooth_transport
from custom_components.aiper_ble_diagnostics.const import DOMAIN
from custom_components.aiper_ble_diagnostics.coordinator import verified_values
from custom_components.aiper_ble_diagnostics.protocol import (
    Control,
    ProtocolError,
    Query,
    query_frame,
    query_telemetry,
)
from custom_components.aiper_ble_diagnostics.s1_states import info_state

from .helpers import TARGET
from .test_bluetooth import radio as radio
from .test_polling import PATH, response, setup
from .test_polling import transport as transport
from .test_protocol import frame
from .test_query import Bus, members, run


@pytest.mark.parametrize(
    "action, expected",
    [
        (
            "start_cleaning",
            b"aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTX3sSPS8FdAU+FjUQeUcjFTAOZ0kiAG8F\n",
        ),
        (
            "stop_cleaning",
            b"aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTX3sSPS8EdAU+FjUQeUcjFTAOYEggDGYF\n",
        ),
    ],
)
def test_fixed_control_golden_frames(action, expected):
    assert query_frame(Control(action)) == expected
    assert verified_values(response("MODE", {"ack": "+OK\r\n"}), Control(action)) == {
        "acknowledged": True
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "toggle"},
        {"action": "AT+MODE=9"},
        {"action": "start_cleaning", "write_mode": "command"},
        {"action": "start_cleaning", "query_type": "INFO"},
        {"action": "stop_cleaning", "allow_missing_advertisement": 1},
    ],
)
def test_arbitrary_commands_and_overrides_rejected(kwargs):
    with pytest.raises(ProtocolError):
        Control(**kwargs)
    with pytest.raises(ProtocolError):
        Query("omit_empty_crc", "request", query_type="MODE")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("+OK\r\n", {"acknowledged": True}),
        ("+ok\r\n", {"acknowledged": True}),
        ("+INFO:0,0,80\r\n", None),
        ("+MODE:1\r\n", None),
    ],
)
def test_setter_response_is_not_telemetry(text, expected):
    assert (
        query_telemetry(response("MODE", {"ack": text}), Control("start_cleaning"))
        == expected
    )


@pytest.mark.parametrize("text", ["+OK", "+OK:1\r\n", "+ERROR:1\r\n"])
def test_unvalidated_ack_shapes_fail_closed(text):
    with pytest.raises(ProtocolError):
        verified_values(response("MODE", {"ack": text}), Control("stop_cleaning"))


def test_control_ack_crc_and_result_required():
    for key, value in (("chksum", 0), ("res", 1), ("res", True)):
        reply = response("MODE", {"ack": "+OK\r\n"})
        reply[key] = value
        with pytest.raises(ProtocolError):
            verified_values(reply, Control("stop_cleaning"))


@pytest.mark.parametrize(
    "status,mode,battery,expected",
    [
        (0, 0, 80, "standby"),
        (1, 1, 80, "working"),
        (1, 8, 80, "standby"),
        (1, 9, 80, "sunward"),
        (0, 9, 80, "sunward"),
        (2, 9, 80, "charging"),
        (2, 0, 100, "fully_charged"),
        (3, 0, 40, "fully_charged"),
        (4, 9, 80, "updating"),
        (2, 0, None, "charging"),
        (99, 9, 80, "unknown_code"),
        (None, 0, 80, "unknown_code"),
    ],
)
def test_app_state_predicate_order_not_enum_ordinals(status, mode, battery, expected):
    assert (
        info_state(
            {"info_status_raw": status, "info_mode_raw": mode, "battery": battery}
        )
        == expected
    )


@pytest.mark.parametrize("action", ["start_cleaning", "stop_cleaning"])
async def test_real_local_transport_control_exactly_once(action):
    bus = Bus()
    bus.response = frame(response("MODE", {"ack": "+OK\r\n"}))
    control = Control(action)
    report = await run(bus, control)
    assert report["status"] == "query_complete"
    assert report["cleanup"] == "disconnected_confirmed"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert bytes(bus.written) == query_frame(control)
    assert members(bus).count("Connect") == members(bus).count("Disconnect") == 1
    assert verified_values(report["protocol_response"], control) == {
        "acknowledged": True
    }


@pytest.mark.parametrize("action", ["start_cleaning", "stop_cleaning"])
async def test_real_ha_transport_control_exactly_once(hass, radio, action):
    radio.mutations.append(
        lambda client: setattr(
            client, "reply", frame(response("MODE", {"ack": "+OK\r\n"}))
        )
    )
    control = Control(action)
    report = {}
    await bluetooth_transport.query_once(hass, TARGET, report, control)
    assert report["status"] == "query_complete"
    assert report["cleanup"] == "disconnected_confirmed"
    assert report["notification_cleanup"] == "stop_confirmed"
    assert len(radio.clients) == 1
    assert bytes(radio.clients[0].written) == query_frame(control)
    assert verified_values(report["protocol_response"], control) == {
        "acknowledged": True
    }


async def call(hass, entry, action="start_cleaning", **overrides):
    return await hass.services.async_call(
        DOMAIN,
        action,
        {
            "entry_id": entry.entry_id,
            "confirm_app_closed": True,
            "confirm_safe_to_move": True,
            **overrides,
        },
        blocking=True,
        return_response=True,
    )


def fake_exchange(calls, *, after="1,1,80", before="0,0,80", warn="0", fail=None):
    async def execute(hass, target, report, request):
        calls.append(request)
        control = isinstance(request, Control)
        payload = (
            "+OK\r\n"
            if control
            else f"+WARN:{warn}\r\n"
            if request.query_type == "WARN"
            else f"+INFO:{after if any(isinstance(x, Control) for x in calls) else before}\r\n"
        )
        report.update(
            status="query_complete",
            cleanup="disconnected_confirmed",
            notification_cleanup="stop_confirmed",
            write_attempts=1,
            protocol_response=response(request.query_type, {"ack": payload}),
        )
        if fail is not None:
            fail(report, request)

    return execute


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize(
    "action,after,expected",
    [
        ("start_cleaning", "1,1,80", ["INFO", "WARN", "MODE", "INFO"]),
        ("stop_cleaning", "0,0,80", ["MODE", "INFO"]),
    ],
)
async def test_control_service_readback_and_transport_selection(
    hass, transport, local, action, after, expected
):
    entry = await setup(hass, {"use_local_adapter": local})
    c = entry.runtime_data.coordinator
    calls = []
    route = "local_query_once" if local else "query_once"
    with patch(f"{PATH}.coordinator.{route}", fake_exchange(calls, after=after)):
        result = await call(hass, entry, action)
    assert [x.query_type for x in calls] == expected
    assert result["status"] == "state_verified"
    assert result["motion_may_have_changed"]
    assert c.last_control_result["state_verified"]
    assert c.runtime.task is None
    assert not c.last_update_success  # Never optimistic sensor state.
    assert c.next_attempt > asyncio.get_running_loop().time()


@pytest.mark.parametrize("field", ["confirm_app_closed", "confirm_safe_to_move"])
async def test_control_requires_fresh_confirmations(hass, transport, field):
    entry = await setup(hass, {})
    with pytest.raises(HomeAssistantError):
        await call(hass, entry, **{field: False})
    assert not transport[0]
    assert entry.runtime_data.coordinator.last_control_result["status"] == "never_run"


@pytest.mark.parametrize(
    "before,warn",
    [
        ("2,0,80", "0"),
        ("3,0,100", "0"),
        ("4,0,80", "0"),
        ("99,0,80", "0"),
        ("1,9,80", "0"),
        ("0,0,80", "1"),
        ("0,0,80", "-1"),
    ],
)
async def test_start_preflight_blocks_without_setter(hass, transport, before, warn):
    entry = await setup(hass, {})
    calls = []
    with patch(
        f"{PATH}.coordinator.query_once", fake_exchange(calls, before=before, warn=warn)
    ):
        with pytest.raises(HomeAssistantError):
            await call(hass, entry)
    assert all(not isinstance(x, Control) for x in calls)
    assert not entry.runtime_data.coordinator.last_control_result[
        "motion_may_have_changed"
    ]


@pytest.mark.parametrize(
    "failure", ["write", "bad_crc", "cleanup", "readback", "mismatch"]
)
async def test_uncertain_control_never_retries_or_claims_success(
    hass, transport, failure
):
    entry = await setup(hass, {})
    calls = []

    def fail(report, request):
        if isinstance(request, Control):
            if failure == "write":
                report["status"] = "failed"
                report.pop("protocol_response")
            if failure == "bad_crc":
                report["protocol_response"]["chksum"] = 0
            if failure == "cleanup":
                report.update(status="cleanup_requires_review", cleanup="failed")
        elif failure == "readback":
            report["status"] = "failed"

    with patch(
        f"{PATH}.coordinator.query_once",
        fake_exchange(
            calls, after="1,1,80" if failure == "mismatch" else "0,0,80", fail=fail
        ),
    ):
        with pytest.raises(HomeAssistantError):
            await call(hass, entry, "stop_cleaning")
    c = entry.runtime_data.coordinator
    assert sum(isinstance(x, Control) for x in calls) == 1
    assert c.last_control_result["motion_may_have_changed"]
    assert not c.last_control_result["state_verified"]
    assert c.suspended == (failure == "cleanup")
    assert c.runtime.task is None


async def test_control_busy_and_cancel_share_poll_mutex(hass, transport):
    entry = await setup(hass, {})
    c = entry.runtime_data.coordinator
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def stall(hass, target, report, request):
        report["write_attempts"] = 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            report.update(
                cleanup="disconnected_confirmed", notification_cleanup="stop_confirmed"
            )
            cleaned.set()

    with patch(f"{PATH}.coordinator.query_once", stall):
        task = asyncio.create_task(call(hass, entry, "stop_cleaning"))
        await entered.wait()
        with pytest.raises(HomeAssistantError, match="already running"):
            await call(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
            await task
    assert cleaned.is_set()
    assert c.last_control_result["status"] == "interrupted"
    assert c.last_control_result["motion_may_have_changed"]
    assert c.runtime.task is None


async def test_enum_sensor_and_suspended_control(hass, transport):
    entry = await setup(hass)
    state = hass.states.get("sensor.aiper_ble_operating_state")
    assert state.state == "charging"
    assert state.attributes["device_class"] == "enum"
    c = entry.runtime_data.coordinator
    c.suspended = True
    with pytest.raises(HomeAssistantError, match="suspended"):
        await call(hass, entry, "stop_cleaning")
    assert len(transport[0]) == 4


async def test_control_timeout_is_bounded_and_does_not_retry(hass, transport):
    entry = await setup(hass, {})
    c = entry.runtime_data.coordinator
    calls = []

    async def stall(hass, target, report, request):
        calls.append(request)
        report["write_attempts"] = 1
        try:
            await asyncio.Event().wait()
        finally:
            report.update(
                cleanup="disconnected_confirmed",
                notification_cleanup="stop_confirmed",
            )

    with (
        patch(f"{PATH}.coordinator.query_once", stall),
        patch(f"{PATH}.controls.CONTROL_SECONDS", 0.01),
    ):
        with pytest.raises(HomeAssistantError):
            await call(hass, entry, "stop_cleaning")
    assert len(calls) == 1
    assert c.runtime.task is None
    assert not c.suspended
    assert c.last_control_result["status"] == "outcome_unknown"
    assert c.status == "awaiting_poll_after_control"


async def test_control_result_is_private_and_cached_diagnostics(hass, transport):
    from custom_components.aiper_ble_diagnostics.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await setup(hass, {})
    calls = []

    def inject_private(report, request):
        report["error"] = "PRIVATE_BACKEND_TEXT"
        report["protocol_response"]["data"]["sn"] = "PRIVATE_SERIAL"
        # Re-sign the full data to model a genuine response with private fields.
        report["protocol_response"] = response(
            request.query_type, report["protocol_response"]["data"]
        )

    with patch(
        f"{PATH}.coordinator.query_once",
        fake_exchange(calls, after="0,0,80", fail=inject_private),
    ):
        await call(hass, entry, "stop_cleaning")
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostic["last_control"]["state_verified"]
    assert len(calls) == 2
    for private in (
        "PRIVATE_SERIAL",
        "PRIVATE_BACKEND_TEXT",
        TARGET.address,
        TARGET.name,
    ):
        assert private not in str(diagnostic)


async def test_stop_is_available_after_uncertain_start_without_retry(hass, transport):
    entry = await setup(hass, {})
    calls = []
    with patch(
        f"{PATH}.coordinator.query_once",
        fake_exchange(calls, after="0,0,80"),
    ):
        with pytest.raises(HomeAssistantError):
            await call(hass, entry, "start_cleaning")
        result = await call(hass, entry, "stop_cleaning")
    assert result["state_verified"]
    assert [x.action for x in calls if isinstance(x, Control)] == [
        "start_cleaning",
        "stop_cleaning",
    ]
