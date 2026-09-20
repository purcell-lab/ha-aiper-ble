"""Local S1 diagnostics and explicitly enabled bounded telemetry polling."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_RESULT
from .coordinator import AiperCoordinator
from .probe import Target, open_bluez
from .probe import probe as run_probe
from .protocol import Listen, Query, preview

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class Runtime:
    """Volatile results and one in-flight discovery per config entry."""

    target: Target
    last_result: dict[str, Any] = field(default_factory=lambda: {"status": "never_run"})
    task: asyncio.Task | None = None
    closing: bool = False
    coordinator: AiperCoordinator | None = None

    async def async_close(self):
        """Cancel an in-flight probe and allow its disconnect cleanup to finish."""
        self.closing = True
        if self.coordinator:
            await self.coordinator.async_shutdown()
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError, HomeAssistantError):
                await self.task


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register actions once, even when no entry is loaded."""

    async def handle(call: ServiceCall):
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or not isinstance(getattr(entry, "runtime_data", None), Runtime)
        ):
            raise ServiceValidationError("Select a loaded Aiper BLE Diagnostics entry.")
        runtime = entry.runtime_data
        if runtime.closing:
            raise ServiceValidationError("Integration is unloading.")
        if runtime.task and not runtime.task.done():
            raise ServiceValidationError("A probe is already running.")
        if call.service == "poll_now":
            if call.data.get("confirm_app_closed") is not True:
                raise ServiceValidationError(
                    "Close the Aiper app and other BLE clients, then "
                    "confirm_app_closed: true."
                )
            if runtime.coordinator is None:
                raise ServiceValidationError("Polling coordinator is unavailable.")
            result = await runtime.coordinator.async_poll_now()
            return result if call.return_response else None
        query = (
            Query(
                call.data["checksum_mode"],
                call.data["write_mode"],
                call.data.get("confirm_legacy_probe", False),
                call.data["query_type"],
            )
            if call.service in {"protocol_preview", "query_once"}
            else None
        )
        if call.service == "protocol_preview":
            return preview(query)
        if runtime.target.adapter_path is None:
            raise ServiceValidationError(
                "This legacy diagnostic action requires a local adapter entry. "
                "HA-managed polling supports this proxy-only entry."
            )
        listen = call.service == "listen_once"
        if listen:
            query = Listen(call.data.get("confirm_legacy_probe", False))
        read = call.service == "read_once"
        connect = call.service in {"discover", "read_once", "query_once", "listen_once"}
        if connect and call.data.get("confirm_app_closed") is not True:
            raise ServiceValidationError(
                "Close the Aiper app and other BLE clients, then confirm_app_closed: true."
            )
        if read and call.data.get("confirm_read_only") is not True:
            raise ServiceValidationError(
                "Authorise one raw characteristic read with confirm_read_only: true."
            )
        if listen and call.data.get("confirm_notifications") is not True:
            raise ServiceValidationError(
                "Authorise notification subscription with confirm_notifications."
            )
        if isinstance(query, Query) and (
            call.data.get("confirm_query_write") is not True
            or call.data.get("confirm_notifications") is not True
        ):
            raise ServiceValidationError(
                "Authorise notification subscription and one selected status "
                "command with confirm_notifications and confirm_query_write."
            )
        # No await before this assignment: concurrent calls cannot both pass the guard.
        runtime.task = asyncio.current_task()
        report = {
            "status": "running",
            "started_utc": dt_util.utcnow().isoformat(),
            "mode": (
                "connect_listen_only_disconnect"
                if listen
                else f"connect_notify_{query.query_type.lower()}_disconnect"
                if query is not None
                else "connect_read_once_disconnect"
                if read
                else "connect_discover_disconnect"
                if connect
                else "preflight_only"
            ),
        }
        runtime.last_result = report
        async_dispatcher_send(hass, SIGNAL_RESULT, entry.entry_id)
        try:
            options = {"allow_read": read}
            if query is not None:
                options["query"] = query
            async with open_bluez(runtime.target, **options) as api:
                await run_probe(
                    api, runtime.target, report, connect, read=read, query=query
                )
        except asyncio.CancelledError:
            report["status"] = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 - preserve diagnostic status for bus failures
            report["status"] = "failed"
            # Error text can contain device identifiers. Keep only the type here.
            report["error"] = type(exc).__name__
            report["error_code"] = "transport_setup_failed"
            report["failure_stage"] = "transport_setup"
        finally:
            report["finished_utc"] = dt_util.utcnow().isoformat()
            if report["status"] == "cleanup_requires_review" and runtime.coordinator:
                coordinator = runtime.coordinator
                coordinator.suspended = True
                coordinator.status = "suspended"
                coordinator.error_code = "manual_probe_cleanup_requires_review"
                coordinator.update_interval = None
                coordinator.async_set_update_error(UpdateFailed(coordinator.error_code))
            runtime.task = None
            async_dispatcher_send(hass, SIGNAL_RESULT, entry.entry_id)
        if report["status"] not in {
            "preflight_passed_no_connection",
            "discovery_complete",
            "read_complete",
            "query_complete",
            "listen_complete",
        }:
            raise HomeAssistantError(
                f"Probe stopped ({report['status']}). Download integration diagnostics."
            )
        return report if call.return_response else None

    schema = {vol.Required("entry_id"): str}
    hass.services.async_register(
        DOMAIN,
        "poll_now",
        handle,
        schema=vol.Schema({**schema, vol.Required("confirm_app_closed"): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    query_schema = {
        **schema,
        vol.Optional("query_type", default="OpInfo"): vol.In(["OpInfo", "S1_INFO"]),
        vol.Required("checksum_mode"): vol.In(["include_empty_crc", "omit_empty_crc"]),
        vol.Required("write_mode"): vol.In(["command", "request"]),
    }
    hass.services.async_register(
        DOMAIN,
        "protocol_preview",
        handle,
        schema=vol.Schema(query_schema),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "query_once",
        handle,
        schema=vol.Schema(
            {
                **query_schema,
                vol.Required("confirm_app_closed"): cv.boolean,
                vol.Required("confirm_query_write"): cv.boolean,
                vol.Required("confirm_notifications"): cv.boolean,
                vol.Optional("confirm_legacy_probe", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "listen_once",
        handle,
        schema=vol.Schema(
            {
                **schema,
                vol.Required("confirm_app_closed"): cv.boolean,
                vol.Required("confirm_notifications"): cv.boolean,
                vol.Optional("confirm_legacy_probe", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "preflight",
        handle,
        schema=vol.Schema(schema),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "read_once",
        handle,
        schema=vol.Schema(
            {
                **schema,
                vol.Required("confirm_app_closed"): cv.boolean,
                vol.Required("confirm_read_only"): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "discover",
        handle,
        schema=vol.Schema(
            {
                **schema,
                vol.Required("confirm_app_closed"): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Keep existing entries offline unless recurring polling is explicitly enabled."""
    entry.runtime_data = Runtime(Target(**entry.data))
    coordinator = entry.runtime_data.coordinator = AiperCoordinator(
        hass, entry, entry.runtime_data
    )
    if coordinator.enabled:
        # An offline robot must not prevent loading the diagnostic actions.
        await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))

    async def stop(_event):
        await entry.runtime_data.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Stop old polling and apply explicitly saved options through a reload."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Release an in-flight connection before unloading."""
    await entry.runtime_data.async_close()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        entry.runtime_data.closing = False
    return unloaded
