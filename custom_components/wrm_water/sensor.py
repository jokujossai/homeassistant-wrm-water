"""Sensor platform for WRM Water Consumption."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WrmCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one meter reading sensor per discovered meter."""
    coordinator: WrmCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WRMMeterSensor(coordinator, meter) for meter in coordinator.meters
    )


class WRMMeterSensor(CoordinatorEntity[WrmCoordinator], SensorEntity):
    """Cumulative water meter reading (m3) as a total_increasing sensor."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_meter"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: WrmCoordinator, meter: dict) -> None:
        super().__init__(coordinator)
        self._serial = meter["serial"]
        location_name = meter.get("location_name")
        self._attr_unique_id = f"{self._serial}_reading"
        device_name = f"Water meter {self._serial}"
        if location_name:
            device_name += f" ({location_name})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer="WRM Systems",
            model="WRM water meter",
            name=device_name,
        )

    @property
    def _reading(self) -> dict:
        data = self.coordinator.data or {}
        return data.get(self._serial) or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._reading)

    @property
    def native_value(self) -> float | None:
        return self._reading.get("reading_m3")

    @property
    def extra_state_attributes(self) -> dict:
        reading = self._reading
        return {
            "timestamp": reading.get("timestamp"),
            "serial_number": self._serial,
            "location_name": reading.get("location_name"),
            "last_interval_consumption_m3": reading.get("consumption_m3"),
        }
