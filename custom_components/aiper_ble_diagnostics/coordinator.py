"""Opt-in S1 polling using HA-managed Bluetooth connections and active proxies."""

import asyncio
import json
import logging
import math
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .bluetooth_transport import query_once
from .const import DOMAIN
from .datapoints import opinfo_values, timezone
from .protocol import ProtocolError, Query, crc16, query_telemetry

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 300
MAX_INTERVAL = 3600
POLL_SECONDS = 180
POLL_DETAIL_FIELDS = (
    "query_type",
    "transport",
    "status",
    "phase",
    "failure_stage",
    "error_code",
    "error_category",
    "write_attempts",
    "notification_count",
    "received_bytes",
    "cleanup",
    "notification_cleanup",
    "cleanup_error_category",
    "notification_cleanup_error_category",
)
SUSPEND_CODES = {
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
    if query.query_type in {"INFO", "WARN"}:
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
        self.status = "waiting" if self.enabled else "disabled"
        self.error_code = None
        self.suspended = False
        self.failures = 0
        self.next_attempt = 0.0
        self.last_attempt_finished = None
        self.last_poll_details = {}
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval) if self.enabled else None,
        )
        self.last_update_success = False

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
        """Run one existing guarded cycle, not a queued or concurrent probe.

        An explicit operator request can shorten failure backoff, but never the
        configured normal interval measured from completion of the last attempt.
        No await precedes _async_update_data's existing runtime task reservation.
        """
        if not self.enabled or self.runtime.closing:
            raise ServiceValidationError("Enable authorised polling first.")
        if self.suspended:
            raise ServiceValidationError("Polling suspended; review diagnostics first.")
        if self.runtime.task and not self.runtime.task.done():
            raise ServiceValidationError("A poll or probe is already running.")
        now = asyncio.get_running_loop().time()
        if self.last_attempt_finished is not None:
            remaining = self.interval - (now - self.last_attempt_finished)
            if remaining > 0:
                raise ServiceValidationError(
                    f"Polling cooldown: retry in {math.ceil(remaining)} seconds."
                )
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
        try:
            values = {}
            # One connection per fixed request. HA chooses an available local
            # adapter or active proxy; requests are never replayed.
            async with asyncio.timeout(POLL_SECONDS):
                for query_type in ("S1_INFO", "OpInfo", "INFO", "WARN"):
                    if self.runtime.closing:
                        raise asyncio.CancelledError
                    query = Query(
                        "omit_empty_crc", "request", self.allow_missing, query_type
                    )
                    report = {"query_type": query_type}
                    await query_once(self.hass, self.runtime.target, report, query)
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
            # Start the cooldown after cleanup, not before a slow connection.
            delay = (
                self.update_interval.total_seconds()
                if self.update_interval
                else self.interval
            )
            self.last_attempt_finished = asyncio.get_running_loop().time()
            self.next_attempt = self.last_attempt_finished + delay
            self.runtime.task = None
