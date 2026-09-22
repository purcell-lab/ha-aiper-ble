"""Shared entity base: device grouping, unique IDs, translated names."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device import device_info


class AiperEntity(CoordinatorEntity):
    """Entities are named by translation key under the shared device name.

    Entity IDs stay fixed by each platform so recorder history survives the
    move to translated names; only friendly names change.
    """

    _attr_has_entity_name = True

    def __init__(self, entry, key):
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self.key = key
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key

    @property
    def polling_active(self):
        """Polling is enabled and the entry is not unloading."""
        coordinator = self.coordinator
        return coordinator.enabled and not coordinator.runtime.closing
