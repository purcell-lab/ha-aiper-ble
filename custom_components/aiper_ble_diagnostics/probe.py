"""Bounded single-target discovery with a separately authorised one-shot read."""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from .protocol import ProtocolError, protocol_hint

EXPECTED_SERVICE = "4a5ad444-2537-11ee-be56-0242ac120002"
EXPECTED_CHARACTERISTIC = "4a5a54e6-2537-11ee-be56-0242ac120002"
KEY_EXCHANGE_CHARACTERISTIC = "4a5a64e6-2537-11ee-be56-0242ac120002"
DEVICE_IF = "org.bluez.Device1"
SERVICE_IF = "org.bluez.GattService1"
CHAR_IF = "org.bluez.GattCharacteristic1"
DESC_IF = "org.bluez.GattDescriptor1"
CONNECT_SECONDS = 20
RESOLVE_SECONDS = 10
DISCONNECT_SECONDS = 10
READ_SECONDS = 5
MAX_SAMPLE_BYTES = 512


@dataclass(frozen=True)
class Target:
    """Selected identity, with optional local-only diagnostic adapter metadata."""

    address: str
    name: str
    adapter_path: str | None = None
    adapter_address: str | None = None

    def __post_init__(self):
        if not isinstance(self.address, str) or not re.fullmatch(
            r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", self.address
        ):
            raise ValueError("Invalid Bluetooth address.")
        if self.adapter_path is not None or self.adapter_address is not None:
            if not isinstance(self.adapter_address, str) or not re.fullmatch(
                r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", self.adapter_address
            ):
                raise ValueError("Invalid Bluetooth address.")
            if not isinstance(self.adapter_path, str) or not re.fullmatch(
                r"/org/bluez/hci\d+", self.adapter_path
            ):
                raise ValueError("Invalid local adapter path.")
        if not self.name.startswith(("Aiper-", "Aiper_")):
            raise ValueError("Only explicitly named Aiper devices are eligible.")

    @property
    def device_path(self):
        if self.adapter_path is None:
            raise ValueError("Local diagnostic adapter is not configured.")
        return self.adapter_path + "/dev_" + self.address.replace(":", "_")


class ProbeError(RuntimeError):
    pass


class BluezError(ProbeError):
    def __init__(self, name):
        self.name = name
        super().__init__(name)


