"""Vacuum entity over the same guarded, verified S1 start/stop controls."""

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.util import dt as dt_util

from .controls import async_control
from .entity import AiperEntity
from .errors import validation
from .s1_states import info_state

VACUUM_CONTROLS_OPTION = "confirm_vacuum_controls"
# Start and stop send BLE commands through the shared poll/control mutex; one
# at a time is the honest concurrency.
PARALLEL_UPDATES = 1

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


class AiperVacuum(AiperEntity, StateVacuumEntity):
    """State from CRC-verified INFO only; controls run the existing bounded path.

    Start and stop are the same exchanges as the start_cleaning and
    stop_cleaning actions: preflight, one setter, one INFO readback, no retry.
    The per-call safety confirmations those actions take are replaced by the
    entry option that enables vacuum controls, which the owner sets once.
    """

    # The device's primary entity carries the device name itself.
    _attr_name = None
    _attr_supported_features = (
        VacuumEntityFeature.START | VacuumEntityFeature.STOP | VacuumEntityFeature.STATE
    )

    def __init__(self, entry):
        super().__init__(entry, "vacuum")
        self._attr_translation_key = None
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
        if not self.polling_active or coordinator.suspended:
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
        data = self.coordinator.data or {}
        if data.get("warning_code_raw") not in (None, 0):
            # A raw fault code is reported as an error; its meaning stays raw.
            return VacuumActivity.ERROR
        return ACTIVITIES.get(info_state(data))

    def _last_route(self):
        """Backend and signal of the last query, from cached diagnostics."""
        queries = self.coordinator.last_poll_queries
        if not queries:
            return None
        diagnostics = queries[-1].get("transport_diagnostics") or {}
        route = {"backend": diagnostics.get("backend")}
        snapshot = (diagnostics.get("route_snapshots") or {}).get("before_connect")
        for candidate in (snapshot or {}).get("routes", []):
            if candidate.get("route_id") == diagnostics.get("selected_route"):
                route["scanner_type"] = candidate.get("scanner_type")
                route["rssi_dbm"] = candidate.get("rssi_dbm")
        return route

    @property
    def extra_state_attributes(self):
        coordinator = self.coordinator
        data = coordinator.data or {}
        readback = self._control_readback()
        control = coordinator.last_control_result
        last_success = data.get("last_success")
        return {
            "operating_state": (
                readback["observed_state"] if readback is not None else info_state(data)
            ),
            "state_source": "control_readback" if readback else "polling_cycle",
            "controls_enabled": self.entry.options.get(VACUUM_CONTROLS_OPTION) is True,
            "battery": data.get("battery"),
            "temperature_c": data.get("temperature"),
            "warning_code_raw": data.get("warning_code_raw"),
            "minutes_counter_raw": data.get("info_field_5_raw"),
            # Local time, per the owner's reporting rule; the sensor keeps UTC.
            "last_successful_poll": (
                dt_util.as_local(last_success).isoformat()
                if last_success is not None
                else None
            ),
            "polling_status": coordinator.status,
            "consecutive_failures": coordinator.failures,
            "last_route": self._last_route(),
            "last_control": (
                {
                    key: control.get(key)
                    for key in ("action", "status", "observed_state", "finished_at")
                }
                if control.get("status") != "never_run"
                else None
            ),
        }

    async def _run(self, action):
        if self.entry.options.get(VACUUM_CONTROLS_OPTION) is not True:
            raise validation("vacuum_controls_disabled")
        await async_control(self.coordinator, action)

    async def async_start(self):
        await self._run("start_cleaning")

    async def async_stop(self, **kwargs):
        await self._run("stop_cleaning")
