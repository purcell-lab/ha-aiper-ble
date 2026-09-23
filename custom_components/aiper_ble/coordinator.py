"""Opt-in S1 polling over an explicitly selected guarded Bluetooth transport."""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .bluetooth_transport import query_once
from .const import DOMAIN
from .datapoints import opinfo_values, timezone
from .errors import validation
from .local_bleak import query_once as local_bleak_query_once
from .local_transport import query_once as local_query_once
from .probe import Target
from .protocol import Control, ProtocolError, Query, crc16, query_telemetry
from .s1_states import info_state

if TYPE_CHECKING:
    from . import Runtime

QueryOnce = Callable[
    [HomeAssistant, Target, dict[str, Any], Query | Control], Awaitable[None]
]

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 60
MAX_INTERVAL = 3600
# An interval shorter than SLOW_INTERVAL is used only while the last cycle's
# route reported at least FAST_POLL_MIN_RSSI; a weaker link polls slowly.
SLOW_INTERVAL = 300
FAST_POLL_MIN_RSSI = -90
POLL_SECONDS = 180
SINGLE_QUERY_SECONDS = 60
# A verified control is confirmed by a full cycle this long after it finishes.
POST_CONTROL_SECONDS = 20
LOCAL_POLL_QUERIES = ("S1_INFO", "INFO")
HA_POLL_QUERIES = ("S1_INFO", "OpInfo", "INFO", "WARN")
LOCAL_BLEAK_SERVICE = "query_opinfo_local_bleak"
PROXY_TRACE_SERVICE = "query_opinfo_proxy_trace"
ISOLATED_QUERIES = {
    "query_s1_info": "S1_INFO",
    "query_opinfo": "OpInfo",
    "query_info": "INFO",
    "query_warn": "WARN",
}
POLL_DETAIL_FIELDS = (
    "query_type",
    "cycle_query_index",
    "seconds_since_previous_query",
    "response_evidence",
    "transport",
    "transport_diagnostics",
    "status",
    "phase",
    "failure_stage",
    "error_code",
    "error_category",
    "preconnect_rssi_dbm",
    "connect_ms",
    "write_attempts",
    "notification_count",
    "received_bytes",
    "cleanup",
    "notification_cleanup",
    "signal_cleanup",
    "cleanup_error_category",
    "notification_cleanup_error_category",
    "proxy_trace",
)
SUSPEND_CODES = {
    "cleanup_requires_review",
    "ecdh_unsupported",
    "ecdh_gatt_unsupported",
    "protocol_malformed",
    "unsupported_model",
    "security_gated_endpoint",
}


