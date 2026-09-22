"""Opt-in S1 polling over an explicitly selected guarded Bluetooth transport."""

import asyncio
import json
import logging
import math
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .bluetooth_transport import query_once
from .const import DOMAIN
from .datapoints import opinfo_values, timezone
from .errors import validation
from .local_bleak import query_once as local_bleak_query_once
from .local_transport import query_once as local_query_once
from .protocol import Control, ProtocolError, Query, crc16, query_telemetry
from .s1_states import info_state

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 300
MAX_INTERVAL = 3600
POLL_SECONDS = 180
SINGLE_QUERY_SECONDS = 60
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


def verified_values(response, query):
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


class AiperCoordinator(DataUpdateCoordinator):
    """Serialize polls with manual probes; no retries within a polling cycle."""

    def __init__(self, hass, entry, runtime):
        self.runtime = runtime
        self.enabled = (
            entry.options.get("polling_enabled") is True
            and entry.options.get("confirm_exclusive_access") is True
        )
        interval = entry.options.get("poll_interval", DEFAULT_INTERVAL)
        if type(interval) is not int or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            interval = DEFAULT_INTERVAL
        self.interval = interval
        self.allow_missing = entry.options.get("allow_missing_advertisement") is True
        self.use_local_adapter = entry.options.get("use_local_adapter") is True
        self.transport = "local_bluez" if self.use_local_adapter else "ha_bluetooth"
        self.poll_queries = (
            LOCAL_POLL_QUERIES if self.use_local_adapter else HA_POLL_QUERIES
        )
        self.status = "waiting" if self.enabled else "disabled"
        self.error_code = None
        self._suspended = False
        self.failures = 0
        self.next_attempt = 0.0
        self.last_attempt_finished = None
        self.last_poll_details = {}
        self.last_poll_queries = []
        self.last_control_result = {"status": "never_run"}
        # Temperature captured only in cycles where the robot reports working.
        self.water_temperature = None
        self.water_temperature_at = None
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
    def suspended(self):
        return self._suspended

    @suspended.setter
    def suspended(self, value):
        """Suspension is surfaced as a repair issue; reloading the entry fixes it."""
        self._suspended = bool(value)
        issue_id = f"polling_suspended_{self.config_entry.entry_id}"
        if self._suspended:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="polling_suspended",
                translation_placeholders={"error_code": str(self.error_code)},
                data={"entry_id": self.config_entry.entry_id},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _async_refresh_finished(self):
        """Keep diagnostic status current even across consecutive failures."""
        self.async_update_listeners()

    @callback
    def _publish_manual_error(self, error):
        """Publish repeated failures without duplicating first-failure events."""
        was_successful = self.last_update_success
        self.async_set_update_error(error)
        if not was_successful:
            self.async_update_listeners()

    async def async_poll_now(self):
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
        self, query_type, *, local_bleak=False, proxy_entry_id=None
    ):
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
        report = {"query_type": query_type}
        started = dt_util.utcnow().isoformat()
        values = {}
        query = Query("omit_empty_crc", "request", self.allow_missing, query_type)
        try:
            async with asyncio.timeout(
                80 if proxy_entry_id is not None else SINGLE_QUERY_SECONDS
            ):
                execute = local_query_once if self.use_local_adapter else query_once
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
                if asyncio.current_task().cancelling():
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

    async def _async_update_data(self):
        if not self.enabled or self.runtime.closing:
            raise UpdateFailed("Polling disabled")
        if self.suspended:
            raise UpdateFailed("Polling suspended; review diagnostics before reload")
        now = asyncio.get_running_loop().time()
        # HA schedules coordinator ticks with sub-second jitter.
        if now + 1 < self.next_attempt:
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
        report = {}
        reports = []
        try:
            values = {}
            # One connection per fixed request. Local mode pins the saved BlueZ
            # adapter; HA mode selects a route. Never retry or change transport.
            previous_finished = None
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
                    execute = local_query_once if self.use_local_adapter else query_once
                    await execute(self.hass, self.runtime.target, report, query)
                    previous_finished = asyncio.get_running_loop().time()
                    if report.get("status") == "cleanup_requires_review":
                        self.suspended = True
                    # The diagnostic probe records cancellation after cleanup.
                    # Do not swallow unload/deadline cancellation or start query 2.
                    if asyncio.current_task().cancelling():
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
            self.update_interval = (
                None
                if self.suspended
                else timedelta(
                    seconds=min(
                        MAX_INTERVAL, self.interval * 2 ** min(self.failures, 4)
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
            # Start the cooldown after cleanup, not before a slow connection.
            delay = (
                self.update_interval.total_seconds()
                if self.update_interval
                else self.interval
            )
            self.last_attempt_finished = asyncio.get_running_loop().time()
            self.next_attempt = self.last_attempt_finished + delay
            self.runtime.task = None