def unpack(value):
    """Unwrap D-Bus Variants without exposing unrelated device properties."""
    if hasattr(value, "value"):
        return unpack(value.value)
    if isinstance(value, dict):
        return {k: unpack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [unpack(v) for v in value]
    return value


def device_summary(properties):
    return {
        "AddressType": (
            properties.get("AddressType")
            if type(properties.get("AddressType")) is str
            and properties["AddressType"] in {"public", "random"}
            else None
        ),
        **{
            key: properties.get(key)
            for key in (
                "Address",
                "Name",
                "Connected",
                "ServicesResolved",
                "Paired",
                "Bonded",
                "Trusted",
                "Blocked",
                "RSSI",
                "UUIDs",
            )
        },
    }


def candidates(objects):
    """List known local devices only; do not start a scanner."""
    result = {}
    for path, interfaces in objects.items():
        device = interfaces.get(DEVICE_IF, {})
        if not device.get("Name", "").startswith(("Aiper-", "Aiper_")):
            continue
        adapter_path = device.get("Adapter", "")
        adapter = objects.get(adapter_path, {}).get("org.bluez.Adapter1", {})
        try:
            target = Target(
                device.get("Address", "").upper(),
                device["Name"],
                adapter_path,
                adapter.get("Address", "").upper(),
            )
        except ValueError:
            continue
        if path == target.device_path and adapter.get("Powered"):
            result[path] = target
    return result


def validate(objects, target):
    adapter = objects.get(target.adapter_path, {}).get("org.bluez.Adapter1", {})
    if adapter.get("Address", "").upper() != target.adapter_address:
        raise ProbeError("Adapter identity differs from the configured local adapter.")
    if not adapter.get("Powered"):
        raise ProbeError("Adapter is off; this probe will not enable it.")
    device = objects.get(target.device_path, {}).get(DEVICE_IF, {})
    if (
        device.get("Address", "").upper() != target.address
        or device.get("Name") != target.name
    ):
        raise ProbeError(
            "Expected robot absent or identity changed; no scan or fallback."
        )
    if device.get("Connected") is not False:
        raise ProbeError(
            "Robot already connected or connection state unknown; refusing."
        )
    if device.get("Blocked"):
        raise ProbeError("Robot blocked; this probe will not unblock it.")
    if device.get("Paired") or device.get("Bonded") or device.get("Trusted"):
        raise ProbeError(
            "Pairing/trust differs from the reviewed baseline; review first."
        )
    return device


def inventory(objects, target):
    """Return UUIDs and properties only. Never return cached Value fields."""
    services = []
    for path, interfaces in sorted(objects.items()):
        props = interfaces.get(SERVICE_IF)
        if not props or props.get("Device") != target.device_path:
            continue
        entry = {
            "uuid": props["UUID"],
            "primary": props.get("Primary"),
            "characteristics": [],
        }
        for char_path, char_interfaces in sorted(objects.items()):
            char = char_interfaces.get(CHAR_IF)
            if not char or char.get("Service") != path:
                continue
            descriptors = [
                {"uuid": item[DESC_IF]["UUID"], "flags": item[DESC_IF].get("Flags", [])}
                for item in objects.values()
                if DESC_IF in item and item[DESC_IF].get("Characteristic") == char_path
            ]
            entry["characteristics"].append(
                {
                    "uuid": char["UUID"],
                    "flags": char.get("Flags", []),
                    "descriptors": descriptors,
                }
            )
        services.append(entry)
    return services


def endpoint_properties(objects, target):
    """Resolve one exact endpoint on the pinned connected robot."""
    adapter = objects.get(target.adapter_path, {}).get("org.bluez.Adapter1", {})
    device = objects.get(target.device_path, {}).get(DEVICE_IF, {})
    if (
        adapter.get("Address", "").upper() != target.adapter_address
        or not adapter.get("Powered")
        or device.get("Address", "").upper() != target.address
        or device.get("Name") != target.name
        or device.get("Adapter") != target.adapter_path
        or device.get("Connected") is not True
        or device.get("ServicesResolved") is not True
        or any(device.get(k) for k in ("Paired", "Bonded", "Trusted", "Blocked"))
    ):
        raise ProbeError("Endpoint prerequisites changed; refusing.")
    services = [
        path
        for path, interfaces in objects.items()
        if (props := interfaces.get(SERVICE_IF))
        and props.get("Device") == target.device_path
        and props.get("UUID", "").lower() == EXPECTED_SERVICE
    ]
    if len(services) != 1 or not services[0].startswith(target.device_path + "/"):
        raise ProbeError("Expected service missing or ambiguous; refusing.")
    chars = [
        (path, props)
        for path, interfaces in objects.items()
        if (props := interfaces.get(CHAR_IF))
        and props.get("Service") == services[0]
        and props.get("UUID", "").lower() == EXPECTED_CHARACTERISTIC
    ]
    if len(chars) != 1 or not chars[0][0].startswith(services[0] + "/"):
        raise ProbeError("Expected characteristic missing or ambiguous; refusing.")
    return chars[0]


def validate_query_protocol(objects, target, query):
    """Negative evidence always vetoes; absence requires a per-call opt-in.

    This does not authorise writes on its own. query_endpoint also requires a
    connected, ServicesResolved device and an unambiguous legacy endpoint.
    Cached GATT can veto a connection, never prove a safe positive match.
    """
    if not target.name.startswith(("Aiper-Surfer S1-", "Aiper_Surfer S1_")):
        raise ProtocolError("unsupported_model")
    for path, interfaces in objects.items():
        char = interfaces.get(CHAR_IF, {})
        service = objects.get(char.get("Service"), {}).get(SERVICE_IF, {})
        if (
            path.startswith(target.device_path + "/")
            or service.get("Device") == target.device_path
        ) and char.get("UUID", "").lower() == KEY_EXCHANGE_CHARACTERISTIC:
            raise ProtocolError("ecdh_gatt_unsupported")
    hint = protocol_hint(objects.get(target.device_path, {}).get(DEVICE_IF, {}))
    if hint == "ecdh":
        raise ProtocolError("ecdh_unsupported")
    if hint == "malformed":
        raise ProtocolError("protocol_malformed")
    if hint == "unknown" and not query.allow_missing_advertisement:
        raise ProtocolError("protocol_unknown")
    return hint


def readable_path(objects, target):
    """A raw read never authorises a write or a notification subscription."""
    path, props = endpoint_properties(objects, target)
    flags = set(props.get("Flags", []))
    if (
        "read" not in flags
        or flags
        & {"encrypt-read", "encrypt-authenticated-read", "secure-read", "authorize"}
        or props.get("Notifying")
    ):
        raise ProbeError("Characteristic unreadable or notifications active; refusing.")
    return path


def sample_hex(value):
    """Accept one bounded byte array, with no parsing or protocol assumptions."""
    if not isinstance(value, (bytes, bytearray, list)):
        raise ProbeError("Read returned an invalid byte array.")
    if len(value) > MAX_SAMPLE_BYTES or any(
        type(item) is not int or not 0 <= item <= 255 for item in value
    ):
        raise ProbeError("Read returned an oversized or invalid byte array.")
    return bytes(value).hex()


class Bluez:
    """Allow metadata/connect; optionally one exact ReadValue with empty options."""

    def __init__(self, bus, target=None, *, allow_read=False):
        self.bus, self.target = bus, target
        self._allow_read = allow_read
        self._read_used = False
        self._read_path = None

    async def call(self, path, interface, member, signature="", body=None):
        allowed = {
            ("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"),
        }
        if self.target:
            allowed.update(
                {
                    (
                        self.target.device_path,
                        "org.freedesktop.DBus.Properties",
                        "GetAll",
                    ),
                    (self.target.device_path, DEVICE_IF, "Connect"),
                    (self.target.device_path, DEVICE_IF, "Disconnect"),
                }
            )
        if (
            self._read_path is not None
            and path == self._read_path
            and interface == CHAR_IF
            and member == "ReadValue"
            and signature == "a{sv}"
            and body == [{}]
        ):
            # Consume before awaiting D-Bus; even an error cannot be retried.
            self._read_path = None
        elif (path, interface, member) not in allowed:
            raise ProbeError("Operation outside diagnostic allowlist.")
        reply = await self.bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise BluezError(reply.error_name)
        return unpack(reply.body)

    async def objects(self):
        return (
            await self.call(
                "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
            )
        )[0]

    async def device(self):
        return (
            await self.call(
                self.target.device_path,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                [DEVICE_IF],
            )
        )[0]

    async def connect(self):
        await self.call(self.target.device_path, DEVICE_IF, "Connect")

    async def disconnect(self):
        await self.call(self.target.device_path, DEVICE_IF, "Disconnect")

    async def read_sample(self):
        if not self._allow_read or self._read_used or self.target is None:
            raise ProbeError("Read not authorised or already attempted.")
        self._read_used = True
        self._read_path = readable_path(await self.objects(), self.target)
        try:
            result = await self.call(
                self._read_path, CHAR_IF, "ReadValue", "a{sv}", [{}]
            )
            if len(result) != 1:
                raise ProbeError("Read returned an invalid response.")
            return result[0]
        finally:
            self._read_path = None


def error_code(exc):
    """Expose actionable fixed codes, never identifiers or payload text."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, BluezError):
        name = exc.name.rsplit(".", 1)[-1]
        if name in {
            "NotAuthorized",
            "NotPermitted",
            "NotSupported",
            "InProgress",
            "AlreadyConnected",
            "NotConnected",
            "Failed",
            "AlreadyExists",
            "InvalidArguments",
            "InvalidValueLength",
            "NotReady",
        }:
            return "bluez_" + name
        return "bluez_error"
    if isinstance(exc, ProtocolError):
        if str(exc) in {
            "invalid_checksum_mode",
            "invalid_write_mode",
            "invalid_legacy_probe_confirmation",
            "query_already_attempted",
            "listen_already_attempted",
            "connection_lost_while_listening",
            "capture_pending_at_deadline",
            "unsupported_write_or_notify_flags",
            "security_gated_endpoint",
            "unsupported_model",
            "ecdh_unsupported",
            "ecdh_gatt_unsupported",
            "protocol_malformed",
            "protocol_unknown",
            "invalid_s1_info_response",
            "invalid_info_response",
            "invalid_warn_response",
            "notification_endpoint_changed",
            "notifications_already_active",
            "notification_queue_overflow",
            "invalid_notification",
            "capture_limit",
            "frame_limit",
            "invalid_frame",
            "unsupported_response_envelope",
        }:
            return str(exc)
        return "protocol_error"
    return "validation_failed" if isinstance(exc, ProbeError) else "internal_error"


async def probe(api, target, report, connect=False, *, read=False, query=None):
    attempted = False
    try:
        report["stage"] = "preflight"
        if (read or query is not None) and not connect:
            raise ProbeError("A read requires an explicitly authorised connection.")
        if read and query is not None:
            raise ProbeError("Raw read and protocol query cannot be combined.")
        initial_objects = await asyncio.wait_for(api.objects(), 5)
        before = validate(initial_objects, target)
        report["before"] = device_summary(before)
        report["protocol_hint"] = protocol_hint(before)
        if query is not None:
            validate_query_protocol(initial_objects, target, query)
            report["legacy_probe_authorised"] = query.allow_missing_advertisement
        if not connect:
            report["status"] = "preflight_passed_no_connection"
            return
        # Recheck immediately before Connect to reduce shared-adapter races.
        fresh_objects = await asyncio.wait_for(api.objects(), 5)
        validate(fresh_objects, target)
        if query is not None:
            validate_query_protocol(fresh_objects, target, query)
        attempted = True
        report["stage"] = "connect"
        try:
            await asyncio.wait_for(api.connect(), CONNECT_SECONDS)
        except BluezError as exc:
            if exc.name in {
                "org.bluez.Error.AlreadyConnected",
                "org.bluez.Error.InProgress",
            }:
                # Another client may own this connection; never disconnect it.
                attempted = False
                report["cleanup"] = "not_touched_possible_other_client"
            raise
        report["stage"] = "service_resolution"
        async with asyncio.timeout(RESOLVE_SECONDS):
            while True:
                state = await api.device()
                if not state.get("Connected"):
                    raise ProbeError("Connection lost before service discovery.")
                if state.get("ServicesResolved"):
                    break
                await asyncio.sleep(0.25)
        report["during"] = device_summary(state)
        report["stage"] = "inventory"
        services = inventory(await asyncio.wait_for(api.objects(), 5), target)
        report["services"] = services
        expected_service = next(
            (s for s in services if s["uuid"].lower() == EXPECTED_SERVICE), None
        )
        characteristic = next(
            (
                c
                for c in (expected_service or {}).get("characteristics", [])
                if c["uuid"].lower() == EXPECTED_CHARACTERISTIC
            ),
            None,
        )
        report["expected_service_found"] = expected_service is not None
        report["expected_characteristic_found_in_expected_service"] = (
            characteristic is not None
        )
        report["expected_characteristic_flags"] = (characteristic or {}).get(
            "flags", []
        )
        report["status"] = (
            "discovery_complete" if services else "no_gatt_services_returned"
        )
        if read:
            report["stage"] = "raw_read"
            # Includes fresh endpoint validation and one D-Bus ReadValue.
            report["read_attempted"] = True
            value = await asyncio.wait_for(api.read_sample(), READ_SECONDS)
            report["sample_hex"] = sample_hex(value)
            report["sample_bytes"] = len(value)
            report["sample_interpretation"] = "unparsed" if value else "empty"
            report["status"] = "read_complete"
        if query is not None:
            await api.query_once(report)
    except asyncio.CancelledError:
        report["status"] = "interrupted"
    except Exception as exc:  # noqa: BLE001 - record failure and always enter cleanup
        report["status"] = "failed"
        report["error_code"] = error_code(exc)
        report["error_category"] = (
            "protocol"
            if isinstance(exc, ProtocolError)
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "transport"
        )
        report["failure_stage"] = report.get("stage", "unknown")
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if attempted:
            try:
                try:
                    await asyncio.wait_for(api.disconnect(), DISCONNECT_SECONDS)
                except BluezError as exc:
                    if exc.name != "org.bluez.Error.NotConnected":
                        raise
                after = await asyncio.wait_for(api.device(), 5)
                report["after"] = device_summary(after)
                if after.get("Connected") is not False:
                    raise ProbeError("Disconnection not confirmed.")
                report["cleanup"] = "disconnected_confirmed"
                for key in ("Paired", "Bonded", "Trusted"):
                    if after.get(key) != report["before"].get(key):
                        raise ProbeError(f"{key} changed; stop and review.")
            except Exception as exc:  # noqa: BLE001 - never hide an unconfirmed disconnect
                report["cleanup"] = f"unconfirmed: {type(exc).__name__}"
                report["error"] = f"{type(exc).__name__}: {exc}"
                report["status"] = "cleanup_requires_review"
                report["cleanup_error_code"] = error_code(exc)
        if (
            report.get("notification_cleanup")
            in {"stop_unconfirmed", "ownership_uncertain"}
            or report.get("signal_cleanup") == "remove_match_unconfirmed"
        ):
            report["status"] = "cleanup_requires_review"


@asynccontextmanager
async def open_bluez(target=None, *, allow_read=False, query=None):
    """Open only the local system bus; do not connect to any BLE device."""
    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await asyncio.wait_for(bus.connect(), 5)
        if query is not None:
            from .listener import ListenBluez
            from .protocol import Listen
            from .telemetry import QueryBluez

            if allow_read:
                raise ProbeError("Cannot combine raw read and protocol query.")
            transport = ListenBluez if isinstance(query, Listen) else QueryBluez
            yield transport(bus, target, query)
        else:
            yield Bluez(bus, target, allow_read=allow_read)
    finally:
        bus.disconnect()
