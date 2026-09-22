"""Buttons: an immediate poll and the option-gated start/stop controls."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .controls import async_entity_control
from .entity import AiperEntity

# Every press uses the radio through the shared poll/control mutex.
PARALLEL_UPDATES = 1
BUTTONS = ("poll_now", "start_cleaning", "stop_cleaning")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([AiperButton(entry, key) for key in BUTTONS])


class AiperButton(AiperEntity, ButtonEntity):
    """Same guarded paths as the actions and the vacuum entity, nothing optimistic.

    Poll now runs one full cycle at once, clearing failure backoff. Start and
    stop run the control sequence (preflight, one setter, one INFO readback,
    no retry) and are refused until the vacuum controls option is enabled.
    """

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self.entity_id = f"button.aiper_ble_{key}"

    @property
    def available(self) -> bool:
        return self.polling_active and not self.coordinator.suspended

    async def async_press(self) -> None:
        if self.key == "poll_now":
            await self.coordinator.async_poll_now()
            return
        await async_entity_control(self.entry, self.coordinator, self.key)
