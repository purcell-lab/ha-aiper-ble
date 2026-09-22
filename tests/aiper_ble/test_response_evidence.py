"""Rejected INFO diagnostics remain bounded, CRC-gated and private."""

import json

import pytest

from custom_components.aiper_ble.protocol import response_evidence

from .test_isolated_services import call_query
from .test_local_transport import local_radio as local_radio
from .test_polling import INFO, S1, response, setup
from .test_protocol import frame


def test_crc_verified_extra_fields_are_evidence_not_telemetry():
    result = response_evidence(
        response("INFO", {"ack": "+INFO:2,1,73,4\r\n", "sn": "PRIVATE_SERIAL"}),
        INFO,
    )
    assert result == {
        "query": "INFO",
        "source": "ack",
        "crc_and_result_valid": True,
        "length": 16,
        "terminator": "CRLF",
        "numeric_fields": ["2", "1", "73", "4"],
    }
    assert "PRIVATE_SERIAL" not in json.dumps(result)


@pytest.mark.parametrize("field", ["chksum", "res"])
def test_invalid_crc_or_result_never_exposes_fields(field):
    reply = response("INFO")
    reply[field] = -1
    result = response_evidence(reply, INFO)
    assert result["crc_and_result_valid"] is False
    assert "numeric_fields" not in result


@pytest.mark.parametrize(
    "payload",
    ["PRIVATE_SERIAL", "1," * 30, "1" * 200, "2,1,secret", "2,1,\u202e73"],
)
def test_non_numeric_or_oversized_payload_redacted(payload):
    result = response_evidence(response("INFO", {"ack": f"+INFO:{payload}\r\n"}), INFO)
    assert result["numeric_fields_redacted"] is True
    assert payload not in json.dumps(result)


def test_other_queries_never_captured():
    assert response_evidence(response("INFO"), S1) == {}
    assert response_evidence(response("OpInfo"), INFO) == {}


async def test_local_rejected_info_preserves_only_safe_evidence(hass, local_radio):
    reply = response("INFO", {"ack": "+INFO:2,1,73,4\r\n", "sn": "PRIVATE_SERIAL"})
    local_radio[1].append(lambda bus: setattr(bus, "response", frame(reply)))
    entry = await setup(hass, {"use_local_adapter": True})
    result = await call_query(hass, entry)
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_info_response"
    assert result["response_evidence"]["numeric_fields"] == ["2", "1", "73", "4"]
    assert result["values"] == {}
    assert result["cleanup"] == "disconnected_confirmed"
    assert "PRIVATE_SERIAL" not in json.dumps(result)
