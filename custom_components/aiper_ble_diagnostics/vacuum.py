"""Vacuum entity over the same guarded, verified S1 start/stop controls."""

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .controls import async_control
from .device import device_info
from .s1_states import info_state

VACUUM_CONTROLS_OPTION = "confirm_vacuum_controls"

# Home Assistant has no charging activity; this robot has no dock, so "docked"
# stands for "on its wall charger". Sunward is the app's intermittent cleaning
# mode. Updating and unknown codes give no activity rather than a guess.
ACTIVITIES = {
    "working": VacuumActivity.CLEANING,
    "sunward": VacuumActivity.CLEANING,
    "standby": VacuumActivity.IDLE,
    "charging": VacuumActivity.DOCKED,
    "fully_charged": VacuumActivity.DOCKED,
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AiperVacuum(entry)])


class AiperVacuum(CoordinatorEntity, StateVacuumEntity):
    """State from CRC-verified INFO only; controls run the existing bounded path.

    Start and stop are the same exchanges as the start_cleaning and
    stop_cleaning actions: preflight, one setter, one INFO readback, no retry.
    The per-call safety confirmations those actions take are replaced by the
    entry option that enables vacuum controls, which the owner sets once.
    """

    _attr_name = "Aiper Surfer S1"
    _attr_supported_features = (
        VacuumEntityFeature.START | VacuumEntityFeature.STOP | VacuumEntityFeature.STATE
    )

    def __init__(self, entry):
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.entry_id}_vacuum"
        self.entity_id = "vacuum.aiper_surfer_s1"

    def _control_readback(self):
        """A verified readback stands in until the next poll replaces it."""
        result = self.coordinator.last_control_result
        if (
            self.coordinator.status == "awaiting_poll_after_control"
            and result.get("state_verified")
            and result.get("observed_state") is not None
        ):
            return result
        return None

    @property
    def available(self):
        coordinator = self.coordinator
        if (
            not coordinator.enabled
            or coordinator.runtime.closing
            or coordinator.suspended
        ):
            return False
        if self._control_readback() is not None:
            return True
        return (
            super().available
            and coordinator.data is not None
            and coordinator.data.get("info_status_raw") is not None
        )

    @property
    def activity(self):
        readback = self._control_readback()
        if readback is not None:
            return ACTIVITIES.get(readback["observed_state"])
        return ACTIVITIES.get(info_state(self.coordinator.data or {}))

    @property
    def extra_state_attributes(self):
        readback = self._control_readback()
        return {
            "operating_state": (
                readback["observed_state"]
                if readback is not None
                else info_state(self.coordinator.data or {})
            ),
            "state_source": "control_readback" if readback else "polling_cycle",
            "controls_enabled": self.entry.options.get(VACUUM_CONTROLS_OPTION) is True,
        }

    async def _run(self, action):
        if self.entry.options.get(VACUUM_CONTROLS_OPTION) is not True:
            raise ServiceValidationError(
                "Vacuum controls are disabled. Enable them in the integration's "
                "options once the robot is in water, unplugged and nobody is in "
                "the pool, and keep the app closed."
            )
        await async_control(self.coordinator, action)

    async def async_start(self):
        await self._run("start_cleaning")

    async def async_stop(self, **kwargs):
        await self._run("stop_cleaning")
