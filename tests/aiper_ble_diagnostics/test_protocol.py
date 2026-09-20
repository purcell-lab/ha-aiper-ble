"""Offline codec fixtures are synthetic, not evidence of firmware compatibility."""

import base64
import json

import pytest

from custom_components.aiper_ble_diagnostics import protocol as p


def frame(obj):
    return (
        base64.b64encode(p.xor(json.dumps(obj, separators=(",", ":")).encode())) + b"\n"
    )


def test_crc_reference_and_xor():
    # Independently checked bitwise polynomial reference values.
    assert p.crc16(b"") == 0x9966
    assert p.crc16(b"{}") == 6921
    assert p.crc16(b"123456789") == 8055
    assert p.crc16(b'{"key":"value"}') == 42401
    # The separate SDK's non-reflected 0x1021 algorithm gives 54666 for {}.
    assert p.crc16(b"{}") not in {6989, 54666}
    assert p.xor(bytes.fromhex("0000000000")) == bytes.fromhex("1234567812")
    assert p.xor(p.xor(b"abc")) == b"abc"


@pytest.mark.parametrize("mode", ["include_empty_crc", "omit_empty_crc"])
def test_exact_opinfo_preview(mode):
    q = p.Query(mode, "command")
    result = p.preview(q)
    obj = result["request_json"]
    assert obj["type"] == "OpInfo"
    assert obj["data"] == {}
    assert set(obj) == (
        {"type", "data", "chksum"} if mode == "include_empty_crc" else {"type", "data"}
    )
    if mode == "include_empty_crc":
        assert obj["chksum"] == p.crc16(b"{}")
    assert p.query_frame(q) == bytes.fromhex(result["request_hex"])
    assert p.Decoder().feed(p.query_frame(q)) == [obj]
    assert result["apk_sha256"] == p.APK_SHA256
    assert result["recommended_checksum_mode"] == "omit_empty_crc"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("omit_empty_crc", b"aRYiAWJRdEIweyYxfFI5Wj4WMhlmVXRCaUkr\n"),
        (
            "include_empty_crc",
            b"aRYiAWJRdEIweyYxfFI5Wj4WMhlmVXRCaUl6WnFcPQtnWXRCJA1kSW8=\n",
        ),
    ],
)
def test_fixed_legacy_frames(mode, expected):
    assert p.query_frame(p.Query(mode, "request")) == expected


@pytest.mark.parametrize("mode", ["omit_empty_crc", "include_empty_crc"])
def test_s1_info_exact_fixed_request_mandatory_crc(mode):
    query = p.Query(mode, "request", query_type="S1_INFO")
    result = p.preview(query)
    # Independent table-based CRC reference over {"cmd":"AT+S1_INFO?"}.
    assert p.crc16(b'{"cmd":"AT+S1_INFO?"}') == 49921
    assert result["request_json"] == {
        "type": "Machine",
        "data": {"cmd": "AT+S1_INFO?"},
        "chksum": 49921,
    }
    assert p.query_frame(query) == (
        b"aRYiAWJRdEIweTcbel04HTAYdBxzQDdaKE90G39QdEIwdQJTQQUJMVxyGUcwSXpa"
        b"cVw9C2dZdEImDW9KI0k=\n"
    )
    assert result["command"] == "S1_INFO"
    assert result["checksum_semantics"] == "nonempty_data_crc_required"
    assert result["recommended_checksum_mode"] == "not_applicable_nonempty_data"


@pytest.mark.parametrize(
    "value", ["Machine", "AT+S1_INFO?", "S1_INFO=1", "FastTask", "s1_info", "", None]
)
def test_no_arbitrary_query_type(value):
    with pytest.raises(p.ProtocolError, match="invalid_query_type"):
        p.Query("omit_empty_crc", "request", query_type=value)


@pytest.mark.parametrize("field", ["report", "ack"])
@pytest.mark.parametrize(
    "raw,solar,celsius",
    [("265", "1", 26.5), ("-55", "0", -5.5), ("+12.5", "2", 1.25)],
)
def test_s1_info_numeric_candidates_only(field, raw, solar, celsius):
    response = {
        "type": "Machine",
        "data": {field: f"+S1_INFO:{raw},{solar}\r\n", "serial": "PRIVATE"},
    }
    assert p.query_telemetry(
        response, p.Query("omit_empty_crc", "request", query_type="S1_INFO")
    ) == {
        "temperature_raw": float(raw),
        "temperature_celsius": celsius,
        "solar_status": int(solar),
    }


@pytest.mark.parametrize(
    "value",
    [
        "+S1_INFO:NaN,1\r\n",
        "+S1_INFO:Infinity,1\r\n",
        "+S1_INFO:1e999,1\r\n",
        "+S1_INFO:265,true\r\n",
        "+S1_INFO:265,2147483648\r\n",
        "+S1_INFO:265,1",
        "+S1_INFO:265,1\n",
        "+S1_INFO:265,1xx",
        "+S1_INFO:265,1,extra\r\n",
        "+S1_INFO:265,1\r\nAT+S1_INFO=1",
        "+S1_INFO:265\r\n",
        "+S1_INFO:１２３,1\r\n",
    ],
)
def test_s1_info_malformed_matching_response_fails_closed(value):
    with pytest.raises(p.ProtocolError, match="invalid_s1_info_response"):
        p.query_telemetry(
            {"type": "Machine", "data": {"report": value}},
            p.Query("omit_empty_crc", "request", query_type="S1_INFO"),
        )