def verified_values(response: object, query: Query | Control) -> dict[str, Any]:
    """Publish only matched, successful, CRC-verified allowlisted scalar values.

    The legacy CRC covers compact UTF-8 JSON data in received key order. It is
    corruption detection, not authentication. All data fields participate in
    the CRC; only explicitly allowlisted fields enter coordinator data.
    """
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise ProtocolError("invalid_response")
    if type(response.get("res")) is not int or response["res"] != 0:
        raise ProtocolError("response_not_successful")
    checksum = response.get("chksum")
    if type(checksum) is not int or not 0 <= checksum <= 65535:
        raise ProtocolError("response_checksum_missing")
    encoded = json.dumps(
        response["data"], separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if crc16(encoded) != checksum:
        raise ProtocolError("response_checksum_mismatch")
    if not isinstance(response.get("type"), str):
        raise ProtocolError("invalid_response")
    values = query_telemetry(response, query)
    if values is None:
        raise ProtocolError("response_query_mismatch")
    if isinstance(query, Control) or query.query_type in {"INFO", "WARN"}:
        return values
    if query.query_type == "S1_INFO":
        return {
            "temperature": values["temperature_celsius"],
            "temperature_raw": values["temperature_raw"],
            "solar_status_raw": values["solar_status"],
            "s1_timezone": timezone(response["data"].get("timeZone")),
        }
    return opinfo_values(response["data"])


def cancel_requested() -> bool:
    """True when the running task has been asked to cancel during cleanup."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class AiperCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Serialize polls with manual probes; no retries within a polling cycle."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, runtime: "Runtime"
    ) -> None:
        self.runtime = runtime
        self.entry_id = entry.entry_id
        self.enabled = (
            entry.options.get("polling_enabled") is True
            and entry.options.get("confirm_exclusive_access") is True
        )
        interval = entry.options.get("poll_interval", DEFAULT_INTERVAL)
        if type(interval) is not int or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            interval = DEFAULT_INTERVAL
        self.interval: int = interval
        self.allow_missing = entry.options.get("allow_missing_advertisement") is True
        self.use_local_adapter = entry.options.get("use_local_adapter") is True
        self.transport = "local_bluez" if self.use_local_adapter else "ha_bluetooth"
        self.poll_queries = (
            LOCAL_POLL_QUERIES if self.use_local_adapter else HA_POLL_QUERIES
        )
        self.status = "waiting" if self.enabled else "disabled"
        self.error_code: str | None = None
        self._suspended = False
        self.failures = 0
        self.next_attempt = 0.0
        self.last_attempt_finished: float | None = None
        self.last_poll_details: dict[str, Any] = {}
        self.last_poll_queries: list[dict[str, Any]] = []
        self.last_control_result: dict[str, Any] = {"status": "never_run"}
        # Temperature captured only in cycles where the robot reports working.
        self.water_temperature: float | None = None
        self.water_temperature_at: datetime | None = None
        self._post_control_timer: CALLBACK_TYPE | None = None
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval) if self.enabled else None,
        )
        self.last_update_success = False
        # A fresh coordinator (setup or reload) clears any earlier repair issue.
        self.suspended = False

    @property
    def suspended(self) -> bool:
        return self._suspended

    @suspended.setter
    def suspended(self, value: bool) -> None:
        """Suspension is surfaced as a repair issue; reloading the entry fixes it."""
        self._suspended = bool(value)
        issue_id = f"polling_suspended_{self.entry_id}"
        if self._suspended:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="polling_suspended",
                translation_placeholders={"error_code": str(self.error_code)},
                data={"entry_id": self.entry_id},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _async_refresh_finished(self) -> None:
        """Keep diagnostic status current even across consecutive failures."""
        self.async_update_listeners()

    @callback
    def _publish_manual_error(self, error: Exception) -> None:
        """Publish repeated failures without duplicating first-failure events."""
        was_successful = self.last_update_success
        self.async_set_update_error(error)
        if not was_successful:
            self.async_update_listeners()

    @property
    def last_route(self) -> dict[str, Any] | None:
        """Backend, scanner type and signal of the last cycle's selected route."""
        if not self.last_poll_queries:
            return None
        diagnostics = self.last_poll_queries[-1].get("transport_diagnostics") or {}
        route: dict[str, Any] = {"backend": diagnostics.get("backend")}
        snapshot = (diagnostics.get("route_snapshots") or {}).get("before_connect")
        for candidate in (snapshot or {}).get("routes", []):
            if candidate.get("route_id") == diagnostics.get("selected_route"):
                route["scanner_type"] = candidate.get("scanner_type")
                route["rssi_dbm"] = candidate.get("rssi_dbm")
        return route

    @property
    def signal_strength(self) -> int | float | None:
        """RSSI of the route the last cycle connected through, in dBm.

        HA routing reports the selected proxy's reading just before the
        connection; the pinned local adapter reports its own pre-connect
        reading. Nothing is inferred when the cycle never selected a route.
        """
        route = self.last_route
        if route is None:
            return None
        value = route.get("rssi_dbm")
        if value is None:
            value = self.last_poll_queries[-1].get("preconnect_rssi_dbm")
        return value if isinstance(value, (int, float)) else None

    @property
    def effective_interval(self) -> int:
        """The configured interval, or SLOW_INTERVAL while the link is too weak."""
        if self.interval >= SLOW_INTERVAL:
            return self.interval
        signal = self.signal_strength
        if signal is not None and signal >= FAST_POLL_MIN_RSSI:
            return self.interval
        return SLOW_INTERVAL

    @callback
    def schedule_post_control_poll(self) -> None:
        """Confirm a control with a full cycle shortly after it, not an interval later.

        Regular ticks stay refused until then; the timer clears the cooldown
        and requests the cycle, which then reschedules the normal interval.
        """
        self.cancel_post_control_poll()
        self.next_attempt = asyncio.get_running_loop().time() + POST_CONTROL_SECONDS
        self._post_control_timer = async_call_later(
            self.hass, POST_CONTROL_SECONDS, self._post_control_poll
        )

    @callback
    def cancel_post_control_poll(self) -> None:
        if self._post_control_timer is not None:
            self._post_control_timer()
            self._post_control_timer = None

    @callback
    def _post_control_poll(self, _now: datetime) -> None:
        self._post_control_timer = None
        if not self.enabled or self.runtime.closing or self.suspended:
            return
        if self.runtime.task and not self.runtime.task.done():
            return  # a cycle or probe is already running; it confirms the state
        self.next_attempt = 0.0
        self.hass.async_create_task(self.async_request_refresh())

    async def async_shutdown(self) -> None:
        self.cancel_post_control_poll()
        await super().async_shutdown()

    @callback
    def mark_stale(self, reason: str) -> None:
        """Retire retained data without Home Assistant's error log.

        After a control the last cycle predates the command, so it must not be
        presented as current; nothing has failed, so this is not an error.
        """
        self.last_exception = UpdateFailed(reason)
        self.last_update_success = False
        self.async_update_listeners()
        LOGGER.debug(
            "%s; next poll due in %.0f s",
            reason,
            max(0.0, self.next_attempt - asyncio.get_running_loop().time()),
        )

    async def async_poll_now(self) -> dict[str, Any]:
        """Run one existing guarded cycle immediately, never concurrently.

        An explicit operator request runs at once: it clears failure backoff and
        does not wait for the configured normal interval. The exclusive-cycle
        guard, suspension, protocol checks and cleanup rules still apply.
        No await precedes _async_update_data's existing runtime task reservation.
        """
        if not self.enabled or self.runtime.closing:
            raise validation("enable_polling_first")
        if self.suspended:
            raise validation("polling_suspended")
        if self.runtime.task and not self.runtime.task.done():
            raise validation("already_running")
        self.next_attempt = 0.0
        try:
            data = await self._async_update_data()
        except asyncio.CancelledError:
            self._publish_manual_error(UpdateFailed("Polling interrupted"))
            raise
        except UpdateFailed as exc:
            self._publish_manual_error(exc)
            raise
        else:
            # Publish through HA and reschedule the next normal automatic poll.
            self.async_set_updated_data(data)
            return {
                "status": self.status,
                "last_successful_poll": data["last_success"].isoformat(),
            }

    async def async_query_isolated(
        self,
        query_type: str,
        *,
        local_bleak: bool = False,
        proxy_entry_id: str | None = None,
    ) -> dict[str, Any]:
        """One production-transport query, without publishing partial telemetry.

        Recurring polling must be disabled so automatic cycles cannot contaminate
        a bisection. All four actions share the same completion-based cooldown,
        task mutex, protocol verification and cleanup suspension.
        """
        if query_type not in ISOLATED_QUERIES.values():
            raise validation("select_fixed_query")
        if local_bleak and query_type != "OpInfo":
            raise validation("same_radio_opinfo_only")
        if proxy_entry_id is not None and (local_bleak or query_type != "OpInfo"):
            raise validation("proxy_trace_opinfo_only")
        if self.enabled:
            raise validation("disable_polling_for_bisection")
        if self.runtime.closing or self.suspended:
            raise validation("closing_or_suspended")
        if self.runtime.task and not self.runtime.task.done():
            raise validation("already_running")
        now = asyncio.get_running_loop().time()
        if self.last_attempt_finished is not None:
            remaining = self.interval - (now - self.last_attempt_finished)
            if remaining > 0:
                raise validation("cooldown", seconds=math.ceil(remaining))
        # No await before reservation: all diagnostic actions use this mutex.
        self.runtime.task = asyncio.current_task()
        report: dict[str, Any] = {"query_type": query_type}
        started = dt_util.utcnow().isoformat()
        values: dict[str, Any] = {}
        query = Query("omit_empty_crc", "request", self.allow_missing, query_type)
        try:
            async with asyncio.timeout(
                80 if proxy_entry_id is not None else SINGLE_QUERY_SECONDS
            ):
                execute: QueryOnce = (
                    local_query_once if self.use_local_adapter else query_once
                )
                if local_bleak:
                    execute = local_bleak_query_once
                if proxy_entry_id is not None:
                    from .proxy_trace import query_once as proxy_trace_query

                    await proxy_trace_query(
                        self.hass, self.runtime.target, report, query, proxy_entry_id
                    )
                else:
                    await execute(self.hass, self.runtime.target, report, query)
                if report.get("status") == "cleanup_requires_review":
                    self.suspended = True
                    report["error_code"] = "cleanup_requires_review"
                if cancel_requested():
                    raise asyncio.CancelledError
                if (
                    report.get("status") != "query_complete"
                    or report.get("cleanup") != "disconnected_confirmed"
                    or report.get("notification_cleanup") != "stop_confirmed"
                ):
                    raise UpdateFailed(report.get("error_code", "query_incomplete"))
                report["phase"] = "verify_response"
                values = {
                    key: value
                    for key, value in verified_values(
                        report.get("protocol_response"), query
                    ).items()
                    if value is not None
                }
        except asyncio.CancelledError:
            report["status"] = "interrupted"
            report.setdefault("failure_stage", report.get("phase", "query"))
            report.setdefault("error_category", "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - never expose backend text
            report["status"] = "failed"
            report.setdefault("failure_stage", report.get("phase", "query"))
            report.setdefault(
                "error_code",
                str(exc)
                if isinstance(exc, (ProtocolError, UpdateFailed))
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "transport_error",
            )
            report.setdefault(
                "error_category",
                "protocol"
                if isinstance(exc, ProtocolError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "transport",
            )
        finally:
            if report.get("proxy_trace", {}).get("log_cleanup") == "unconfirmed":
                self.suspended = True
                report["error_code"] = "cleanup_requires_review"
            if (
                (
                    report.get("cleanup") != "disconnected_confirmed"
                    and report.get("cleanup") is not None
                )
                or report.get("notification_cleanup")
                in {
                    "stop_unconfirmed",
                    "ownership_uncertain",
                }
                or report.get("signal_cleanup") == "remove_match_unconfirmed"
            ):
                self.suspended = True
                report["error_code"] = "cleanup_requires_review"
            if report.get("error_code") in SUSPEND_CODES:
                self.suspended = True
            if self.suspended:
                self.status = "suspended"
                self.error_code = report["error_code"]
                self.update_interval = None
                report["status"] = "suspended"
                values = {}
                self._publish_manual_error(UpdateFailed(self.error_code))
            finished = asyncio.get_running_loop().time()
            self.last_attempt_finished = finished
            self.next_attempt = finished + self.interval
            # Only verified allowlisted scalars, never raw frames or identifiers.
            self.runtime.last_result = {
                "mode": (
                    "isolated_ha_proxy_trace"
                    if proxy_entry_id is not None
                    else "isolated_ha_bluetooth_local_query"
                    if local_bleak
                    else f"isolated_{self.transport}_query"
                ),
                "started_utc": started,
                "finished_utc": dt_util.utcnow().isoformat(),
                **{key: report[key] for key in POLL_DETAIL_FIELDS if key in report},
                "values": values,
                "updates_entities": False,
            }
            self.runtime.task = None
        return dict(self.runtime.last_result)

    async def _async_update_data(self) -> dict[str, Any]:
        if not self.enabled or self.runtime.closing:
            raise UpdateFailed("Polling disabled")
        if self.suspended:
            raise UpdateFailed("Polling suspended; review diagnostics before reload")
        now = asyncio.get_running_loop().time()
        # HA schedules coordinator ticks with sub-second jitter.
        if now + 1 < self.next_attempt:
            # A control moved the cooldown without moving HA's tick. Land the
            # next tick at the cooldown end rather than a full interval later;
            # the cycle that then runs restores the normal or backoff interval.
            self.update_interval = timedelta(seconds=max(1.0, self.next_attempt - now))
            if self.last_update_success and self.data is not None:
                return self.data
            raise UpdateFailed("Waiting for the next bounded polling interval")
        if self.runtime.task and not self.runtime.task.done():
            self.status = "busy"
            self.error_code = "manual_probe_running"
            raise UpdateFailed("A manual probe is already running")
        self.runtime.task = asyncio.current_task()
        self.status = "polling"
        self.error_code = None
        report: dict[str, Any] = {}
        reports: list[dict[str, Any]] = []
        try:
            values: dict[str, Any] = {}
            # One connection per fixed request. Local mode pins the saved BlueZ
            # adapter; HA mode selects a route. Never retry or change transport.
            previous_finished: float | None = None
            async with asyncio.timeout(POLL_SECONDS):
                for index, query_type in enumerate(self.poll_queries, 1):
                    if self.runtime.closing:
                        raise asyncio.CancelledError
                    query = Query(
                        "omit_empty_crc", "request", self.allow_missing, query_type
                    )
                    report = {"query_type": query_type, "cycle_query_index": index}
                    if previous_finished is not None:
                        # Measured from the previous query's returned transport
                        # call, which completes after its confirmed cleanup.
                        report["seconds_since_previous_query"] = round(
                            asyncio.get_running_loop().time() - previous_finished, 3
                        )
                    reports.append(report)
                    execute: QueryOnce = (
                        local_query_once if self.use_local_adapter else query_once
                    )
                    await execute(self.hass, self.runtime.target, report, query)
                    previous_finished = asyncio.get_running_loop().time()
                    if report.get("status") == "cleanup_requires_review":
                        self.suspended = True
                    # The diagnostic probe records cancellation after cleanup.
                    # Do not swallow unload/deadline cancellation or start query 2.
                    if cancel_requested():
                        raise asyncio.CancelledError
                    if self.suspended:
                        raise UpdateFailed("cleanup_requires_review")
                    if report.get("error_code") in SUSPEND_CODES:
                        self.suspended = True
                    if (
                        report.get("status") != "query_complete"
                        or report.get("cleanup") != "disconnected_confirmed"
                        or report.get("notification_cleanup") != "stop_confirmed"
                    ):
                        raise UpdateFailed(report.get("error_code", "query_incomplete"))
                    report["phase"] = "verify_response"
                    values.update(
                        verified_values(report.get("protocol_response"), query)
                    )
            values["last_success"] = dt_util.utcnow()
            if (
                values.get("info_status_raw") == 1
                and info_state(values) == "working"
                and values.get("temperature") is not None
            ):
                # The only cycles in which the robot is certainly in the water
                # and moving; the sensor's physical location stays unverified.
                self.water_temperature = values["temperature"]
                self.water_temperature_at = values["last_success"]
            self.failures = 0
            self.status = "ok"
            self.error_code = None
            self.update_interval = timedelta(seconds=self.interval)
            return values
        except asyncio.CancelledError:
            self.status = "interrupted"
            report.setdefault("failure_stage", report.get("phase", "query"))
            report.setdefault("error_category", "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - do not expose transport identifiers
            self.failures += 1
            self.status = "suspended" if self.suspended else "failed"
            self.error_code = (
                str(exc)
                if isinstance(exc, (ProtocolError, UpdateFailed))
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "transport_error"
            )
            if isinstance(exc, TimeoutError):
                report.setdefault("failure_stage", report.get("phase", "query"))
                report.setdefault("error_category", "timeout")
            elif isinstance(exc, ProtocolError):
                report.setdefault("failure_stage", report.get("phase", "query"))
                report.setdefault("error_category", "protocol")
            report["status"] = self.status
            report.setdefault("error_code", self.error_code)
            # Backoff starts from the slow interval so a fast configured
            # interval does not shorten the retreat from a failing link.
            self.update_interval = (
                None
                if self.suspended
                else timedelta(
                    seconds=min(
                        MAX_INTERVAL,
                        max(self.interval, SLOW_INTERVAL) * 2 ** min(self.failures, 4),
                    )
                )
            )
            raise UpdateFailed(self.error_code) from None
        finally:
            # Never retain response payloads, exception text or route identifiers.
            self.last_poll_details = {
                key: report[key] for key in POLL_DETAIL_FIELDS if key in report
            }
            self.last_poll_queries = [
                {key: item[key] for key in POLL_DETAIL_FIELDS if key in item}
                for item in reports
            ]
            if self.status == "ok":
                # Decided from this cycle's own route signal.
                self.update_interval = timedelta(seconds=self.effective_interval)
            # Start the cooldown after cleanup, not before a slow connection.
            delay = (
                self.update_interval.total_seconds()
                if self.update_interval
                else self.interval
            )
            self.last_attempt_finished = asyncio.get_running_loop().time()
            self.next_attempt = self.last_attempt_finished + delay
            self.runtime.task = None
