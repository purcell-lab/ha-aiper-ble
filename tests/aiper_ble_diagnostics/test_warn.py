"""Synthetic APK-derived WARN fixtures, not a live firmware capture."""

from unittest.mock import patch

import pytest

from custom_components.aiper_ble_diagnostics.coordinator import verified_values
from custom_components.aiper_ble_diagnostics.protocol import (
    ProtocolError,
    Query,
    preview,
    query_frame,
)

from .test_polling import INFO, OP, S1, WARN, response, setup
from .test_polling import transport as transport
from .test_protocol import frame
from .test_query import members


@pytest.mark.parametrize("mode", ["omit_empty_crc", "include_empty_crc"])
def test_warn_exact_frame_and_independent_crc(mode):
    query = Query(mode, "request", query_type="WARN")
    # Independent table-driven calculation, not the production CRC helper.
    assert preview(query)["request_json"] == {
        "type": "Machine",
        "data": {"cmd": "AT+WARN?"},
        "chksum": 10501,
    }
    assert query_frame(query) == (
        b"aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTRXUENi0WK1Qw"
        b"Vz4TYUE7WigFZk0iBSs=\n"
    )


@pytest.mark.parametrize("field", ["ack", "report"])
@pytest.mark.parametrize("code", [0, 1, 256, -1, -(2**63), 2**63 - 1])
def test_warn_signed_long_no_fault_label_inference(field, code):
    assert verified_values(response("WARN", {field: f"+WARN:{code}\r\n"}), WARN) == {
        "warning_code_raw": code
    }


@pytest.mark.parametrize(
    "text",
    [
        "+WARN:0",
        "+WARN:0\n",
        "+WARN:0\r\nextra",
        "+WARN:\r\n",
        "+WARN:1,2\r\n",
        "+WARN:0x10\r\n",
        "+WARN:true\r\n",
        "+WARN:1.0\r\n",
        "+WARN:１\r\n",
        "+WARN: 1\r\n",
        "+WARN:9223372036854775808\r\n",
        "+WARN:-9223372036854775809\r\n",
    ],
)
def test_warn_malformed_fails_closed(text):
    with pytest.raises(ProtocolError, match="invalid_warn_response"):
        verified_values(response("WARN", {"ack": text}), WARN)


@pytest.mark.parametrize("other", [S1, OP, INFO])
def test_warn_query_isolation(other):
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(response("WARN"), other)
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(response(other.query_type), WARN)


def test_warn_report_precedence_crc_and_result_guards():
    with pytest.raises(ProtocolError, match="response_query_mismatch"):
        verified_values(
            response("WARN", {"report": "+OTHER:0\r\n", "ack": "+WARN:0\r\n"}),
            WARN,
        )
    payload = response("WARN")
    payload["data"]["ack"] = "+WARN:1\r\n"
    with pytest.raises(ProtocolError, match="response_checksum_mismatch"):
        verified_values(payload, WARN)
    payload = response("WARN")
    payload["res"] = 1
    with pytest.raises(ProtocolError, match="response_not_successful"):
        verified_values(payload, WARN)


@pytest.mark.parametrize(
    "command",
    ["WARN=0", "AT+WARN?", "warn", "RECORD", "ULTRAS", "DevInfo", "POWER_SAVE", "AUTO"],
)
def test_other_apk_queries_not_implicitly_allowed(command):
    with pytest.raises(ProtocolError, match="invalid_query_type"):
        Query("omit_empty_crc", "request", query_type=command)


@pytest.mark.parametrize("failure", ["crc", "cleanup", "shape"])
async def test_fourth_query_failure_no_partial_update(hass, transport, failure):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    previous = coordinator.data
    state = hass.states.get("sensor.aiper_ble_warning_code_raw")
    assert state.state == "0"
    assert "state_class" not in state.attributes
    assert "unit_of_measurement" not in state.attributes
    assert bytes(transport[0][3].written) == query_frame(WARN)

    def fail(bus):
        if failure == "cleanup":
            bus.failure = "StopNotify"
        else:
            value = response("WARN")
            if failure == "crc":
                value["chksum"] = 0
            else:
                value = response("WARN", {"ack": "+WARN:0,1\r\n"})
            bus.response = frame(value)

    transport[1].extend([lambda bus: None] * 3 + [fail])
    coordinator.next_attempt = 0
    await coordinator.async_refresh()
    assert coordinator.data == previous
    assert not coordinator.last_update_success
    assert coordinator.last_poll_details["query_type"] == "WARN"
    for entity_id in ("warning_code_raw", "battery", "temperature"):
        assert hass.states.get(f"sensor.aiper_ble_{entity_id}").state == "unavailable"
    assert len(transport[0]) == 8
    assert coordinator.suspended is (failure == "cleanup")


async def test_fourth_query_shares_cycle_deadline_and_cleans_up(hass, transport):
    transport[1].extend(
        [lambda bus: None] * 3 + [lambda bus: setattr(bus, "stall", "WriteValue")]
    )
    with patch("custom_components.aiper_ble_diagnostics.coordinator.POLL_SECONDS", 0.5):
        entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.error_code == "timeout"
    assert not coordinator.last_update_success
    assert coordinator.last_poll_details["query_type"] == "WARN"
    assert len(transport[0]) == 4
    assert members(transport[0][-1]).count("StopNotify") == 1
    assert members(transport[0][-1]).count("Disconnect") == 1
    assert hass.states.get("sensor.aiper_ble_battery").state == "unavailable"
