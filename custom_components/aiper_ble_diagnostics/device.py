"""Stable, integration-scoped identity without merging unrelated cloud devices."""

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import DOMAIN


def device_info(entry):
    """Group entities without changing their existing unique IDs or names.

    The Bluetooth connection lets HA's device page show its Bluetooth section
    (last seen via which adapter or proxy, signal, troubleshooting link). It
    records the address in the device registry only; downloaded integration
    diagnostics keep redacting it.
    """
    target = entry.runtime_data.target
    return DeviceInfo(
        identifiers={(DOMAIN, target.address.upper())},
        connections={(CONNECTION_BLUETOOTH, target.address.upper())},
        name="Aiper Surfer S1 (BLE)",
        manufacturer="Aiper",
        model="Surfer S1",
    )
