"""One fixed opt-in status exchange on a pinned S1, without retry or controls."""

import asyncio

from dbus_fast import Message, MessageType, Variant

from .probe import (
    CHAR_IF,
    Bluez,
    BluezError,
    ProbeError,
    endpoint_properties,
    unpack,
    validate_query_protocol,
)
from .protocol import (
    MAX_TOTAL_BYTES,
    Decoder,
    Listen,
    ProtocolError,
    chunks,
    query_frame,
    query_telemetry,
)

EXCHANGE_SECONDS = 15
NOTIFY_CLEANUP_SECONDS = 5
DBUS = "org.freedesktop.DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"


def query_endpoint(objects, target, query, *, active=False):
    """Require query flags independently of the raw-read flag."""
    path, props = endpoint_properties(objects, target)
    if props.get("Notifying") and not active:
        raise ProtocolError("notifications_already_active")
    flags = set(props.get("Flags", []))
    required = {"notify"}
    if not isinstance(query, Listen):
        required.add(
            "write-without-response" if query.write_mode == "command" else "write"
        )
    if not required <= flags:
        raise ProtocolError("unsupported_write_or_notify_flags")
    if flags & {
        "encrypt-write",
        "encrypt-authenticated-write",
        "secure-write",
        "authorize",
        "encrypt-read",
        "encrypt-authenticated-read",
        "secure-read",
    }:
        raise ProtocolError("security_gated_endpoint")
    validate_query_protocol(objects, target, query)
    return path, props


