"""Integration tests: entry setup, unload, reauth and service registration."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigEntryState

from custom_components.wrm_water.const import (
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)

SERIAL = "12345678"


async def test_setup_and_unload(hass, config_entry, portal, setup_entry):
    await setup_entry(config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)

    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    assert SERIAL in coordinator.data
    assert coordinator.data[SERIAL]["reading_m3"] == 100.500

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    # Last entry gone -> the service is deregistered.
    assert not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)


async def test_expired_session_starts_reauth(
    hass, config_entry, portal, setup_entry, mocks
):
    # The portal serves the login page -> SessionExpired -> reauth flow.
    portal(dashboard=mocks.LOGIN_PAGE)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == "reauth" for f in flows)


async def test_failed_poll_marks_sensor_unavailable(
    hass, config_entry, portal, setup_entry, aioclient_mock, mocks
):
    from homeassistant.helpers import entity_registry as er

    await setup_entry(config_entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_reading"
    )
    assert hass.states.get(entity_id).state == "100.5"

    # Portal starts returning empty readings -> WrmError -> UpdateFailed.
    portal(rows=[])
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"
