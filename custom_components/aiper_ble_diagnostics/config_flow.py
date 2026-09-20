"""Manual setup using HA shared discovery, with legacy local metadata fallback."""

import asyncio
from dataclasses import asdict

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN
from .coordinator import DEFAULT_INTERVAL, MAX_INTERVAL, MIN_INTERVAL
from .probe import Target, candidates, open_bluez


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an Aiper identity without connecting during setup."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def async_step_user(self, user_input=None):
        """Select cached Aiper metadata; never start a scanner or connection."""
        available = {}
        try:
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            ):
                if info.name.startswith(("Aiper-Surfer S1-", "Aiper_Surfer S1_")):
                    target = Target(info.address.upper(), info.name)
                    available[target.address] = target
        except Exception:  # noqa: BLE001 - unavailable shared manager
            return self.async_abort(reason="local_bluetooth_unavailable")
        if not available:
            try:
                async with open_bluez() as api:
                    available = candidates(await asyncio.wait_for(api.objects(), 5))
            except Exception:  # noqa: BLE001 - proxy-only hosts need no BlueZ
                return self.async_abort(reason="no_local_aiper")
        if not available:
            return self.async_abort(reason="no_local_aiper")
        errors = {}
        if user_input is not None:
            target = available.get(user_input.get("device"))
            if target is None:
                errors["base"] = "device_changed"
            else:
                await self.async_set_unique_id(target.address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Aiper BLE Diagnostics", data=asdict(target)
                )
        options = [
            selector.SelectOptionDict(
                value=path,
                label=f"{target.name} ({target.address})",
            )
            for path, target in available.items()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    ),
                }
            ),
            errors=errors,
        )


class OptionsFlow(config_entries.OptionsFlow):
    """Persistent authorisation is separate from past one-shot confirmations."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("use_local_adapter") and not (
                self.config_entry.data.get("adapter_path")
                and self.config_entry.data.get("adapter_address")
            ):
                errors["base"] = "local_adapter_required"
            elif (
                user_input["polling_enabled"]
                and not user_input["confirm_exclusive_access"]
            ):
                errors["base"] = "exclusive_access_required"
            else:
                return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "polling_enabled", default=current.get("polling_enabled", False)
                    ): bool,
                    vol.Required(
                        "poll_interval",
                        default=current.get("poll_interval", DEFAULT_INTERVAL),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_INTERVAL, max=MAX_INTERVAL)
                    ),
                    vol.Required(
                        "confirm_exclusive_access",
                        default=current.get("confirm_exclusive_access", False),
                    ): bool,
                    vol.Required(
                        "allow_missing_advertisement",
                        default=current.get("allow_missing_advertisement", False),
                    ): bool,
                    vol.Optional(
                        "use_local_adapter",
                        default=current.get("use_local_adapter", False),
                    ): bool,
                }
            ),
            errors=errors,
        )
