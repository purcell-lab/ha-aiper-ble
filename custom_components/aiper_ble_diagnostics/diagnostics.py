"""Download cached results only; downloading never initiates Bluetooth activity."""

from dataclasses import asdict

from homeassistant.components.diagnostics import async_redact_data

from .const import REDACT_KEYS
from .datapoints import SENSOR_NAMES


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    return async_redact_data(
        {
            "target_config": asdict(runtime.target),
            "last_result": runtime.last_result,
            "last_control": runtime.coordinator.last_control_result,
            "polling": {
                "enabled": runtime.coordinator.enabled,
                "status": runtime.coordinator.status,
                "error_code": runtime.coordinator.error_code,
                "interval_seconds": runtime.coordinator.interval,
                "consecutive_failures": runtime.coordinator.failures,
                "allow_missing_advertisement": runtime.coordinator.allow_missing,
                "transport": runtime.coordinator.transport,
                "configured_queries": list(runtime.coordinator.poll_queries),
                "last_poll_details": dict(runtime.coordinator.last_poll_details),
                "last_poll_queries": [
                    dict(item) for item in runtime.coordinator.last_poll_queries
                ],
                "field_evidence": {
                    "current": runtime.coordinator.last_update_success,
                    "present_in_last_successful_cycle": sorted(
                        key
                        for key in SENSOR_NAMES
                        if (runtime.coordinator.data or {}).get(key) is not None
                        and key != "last_success"
                    ),
                },
            },
            "scope": "Selected BlueZ or HA Bluetooth; explicit guarded S1 start/stop actions",
        },
        REDACT_KEYS,
    )
