"""CRC-verified S1 telemetry and separate diagnostic result indicators."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import slugify

from .const import DOMAIN, SIGNAL_RESULT
from .datapoints import DEFAULT_ENABLED, SENSOR_NAMES
from .device import device_info
from .entity import AiperEntity
from .s1_states import INFO_STATES, info_state


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            ResultSensor(entry),
            *[TelemetrySensor(entry, key) for key in SENSOR_NAMES],
            PollingStatusSensor(entry),
            OperatingStateSensor(entry),
        ]
    )


class TelemetrySensor(AiperEntity, SensorEntity):
    """Never publish unverified data or claim a temperature sensor location."""

    def __init__(self, entry, key):
        super().__init__(entry, key)
        self._attr_entity_registry_enabled_default = key in DEFAULT_ENABLED
        # Fixed IDs from the historical names; HA still honours user-renamed IDs.
        self.entity_id = f"sensor.{slugify(SENSOR_NAMES[key])}"
        if key == "temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "battery":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "last_success":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def available(self):
        return (
            self.polling_active
            # Measured values disappear as soon as a cycle fails, so no stale
            # reading is ever presented as current. The last-successful-poll
            # timestamp is staleness evidence rather than a measurement, and is
            # most needed while polling is failing, so it stays visible on the
            # retained value until polling is disabled or the entry unloads.
            and (self.key == "last_success" or super().available)
            and self.coordinator.data is not None
            and self.coordinator.data.get(self.key) is not None
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self.key)


class OperatingStateSensor(AiperEntity, SensorEntity):
    """INFO-derived enum, not a claim to reproduce the app's cloud overlays."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(INFO_STATES)

    def __init__(self, entry):
        super().__init__(entry, "operating_state")
        self.entity_id = "sensor.aiper_ble_operating_state"

    @property
    def available(self):
        return (
            self.polling_active
            and super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get("info_status_raw") is not None
        )

    @property
    def native_value(self):
        return info_state(self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        return {
            "evidence": "Android_3.6.1_S1StatusInfo_INFO_subset",
            "warning_connectivity_and_external_ota_overlays": "not_inferred",
        }


class PollingStatusSensor(AiperEntity, SensorEntity):
    """Explain disabled, failed or suspended polling even with no telemetry."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth"

    def __init__(self, entry):
        super().__init__(entry, "polling_status")
        self.entity_id = "sensor.aiper_ble_polling_status"

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.status

    @property
    def extra_state_attributes(self):
        return {
            "error_code": self.coordinator.error_code,
            "consecutive_failures": self.coordinator.failures,
            "configured_interval_seconds": self.coordinator.interval,
            "configured_queries": list(self.coordinator.poll_queries),
            "temperature_sensor_location": "unverified",
            "solar_status_mapping": "unverified",
            "wifi_rssi_interpretation": "unverified",
            "battery_source": "INFO_field_2_app_battLevel_validated_0_to_100",
            "info_status_mode_mapping": "Android_3.6.1_S1StatusInfo_INFO_subset",
            "warning_code_mapping": "WARN_signed_int64_fault_meanings_unverified",
            "optional_machine_fields": "unverified_entities_retired",
        }


class ResultSensor(SensorEntity):
    """Expose result status only; keep full inventories out of recorder."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "discovery_result"
    _attr_icon = "mdi:bluetooth"

    def __init__(self, entry):
        self.entry = entry
        self._attr_device_info = device_info(entry)
        self._attr_unique_id = f"{entry.entry_id}_discovery_result"
        self.entity_id = "sensor.aiper_ble_discovery_result"

    @property
    def native_value(self):
        return self.entry.runtime_data.last_result["status"]

    @property
    def extra_state_attributes(self):
        result = self.entry.runtime_data.last_result
        return {
            "entry_id": self.entry.entry_id,
            "integration": DOMAIN,
            **{
                key: result[key]
                for key in (
                    "started_utc",
                    "mode",
                    "cleanup",
                    "expected_service_found",
                    "expected_characteristic_found_in_expected_service",
                    "error_code",
                    "failure_stage",
                    "cleanup_error_code",
                    "notification_cleanup",
                )
                if key in result
            },
        }

    async def async_added_to_hass(self):
        @callback
        def updated(entry_id):
            if entry_id == self.entry.entry_id:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RESULT, updated)
        )