@pytest.mark.parametrize(
    "response",
    [
        {"type": "OpInfo", "data": {"report": "+S1_INFO:265,1\r\n"}},
        {"type": "Machine", "data": {"report": "+OTHER:265,1\r\n"}},
        {"type": "Machine", "data": {"report": ["+S1_INFO:265,1\r\n"]}},
        {"type": "Machine", "data": {"ack": None}},
        {
            "type": "Machine",
            "data": {"report": "+OTHER:1\r\n", "ack": "+S1_INFO:265,1\r\n"},
        },
    ],
)
def test_s1_info_ignores_unrelated_and_prefers_report(response):
    assert (
        p.query_telemetry(
            response, p.Query("omit_empty_crc", "request", query_type="S1_INFO")
        )
        is None
    )


@pytest.mark.parametrize("value", [None, 0, 1, "true", []])
def test_legacy_probe_requires_actual_boolean(value):
    with pytest.raises(p.ProtocolError, match="invalid_legacy_probe_confirmation"):
        p.Query("omit_empty_crc", "request", value)


@pytest.mark.parametrize(
    "checksum,write",
    [
        ("auto", "command"),
        ("include_empty_crc", "auto"),
        ("FastTask", "request"),
    ],
)
def test_invalid_query_choices(checksum, write):
    with pytest.raises(p.ProtocolError):
        p.Query(checksum, write)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "unknown"),
        ([], "unknown"),
        ([True], "malformed"),
        ([256], "malformed"),
        ([-1], "malformed"),
        ("00", "malformed"),
        ([1], "ecdh"),
        (b"\x00", "legacy_xor"),
        ([2], "legacy_xor"),
    ],
)
def test_manufacturer_hint(value, expected):
    assert p.protocol_hint({"ManufacturerData": {0: value}}) == expected
    assert p.protocol_hint({}) == "unknown"
    assert p.protocol_hint({"ManufacturerData": {42: [0]}}) == "unknown"


@pytest.mark.parametrize("value", [None, [], "bad", {"0": [0]}, {False: [0]}])
def test_malformed_manufacturer_container(value):
    assert p.protocol_hint({"ManufacturerData": value}) == "malformed"


@pytest.mark.parametrize("mtu,max_size", [(None, 20), (23, 20), (50, 47), (512, 200)])
def test_chunks_respect_mtu_and_preserve_frame(mtu, max_size):
    value = bytes(range(250)) * 2
    parts = p.chunks(value, mtu)
    assert max(map(len, parts)) <= max_size
    assert b"".join(parts) == value


def test_fragmented_and_coalesced_frames():
    obj = {"type": "OpInfo", "data": {"Machine": {"temp": 24.5}}}
    raw = frame(obj)
    decoder = p.Decoder()
    assert decoder.feed(raw[:4]) == []
    assert decoder.feed(raw[4:] + raw) == [obj, obj]
    assert decoder.buffer == b""
    assert decoder.total_bytes == 2 * len(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"\n",
        b"%%%\n",
        base64.b64encode(p.xor(b"[]")) + b"\n",
        frame({"type": "OpInfo", "data": []}),
        frame({"OpInfo": {}}),
        base64.b64encode(p.xor(b'{"type":"OpInfo","data":{"a":NaN}}')) + b"\n",
        base64.b64encode(p.xor(b'{"type":"OpInfo","type":"FastTask","data":{}}'))
        + b"\n",
    ],
)
def test_malformed_frames_fail_closed(raw):
    with pytest.raises(p.ProtocolError):
        p.Decoder().feed(raw)


@pytest.mark.parametrize("raw", [[True], [256], "hello", None])
def test_invalid_notification_types(raw):
    with pytest.raises(p.ProtocolError):
        p.Decoder().feed(raw)


def test_bounded_buffer_and_notification_count():
    with pytest.raises(p.ProtocolError, match="frame_limit"):
        p.Decoder().feed(b"a" * 4097)
    decoder = p.Decoder()
    for _ in range(64):
        decoder.feed(b"")
    with pytest.raises(p.ProtocolError, match="capture_limit"):
        decoder.feed(b"")
    with pytest.raises(p.ProtocolError, match="capture_limit"):
        p.Decoder().feed(bytes(8193))


def test_only_scalar_machine_evidence_no_privacy_leaks_or_units():
    assert p.telemetry(
        {
            "data": {
                "Machine": {
                    "temp": 25.5,
                    "cap": 80,
                    "in_water": True,
                    "warn": "secret",
                    "status": {"private": "value"},
                    "serial": "private",
                    "token": "private",
                    "mode": float("inf"),
                }
            }
        }
    ) == {"temp": 25.5, "cap": 80, "in_water": True}
    assert p.telemetry({"data": {"temp": 25.5}}) == {}
