"""Integration tests: the meter reading sensor entity."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.wrm_water.const import DOMAIN

SERIAL = "12345678"


async def test_sensor_state_and_attributes(
    hass, config_entry, portal, setup_entry
):
    await setup_entry(config_entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_reading"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state.state == "100.5"
    attrs = state.attributes
    assert attrs["device_class"] == "water"
    assert attrs["state_class"] == "total_increasing"
    assert attrs["unit_of_measurement"] == "m³"
    assert attrs["timestamp"] == "27.6.2026 8:00"
    assert attrs["serial_number"] == SERIAL
    assert attrs["last_interval_consumption_m3"] == pytest.approx(0.1)


async def test_device_created_per_meter(
    hass, config_entry, portal, setup_entry
):
    await setup_entry(config_entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, SERIAL)}
    )
    assert device is not None
    assert device.manufacturer == "WRM Systems"
    assert device.name == f"Water meter {SERIAL}"


async def test_sensor_updates_on_new_reading(
    hass, config_entry, portal, setup_entry, mocks
):
    await setup_entry(config_entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_reading"
    )

    newer = [["28.6.2026 8:00", 101.250, 0.750, 1782622800]]
    portal(rows=newer + mocks.READINGS_JSON)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "101.25"
    assert state.attributes["timestamp"] == "28.6.2026 8:00"
