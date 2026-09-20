"""Stable, integration-scoped identity without merging unrelated cloud devices."""

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def device_info(entry):
    """Group entities without changing their existing unique IDs or names."""
    target = entry.runtime_data.target
    return DeviceInfo(
        identifiers={(DOMAIN, target.address.upper())},
        name="Aiper Surfer S1 (BLE)",
        manufacturer="Aiper",
        model="Surfer S1",
    )
