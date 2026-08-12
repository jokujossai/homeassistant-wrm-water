"""The WRM Water Consumption integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized, UnknownUser
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import WrmClient
from .const import (
    ATTR_END,
    ATTR_RESET,
    ATTR_START,
    CONF_BASE_URL,
    DEFAULT_BASE_URL,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import WrmCoordinator
from .statistics_import import async_import_history, async_import_new_readings

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WRM Water Consumption from a config entry."""
    base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
    # Dedicated session so the auth cookie jar is isolated per entry.
    session = async_create_clientsession(hass)
    client = WrmClient(base_url, session)
    client.load_cookies(entry.data.get("cookies"))

    coordinator = WrmCoordinator(hass, entry, client)
    coordinator.session = session
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The first refresh ran before the sensors were registered, so its
    # statistics import was skipped; catch up now that the entities exist.
    async def _initial_import() -> None:
        try:
            await async_import_new_readings(hass, coordinator)
            await coordinator._persist()
        except Exception:  # noqa: BLE001 - import must not break setup
            _LOGGER.exception("Initial statistics import failed")

    entry.async_create_background_task(
        hass, _initial_import(), f"{DOMAIN}_initial_import"
    )

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator and getattr(coordinator, "session", None):
            await coordinator.session.close()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
    return unloaded


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        return

    async def _handle_import_history(call: ServiceCall) -> None:
        reset = call.data.get(ATTR_RESET, False)
        if reset and call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                raise Unauthorized(context=call.context)
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await async_import_history(
                hass,
                coordinator,
                start=call.data.get(ATTR_START),
                end=call.data.get(ATTR_END),
                reset=reset,
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _handle_import_history,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_START): cv.string,
                vol.Optional(ATTR_END): cv.string,
                vol.Optional(ATTR_RESET, default=False): cv.boolean,
            }
        ),
    )
