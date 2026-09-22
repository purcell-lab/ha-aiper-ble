"""Bounded notification observation. No application query, read or control."""

import asyncio
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .probe import CHAR_IF, BluezError, ProbeError, Target
from .protocol import MAX_TOTAL_BYTES, Control, Decoder, Listen, ProtocolError, Query
from .telemetry import (
    NOTIFY_CLEANUP_SECONDS,
    PROPERTIES,
    QueryBluez,
    query_endpoint,
)

LISTEN_SECONDS = 60
SETUP_SECONDS = 15


class ListenBluez(QueryBluez):
    """Reuse exact subscription/routing permits, but deny all application I/O."""

    def __init__(
        self, bus: MessageBus, target: Target, query: Query | Control | Listen
    ) -> None:
        if not isinstance(query, Listen):
            raise ProtocolError("not_a_listener")
        super().__init__(bus, target, query)

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> Any:
        if member in {"WriteValue", "ReadValue"}:
            raise ProbeError("Application I/O forbidden in listen-only mode.")
        return await super().call(path, interface, member, signature, body)

    async def _exact(
        self,
        path: str | None,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> Any:
        if member not in {"StartNotify", "StopNotify"}:
            raise ProbeError("Only notification subscription is authorised.")
        return await super()._exact(path, member, signature, body)

    async def query_once(self, report: dict[str, Any]) -> None:
        """Transport hook used by the shared probe; deliberately sends no query."""
        if self._query_used:
            raise ProtocolError("listen_already_attempted")
        self._query_used = True
        report.update(
            command=None,
            write_attempts=0,
            writes_completed=0,
            read_attempts=0,
            listen_seconds_requested=LISTEN_SECONDS,
            notification_samples=[],
            protocol_responses=[],
            response_checksum_validation="not_verified",
        )
        queue: asyncio.Queue[tuple[float, Any]] = asyncio.Queue(maxsize=32)
        overflow = False
        handler_added = match_attempted = notify_attempted = False
        path: str | None = None
        owner: str | None = None
        rule = ""
        started: float | None = None
        decoder = Decoder()
        loop = asyncio.get_running_loop()

        def handler(message: Message) -> None:
            nonlocal overflow
            if (
                started is None
                or loop.time() > started + LISTEN_SECONDS
                or message.message_type != MessageType.SIGNAL
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
                queue.put_nowait((loop.time(), value))

        try:
            async with asyncio.timeout(SETUP_SECONDS):
                report["stage"] = "listen_validation"
                path, _ = query_endpoint(await self.objects(), self.pinned, self.query)
                self._query_path = path
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
                # Recheck after asynchronous routing setup, before subscribing.
                fresh_path, _ = query_endpoint(
                    await self.objects(), self.pinned, self.query
                )
                if fresh_path != path:
                    raise ProtocolError("notification_endpoint_changed")
                report["stage"] = "notify_start"
                notify_attempted = True
                started = loop.time()
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
                fresh_path, props = query_endpoint(
                    await self.objects(), self.pinned, self.query, active=True
                )
                if fresh_path != path or props.get("Notifying") is not True:
                    raise ProtocolError("notification_endpoint_changed")
            report["stage"] = "listen"
            deadline = started + LISTEN_SECONDS
            # Include startup notifications, unlike query-response attribution.
            while True:
                if overflow:
                    raise ProtocolError("notification_queue_overflow")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    received_at, value = await asyncio.wait_for(
                        queue.get(), min(remaining, 1)
                    )
                except TimeoutError:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    # Cached BlueZ properties only, never a GATT read.
                    state = await asyncio.wait_for(self.device(), min(remaining, 2))
                    if state.get("Connected") is not True:
                        raise ProtocolError("connection_lost_while_listening")
                    continue
                if isinstance(value, ProtocolError):
                    raise value
                previous_bytes = decoder.total_bytes
                frames = decoder.feed(value)
                report["notification_samples"].append(
                    {
                        "elapsed_seconds": round(received_at - started, 6),
                        "bytes": decoder.total_bytes - previous_bytes,
                        "hex": bytes(value).hex(),
                    }
                )
                report["protocol_responses"].extend(frames)
            if overflow or not queue.empty():
                raise ProtocolError("capture_pending_at_deadline")
            # Confirm the window did not silently end on a lost connection.
            state = await asyncio.wait_for(self.device(), 2)
            if state.get("Connected") is not True:
                raise ProtocolError("connection_lost_while_listening")
            report["status"] = "listen_complete"
            report["partial_frame_bytes"] = len(decoder.buffer)
        finally:
            report["listen_seconds_observed"] = (
                round(loop.time() - started, 6) if started is not None else 0
            )
            report["notification_count"] = decoder.notifications
            report["received_bytes"] = decoder.total_bytes
            report["frame_count"] = len(report["protocol_responses"])
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
