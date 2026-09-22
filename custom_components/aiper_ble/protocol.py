"""Experimental standard/S1 codec with fixed queries and opt-in S1 controls.

Evidence: Aiper 3.6.1 build 82 CmdFactory (legacy serializer only).
See docs/aiper_ble_apk_361_evidence.md. Static evidence is not a wire capture.
"""

import base64
import binascii
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .datapoints import MACHINE_FIELDS

MAX_FRAME_BYTES = 4096
MAX_TOTAL_BYTES = 8192
MAX_NOTIFICATIONS = 64
XOR_KEY = bytes((0x12, 0x34, 0x56, 0x78))
APK_SHA256 = "b2e81eb9db09873dee7f0f3887008f810383d66b4df354894e78da565021d2a1"


class ProtocolError(ValueError):
    """Use fixed codes, never data-dependent exception text in diagnostics."""


@dataclass(frozen=True)
class Listen:
    """Fixed 60-second notification observation, with no application command."""

    allow_missing_advertisement: bool = False

    def __post_init__(self) -> None:
        if type(self.allow_missing_advertisement) is not bool:
            raise ProtocolError("invalid_legacy_probe_confirmation")


@dataclass(frozen=True)
class Query:
    """One of four fixed status requests; no caller-supplied command or data."""

    checksum_mode: str
    write_mode: str
    allow_missing_advertisement: bool = False
    query_type: str = "OpInfo"

    def __post_init__(self) -> None:
        if self.checksum_mode not in {"include_empty_crc", "omit_empty_crc"}:
            raise ProtocolError("invalid_checksum_mode")
        if self.write_mode not in {"command", "request"}:
            raise ProtocolError("invalid_write_mode")
        if type(self.allow_missing_advertisement) is not bool:
            raise ProtocolError("invalid_legacy_probe_confirmation")
        if self.query_type not in {"OpInfo", "S1_INFO", "INFO", "WARN"}:
            raise ProtocolError("invalid_query_type")


@dataclass(frozen=True)
class Control:
    """Only explicit S1 start/standby setters, never a toggle or arbitrary AT."""

    action: str
    allow_missing_advertisement: bool = False
    checksum_mode: str = "nonempty_crc"
    write_mode: str = "request"
    query_type: str = "MODE"

    def __post_init__(self) -> None:
        if self.action not in {"start_cleaning", "stop_cleaning"}:
            raise ProtocolError("invalid_control")
        if type(self.allow_missing_advertisement) is not bool:
            raise ProtocolError("invalid_legacy_probe_confirmation")
        if (
            self.checksum_mode != "nonempty_crc"
            or self.write_mode != "request"
            or self.query_type != "MODE"
        ):
            raise ProtocolError("invalid_control")


def crc16(data: Iterable[int]) -> int:
    """Legacy CmdFactory Modbus CRC, NOT the newer SDK's 0x1021 algorithm."""
    crc = 0x9966
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def xor(data: bytes | bytearray) -> bytes:
    return bytes(byte ^ XOR_KEY[index % 4] for index, byte in enumerate(data))


def query_frame(query: Query | Control) -> bytes:
    """Build one fixed query or explicit control, never a generic command."""
    if not isinstance(query, (Query, Control)):
        raise ProtocolError("not_a_query")
    command: dict[str, Any]
    if isinstance(query, Control):
        command = {
            "type": "Machine",
            "data": {
                "cmd": "AT+MODE=1" if query.action == "start_cleaning" else "AT+MODE=0"
            },
        }
    else:
        command = (
            {"type": "Machine", "data": {"cmd": f"AT+{query.query_type}?"}}
            if query.query_type in {"S1_INFO", "INFO", "WARN"}
            else {"type": "OpInfo", "data": {}}
        )
    if query.query_type != "OpInfo" or query.checksum_mode == "include_empty_crc":
        command["chksum"] = crc16(
            json.dumps(command["data"], separators=(",", ":")).encode()
        )
    encoded = json.dumps(command, separators=(",", ":")).encode()
    return base64.b64encode(xor(encoded)) + b"\n"


def preview(query: Query | Control) -> dict[str, Any]:
    """Offline request inspection. No bus, account or hardware access."""
    frame = query_frame(query)
    return {
        "status": "protocol_preview",
        "command": query.query_type,
        "protocol": "legacy_xor",
        "checksum_mode": query.checksum_mode,
        "write_mode": query.write_mode,
        "request_json": json.loads(xor(base64.b64decode(frame))),
        "request_hex": frame.hex(),
        "request_bytes": len(frame),
        "evidence": "app_3_6_1_82_legacy_cmdfactory_static_not_wire_verified",
        "apk_sha256": APK_SHA256,
        "recommended_checksum_mode": (
            "not_applicable_nonempty_data"
            if query.query_type != "OpInfo"
            else "omit_empty_crc"
        ),
        "checksum_semantics": (
            "nonempty_data_crc_required"
            if query.query_type != "OpInfo"
            else "explicit_empty_object"
            if query.checksum_mode == "include_empty_crc"
            else "null_data_legacy_serializer"
        ),
    }