class QueryBluez(Bluez):
    """Keep legacy discovery/read allowlists unchanged; permit exact messages."""

    def __init__(self, bus, target, query):
        super().__init__(bus, target)
        self.query = query
        self._query_used = False
        self._permit = None
        self._query_path = None
        self._start_used = self._stop_used = False
        self._write_plan = []

    async def call(self, path, interface, member, signature="", body=None):
        attempted = (path, interface, member, signature, body or [])
        if self._permit is None or attempted != self._permit:
            return await super().call(path, interface, member, signature, body)
        self._permit = None
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

    async def _exact(self, path, member, signature="", body=None):
        if path is None or path != self._query_path:
            raise ProbeError("Query endpoint not authorised.")
        if (
            member == "StartNotify"
            and not signature
            and not body
            and not self._start_used
        ):
            self._start_used = True
        elif (
            member == "StopNotify"
            and not signature
            and not body
            and self._start_used
            and not self._stop_used
        ):
            self._stop_used = True
        elif (
            member == "WriteValue"
            and signature == "aya{sv}"
            and self._write_plan
            and body == self._write_plan[0]
        ):
            self._write_plan.pop(0)
        else:
            raise ProbeError("Operation outside one-shot query plan.")
        self._permit = (path, CHAR_IF, member, signature, body or [])
        try:
            return await self.call(path, CHAR_IF, member, signature, body)
        finally:
            self._permit = None

    async def _daemon(self, member, value):
        """Private fixed D-Bus routing operations, never Bluetooth properties."""
        if member not in {"GetNameOwner", "AddMatch", "RemoveMatch"}:
            raise ProbeError("Unsupported signal routing operation.")
        reply = await self.bus.call(
            Message(
                destination=DBUS,
                path="/org/freedesktop/DBus",
                interface=DBUS,
                member=member,
                signature="s",
                body=[value],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise BluezError(reply.error_name)
        return unpack(reply.body)

    async def query_once(self, report):
        if self._query_used:
            raise ProtocolError("query_already_attempted")
        self._query_used = True
        report.update(
            command=self.query.query_type,
            checksum_mode=self.query.checksum_mode,
            write_mode=self.query.write_mode,
            write_attempts=0,
            writes_completed=0,
            protocol_evidence="not_yet_validated",
        )
        queue = asyncio.Queue(maxsize=32)
        overflow = False
        handler_added = match_attempted = notify_attempted = False
        path = rule = owner = None
        decoder = Decoder()

        def handler(message):
            nonlocal overflow
            if (
                message.message_type != MessageType.SIGNAL
                or message.sender != owner
                or message.path != path
                or message.interface != PROPERTIES
                or message.member != "PropertiesChanged"
                or len(message.body) != 3
                or message.body[0] != CHAR_IF
                or not isinstance(message.body[1], dict)
                or "Value" not in message.body[1]
            ):
                return
            # Never inspect cached Value properties or unrelated device signals.
            value = message.body[1]["Value"]
            value = value.value if isinstance(value, Variant) else value
            if (
                isinstance(value, (bytes, bytearray, list))
                and len(value) > MAX_TOTAL_BYTES
            ):
                value = ProtocolError("capture_limit")
            if queue.full():
                overflow = True
            else:
                queue.put_nowait(value)

        def decode(value):
            if isinstance(value, ProtocolError):
                raise value
            return decoder.feed(value)

        try:
            async with asyncio.timeout(EXCHANGE_SECONDS):
                report["stage"] = "query_validation"
                data = await self.objects()
                path, props = query_endpoint(data, self.target, self.query)
                self._query_path = path
                report["protocol"] = "legacy_xor"
                report["negotiated_mtu"] = (
                    props.get("MTU") if type(props.get("MTU")) is int else None
                )
                owner = (await self._daemon("GetNameOwner", "org.bluez"))[0]
                rule = (
                    "type='signal',sender='org.bluez',"
                    f"interface='{PROPERTIES}',member='PropertiesChanged',"
                    f"path='{path}',arg0='{CHAR_IF}'"
                )
                self.bus.add_message_handler(handler)
                handler_added = True
                match_attempted = True
                await self._daemon("AddMatch", rule)
                report["stage"] = "notify_start"
                notify_attempted = True
                try:
                    await self._exact(path, "StartNotify")
                except BluezError as exc:
                    if exc.name in {
                        "org.bluez.Error.InProgress",
                        "org.bluez.Error.AlreadyExists",
                    }:
                        notify_attempted = False
                        report["notification_cleanup"] = "ownership_uncertain"
                    raise
                report["notification_started"] = True
                report["stage"] = "query_revalidation"
                fresh_objects = await self.objects()
                fresh_path, props = query_endpoint(
                    fresh_objects, self.target, self.query, active=True
                )
                if fresh_path != path or props.get("Notifying") is not True:
                    raise ProtocolError("notification_endpoint_changed")
                hint = validate_query_protocol(fresh_objects, self.target, self.query)
                report["protocol_evidence"] = (
                    "explicit_probe_resolved_legacy_gatt_no_advertisement"
                    if hint == "unknown"
                    else "cached_advertisement_and_resolved_legacy_gatt"
                )
                report["wire_compatibility"] = "unverified"
                # Discard startup notifications, not counted as a query response.
                while not queue.empty():
                    decode(queue.get_nowait())
                decoder.buffer.clear()
                report["stage"] = "query_write"
                self._write_plan = [
                    [chunk, {"type": Variant("s", self.query.write_mode)}]
                    for chunk in chunks(query_frame(self.query), props.get("MTU"))
                ]
                while self._write_plan:
                    if overflow:
                        raise ProtocolError("notification_queue_overflow")
                    report["write_attempts"] += 1
                    await asyncio.wait_for(
                        self._exact(
                            path,
                            "WriteValue",
                            "aya{sv}",
                            self._write_plan[0],
                        ),
                        5,
                    )
                    report["writes_completed"] += 1
                report["stage"] = "query_response"
                while True:
                    if overflow:
                        raise ProtocolError("notification_queue_overflow")
                    frames = decode(await queue.get())
                    for response in frames:
                        candidates = query_telemetry(response, self.query)
                        if candidates is None:
                            continue
                        report["protocol_response"] = response
                        report["telemetry_candidates"] = candidates
                        report["temperature_interpretation"] = (
                            "app_3_6_1_celsius_div10_sensor_location_unverified"
                            if self.query.query_type == "S1_INFO"
                            else "unverified_no_units"
                        )
                        report["response_match"] = (
                            "type_and_s1_info_prefix_no_request_id"
                            if self.query.query_type == "S1_INFO"
                            else "type_only_no_request_id"
                        )
                        report["response_checksum_validation"] = "not_verified"
                        report["status"] = "query_complete"
                        return
        finally:
            report["notification_count"] = decoder.notifications
            report["received_bytes"] = decoder.total_bytes
            cleanup_failed = False
            if notify_attempted:
                try:
                    await asyncio.wait_for(
                        self._exact(path, "StopNotify"), NOTIFY_CLEANUP_SECONDS
                    )
                    report["notification_cleanup"] = "stop_confirmed"
                except Exception:  # noqa: BLE001 - disconnect must still run
                    report["notification_cleanup"] = "stop_unconfirmed"
                    cleanup_failed = True
            if handler_added:
                self.bus.remove_message_handler(handler)
            if match_attempted:
                try:
                    await asyncio.wait_for(self._daemon("RemoveMatch", rule), 5)
                except Exception:  # noqa: BLE001 - preserve disconnect path
                    report["signal_cleanup"] = "remove_match_unconfirmed"
                    cleanup_failed = True
            self._permit = None
            self._query_path = None
            self._write_plan.clear()
            if cleanup_failed:
                report["status"] = "cleanup_requires_review"
