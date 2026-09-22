"""Explicit bounded controls sharing the polling/probe mutex and transport."""

import asyncio

from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .coordinator import POLL_DETAIL_FIELDS, SUSPEND_CODES, verified_values
from .errors import failure, validation
from .protocol import Control, ProtocolError, Query
from .s1_states import info_state

CONTROL_SERVICES = ("start_cleaning", "stop_cleaning")
CONTROL_SECONDS = 180


async def async_control(coordinator, action):
    """Never queue, retry, toggle, change routes or optimistically publish state."""
    # Resolve at call time, as the coordinator does for both supported transports.
    from .coordinator import local_query_once, query_once

    runtime = coordinator.runtime
    if action not in CONTROL_SERVICES:
        raise validation("select_control")
    if runtime.closing or coordinator.suspended:
        raise validation("control_closing_or_suspended")
    if runtime.task and not runtime.task.done():
        raise validation("control_busy")
    # Reserve before the first await. Stop does not wait through the normal
    # five-minute telemetry cooldown, but cannot interrupt an owned BLE session.
    runtime.task = asyncio.current_task()
    reports = []
    result = {
        "action": action,
        "status": "not_sent",
        "acknowledged": False,
        "state_verified": False,
        "motion_may_have_changed": False,
        "started_at": dt_util.now().isoformat(),
    }
    execute = local_query_once if coordinator.use_local_adapter else query_once

    async def exchange(request):
        report = {"query_type": request.query_type}
        reports.append(report)
        try:
            await execute(coordinator.hass, runtime.target, report, request)
        finally:
            if isinstance(request, Control):
                result["motion_may_have_changed"] = report.get("write_attempts", 0) > 0
            if (
                report.get("status") == "cleanup_requires_review"
                or report.get("error_code") in SUSPEND_CODES
                or report.get("cleanup")
                not in {"disconnected_confirmed", "not_connected"}
                or report.get("notification_cleanup")
                in {"stop_unconfirmed", "ownership_uncertain"}
            ):
                coordinator.suspended = True
                coordinator.status = "suspended"
                coordinator.error_code = "control_cleanup_or_protocol_requires_review"
                coordinator.update_interval = None
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if (
            coordinator.suspended
            or report.get("status") != "query_complete"
            or report.get("cleanup") != "disconnected_confirmed"
            or report.get("notification_cleanup") != "stop_confirmed"
        ):
            raise ProtocolError("control_exchange_incomplete")
        return verified_values(report.get("protocol_response"), request)

    def query(name):
        return Query("omit_empty_crc", "request", coordinator.allow_missing, name)

    try:
        async with asyncio.timeout(CONTROL_SECONDS):
            if action == "start_cleaning":
                # Fresh reads, not cached state. Unknown faults conservatively
                # block start; warning decoding and OTA cloud state are absent.
                before = await exchange(query("INFO"))
                if before["info_status_raw"] not in {0, 1} or info_state(
                    before
                ) not in {"standby", "working"}:
                    raise ProtocolError("start_blocked_by_operating_state")
                warning = await exchange(query("WARN"))
                if warning.get("warning_code_raw") != 0:
                    raise ProtocolError("start_blocked_by_warning")
            await exchange(Control(action, coordinator.allow_missing))
            result["acknowledged"] = True
            after = await exchange(query("INFO"))
            result["observed_state"] = info_state(after)
            result["info_status_raw"] = after["info_status_raw"]
            result["info_mode_raw"] = after["info_mode_raw"]
            # Strict evidence: a stop needs status 0, not merely the UI's
            # standby fallback for mode 8. No polling loop or automatic retry.
            result["state_verified"] = (
                after["info_status_raw"] == 1 and info_state(after) == "working"
                if action == "start_cleaning"
                else after["info_status_raw"] == 0 and info_state(after) == "standby"
            )
            result["status"] = (
                "state_verified"
                if result["state_verified"]
                else "acknowledged_state_unconfirmed"
            )
    except asyncio.CancelledError:
        result["status"] = "interrupted"
        raise
    except Exception as exc:  # noqa: BLE001 - never expose backend identifiers
        result["status"] = (
            "outcome_unknown" if result["motion_may_have_changed"] else "not_sent"
        )
        result["error_code"] = (
            str(exc) if isinstance(exc, ProtocolError) else "control_transport_error"
        )
    finally:
        result["finished_at"] = dt_util.now().isoformat()
        result["details"] = [
            {key: item[key] for key in POLL_DETAIL_FIELDS if key in item}
            for item in reports
        ]
        coordinator.last_control_result = result
        runtime.last_result = {"mode": "cleaning_control", **result}
        coordinator.last_attempt_finished = asyncio.get_running_loop().time()
        coordinator.next_attempt = (
            coordinator.last_attempt_finished + coordinator.interval
        )
        runtime.task = None
        if result["motion_may_have_changed"] or coordinator.suspended:
            # Retained poll data predates the command; do not present it as current.
            # Readback is returned separately, never merged into an atomic cycle.
            if not coordinator.suspended:
                coordinator.status = "awaiting_poll_after_control"
                coordinator.error_code = (
                    None if result["state_verified"] else "control_state_unconfirmed"
                )
            coordinator._publish_manual_error(
                UpdateFailed("Refresh telemetry after control.")
            )
    if not result["state_verified"]:
        raise failure("control_unconfirmed", status=result["status"])
    return dict(result)