def protocol_hint(device: Mapping[str, Any]) -> str:
    """Distinguish absent evidence from malformed evidence; neither is legacy."""
    manufacturer = device.get("ManufacturerData", {})
    if not isinstance(manufacturer, dict) or any(
        type(key) is not int or not 0 <= key <= 65535 for key in manufacturer
    ):
        return "malformed"
    value = manufacturer.get(0)
    if value is None:
        return "unknown"
    if not isinstance(value, (bytes, bytearray, list)) or any(
        type(item) is not int or not 0 <= item <= 255 for item in value
    ):
        return "malformed"
    if not value:
        return "unknown"
    return "ecdh" if value[0] == 1 else "legacy_xor"


def chunks(frame: bytes, mtu: object = None) -> list[bytes]:
    """BlueZ negotiates MTU; absent metadata uses the ATT minimum payload."""
    size = min(200, mtu - 3) if isinstance(mtu, int) and mtu >= 23 else 20
    return [frame[index : index + size] for index in range(0, len(frame), size)]


def _reject_constant(_value: str) -> Any:
    raise ProtocolError("nonfinite_json")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_json_key")
        result[key] = value
    return result


class Decoder:
    """Bounded incremental newline framing, including split/coalesced frames."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.total_bytes = 0
        self.notifications = 0

    def feed(self, value: object) -> list[dict[str, Any]]:
        if not isinstance(value, (bytes, bytearray, list)):
            raise ProtocolError("invalid_notification")
        self.notifications += 1
        self.total_bytes += len(value)
        if self.notifications > MAX_NOTIFICATIONS or self.total_bytes > MAX_TOTAL_BYTES:
            raise ProtocolError("capture_limit")
        if any(type(item) is not int or not 0 <= item <= 255 for item in value):
            raise ProtocolError("invalid_notification")
        self.buffer.extend(value)
        result: list[dict[str, Any]] = []
        while b"\n" in self.buffer:
            frame, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            if not frame or len(frame) > MAX_FRAME_BYTES:
                raise ProtocolError("frame_limit")
            try:
                raw = xor(base64.b64decode(frame, validate=True))
                obj = json.loads(
                    raw,
                    parse_constant=_reject_constant,
                    object_pairs_hook=_unique_keys,
                )
            except (ValueError, UnicodeError, binascii.Error, RecursionError) as exc:
                raise ProtocolError("invalid_frame") from exc
            if (
                not isinstance(obj, dict)
                or not isinstance(obj.get("type"), str)
                or not isinstance(obj.get("data"), dict)
            ):
                raise ProtocolError("unsupported_response_envelope")
            result.append(obj)
        if len(self.buffer) > MAX_FRAME_BYTES:
            raise ProtocolError("frame_limit")
        return result


def telemetry(response: Mapping[str, Any]) -> dict[str, int | float | bool]:
    """Allowlist scalar evidence only. No units, entity mappings or guessing."""
    machine = response.get("data", {}).get("Machine", {})
    if not isinstance(machine, dict):
        return {}
    result: dict[str, int | float | bool] = {}
    for key in MACHINE_FIELDS:
        value = machine.get(key)
        if (
            isinstance(value, (int, float))
            and type(value) in (int, float, bool)
            and (type(value) is not float or math.isfinite(value))
        ):
            result[key] = value
    return result


def response_evidence(
    response: Mapping[str, Any], query: Query | Control
) -> dict[str, Any]:
    """Bounded numeric INFO evidence only, never the full device response."""
    if query.query_type != "INFO" or response.get("type") != "Machine":
        return {}
    data = response.get("data")
    if not isinstance(data, dict):
        return {}
    report = data.get("report")
    source = "report" if isinstance(report, str) else "ack"
    text = data.get(source)
    if not isinstance(text, str) or not text.startswith("+INFO:"):
        return {}
    checksum = response.get("chksum")
    encoded = json.dumps(
        data, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    valid = (
        type(checksum) is int
        and 0 <= checksum <= 65535
        and checksum == crc16(encoded)
        and type(response.get("res")) is int
        and response["res"] == 0
    )
    evidence: dict[str, Any] = {
        "query": "INFO",
        "source": source,
        "crc_and_result_valid": valid,
    }
    if not valid:
        return evidence
    evidence["length"] = min(len(text), MAX_FRAME_BYTES)
    evidence["terminator"] = (
        "CRLF" if text.endswith("\r\n") else "LF" if text.endswith("\n") else "none"
    )
    # Numeric-only fixed-prefix evidence is not interpreted as telemetry.
    # Never include arbitrary strings, keys, serials, raw frames or error text.
    payload = text[6:].removesuffix("\r\n").removesuffix("\n")
    fields = payload.split(",")
    if (
        len(text) <= 160
        and 1 <= len(fields) <= 12
        and all(re.fullmatch(r"[ +\-0-9.]{1,12}", field) for field in fields)
    ):
        evidence["numeric_fields"] = fields
    else:
        evidence["numeric_fields_redacted"] = True
    return evidence


def query_telemetry(
    response: Mapping[str, Any], query: Query | Control
) -> dict[str, Any] | None:
    """Return numeric candidates for the selected query, or None if unrelated.

    S1_INFO is deliberately stricter than the app: exactly two numeric fields,
    CRLF termination, bounded decimal notation and an int32 solar state.
    The app strips two trailing characters; CRLF was subsequently observed on S1.
    """
    if query.query_type == "OpInfo":
        return telemetry(response) if response["type"].casefold() == "opinfo" else None
    if response["type"] != "Machine":
        return None
    data = response["data"]
    # Mirror the app's report-before-ack selection without interpreting other ATs.
    report = data.get("report")
    value = report if isinstance(report, str) else data.get("ack")
    if isinstance(query, Control):
        # APK receiveResponse accepts a +ok prefix for setters. Deliberately
        # accept only the bounded bare acknowledgement until wire validation.
        # A generic +OK carries no command ID and cannot prove physical motion.
        if not isinstance(value, str):
            return None
        if value.lower().startswith("+error"):
            raise ProtocolError("control_device_error")
        if value.lower() == "+ok\r\n":
            return {"acknowledged": True}
        if value.lower().startswith("+ok"):
            raise ProtocolError("unsupported_control_ack")
        return None
    if query.query_type == "WARN":
        if not isinstance(value, str) or not value.startswith("+WARN:"):
            return None
        # S1 app reads the first decimal field with Java Long.parseLong.
        # Require exactly one signed int64 field; do not guess fault-bit labels.
        match = re.fullmatch(r"\+WARN:([+-]?[0-9]{1,19})\r\n", value)
        if match is None or not -(2**63) <= int(match[1]) < 2**63:
            raise ProtocolError("invalid_warn_response")
        return {"warning_code_raw": int(match[1])}
    if query.query_type == "INFO":
        if not isinstance(value, str) or not value.startswith("+INFO:"):
            return None
        # APK 3.6.1 reads status, mode, battLevel from the first three fields.
        # This S1 also returned five fields in a CRC-verified live response.
        # Accept only these two bounded shapes; never interpret the extra pair.
        match = re.fullmatch(
            r"\+INFO:([+-]?[0-9]{1,10}(?:,[+-]?[0-9]{1,10}){2}"
            r"(?:(?:,[+-]?[0-9]{1,10}){2})?)\r\n",
            value,
        )
        if match is None:
            raise ProtocolError("invalid_info_response")
        fields = [int(part) for part in match[1].split(",")]
        if not all(-(2**31) <= item < 2**31 for item in fields):
            raise ProtocolError("invalid_info_response")
        status, mode, battery = fields[:3]
        values: dict[str, Any] = {
            "info_status_raw": status,
            "info_mode_raw": mode,
            # Preserve other valid fields, but never clamp a sentinel to 0/100.
            "battery": battery if 0 <= battery <= 100 else None,
        }
        if len(fields) == 5:
            # Retained raw, not interpreted: field 4 has only ever read 0 and
            # field 5 behaved as a minute counter in live polling (observed).
            values["info_field_4_raw"] = fields[3]
            values["info_field_5_raw"] = fields[4]
        return values
    if not isinstance(value, str) or not value.startswith("+S1_INFO:"):
        return None
    match = re.fullmatch(
        r"\+S1_INFO:([+-]?[0-9]{1,9}(?:\.[0-9]{1,6})?),([+-]?[0-9]{1,10})\r\n",
        value,
    )
    if match is None:
        raise ProtocolError("invalid_s1_info_response")
    temperature = float(match[1])
    solar = int(match[2])
    if not -(2**31) <= solar < 2**31:
        raise ProtocolError("invalid_s1_info_response")
    return {
        "temperature_raw": temperature,
        "temperature_celsius": temperature / 10,
        "solar_status": solar,
    }
