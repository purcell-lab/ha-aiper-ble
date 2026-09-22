"""Manual setup using HA shared discovery, with legacy local metadata fallback."""

import asyncio
from dataclasses import asdict

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN
from .coordinator import DEFAULT_INTERVAL, MAX_INTERVAL, MIN_INTERVAL
from .probe import Target, candidates, open_bluez


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an Aiper identity without connecting during setup."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self):
        self._discovered = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        """Offer a robot seen by HA's shared scanners; still no connection."""
        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        # A configured robot that now advertises a different name keeps its
        # entry and picks up the new name (discovery-update-info).
        self._abort_if_unique_id_configured(updates={"name": discovery_info.name})
        self._discovered = Target(address, discovery_info.name)
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None):
        """Confirm the discovered robot before creating the entry."""
        if user_input is not None:
            return self.async_create_entry(
                title="Aiper BLE", data=asdict(self._discovered)
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovered.name},
        )

    async def async_step_reconfigure(self, user_input=None):
        """Point an existing entry at a different robot, keeping its options."""
        return await self.async_step_user(user_input, reconfigure=True)

    async def async_step_user(self, user_input=None, reconfigure=False):
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
            elif reconfigure:
                entry = self._get_reconfigure_entry()
                await self.async_set_unique_id(target.address)
                if target.address != entry.unique_id:
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(
                    entry, data=asdict(target), unique_id=target.address
                )
            else:
                await self.async_set_unique_id(target.address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Aiper BLE", data=asdict(target))
        options = [
            selector.SelectOptionDict(
                value=path,
                label=f"{target.name} ({target.address})",
            )
            for path, target in available.items()
        ]
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
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
                    vol.Optional(
                        "confirm_vacuum_controls",
                        default=current.get("confirm_vacuum_controls", False),
                    ): bool,
                }
            ),
            errors=errors,
        )
