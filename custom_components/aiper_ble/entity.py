"""Shared entity base: device grouping, unique IDs, translated names."""

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device import device_info

if TYPE_CHECKING:
    from .coordinator import AiperCoordinator  # noqa: F401 - forward reference


class AiperEntity(CoordinatorEntity["AiperCoordinator"]):
    """Entities are named by translation key under the shared device name.

    Entity IDs stay fixed by each platform so recorder history survives the
    move to translated names; only friendly names change.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self.key = key
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key

    @property
    def polling_active(self) -> bool:
        """Polling is enabled and the entry is not unloading."""
        coordinator: Any = self.coordinator
        return bool(coordinator.enabled and not coordinator.runtime.closing)
