"""Download cached results only; downloading never initiates Bluetooth activity."""

from dataclasses import asdict

from homeassistant.components.diagnostics import async_redact_data

from .const import REDACT_KEYS


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    return async_redact_data(
        {
            "target_config": asdict(runtime.target),
            "last_result": runtime.last_result,
            "polling": {
                "enabled": runtime.coordinator.enabled,
                "status": runtime.coordinator.status,
                "error_code": runtime.coordinator.error_code,
                "interval_seconds": runtime.coordinator.interval,
                "consecutive_failures": runtime.coordinator.failures,
                "allow_missing_advertisement": runtime.coordinator.allow_missing,
                "transport": "ha_bluetooth",
            },
            "scope": "HA Bluetooth/proxy polling; legacy diagnostics local only; no control",
        },
        REDACT_KEYS,
    )
