"""Config and reauth flow for WRM Water Consumption."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    InvalidBaseUrl,
    WrmAuthError,
    WrmClient,
    WrmError,
    validate_base_url,
)
from .const import (
    CONF_BASE_URL,
    CONF_METERS,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
from .identity import entry_unique_id

_LOGGER = logging.getLogger(__name__)


async def _login_and_capture(
    hass: HomeAssistant, base_url: str, email: str, password: str
) -> tuple[list[dict], dict[str, str]]:
    """Log in and return (meters, cookies). Password is not kept.

    Uses a throwaway aiohttp session so the captured cookies are isolated.
    """
    session = async_create_clientsession(hass)
    try:
        client = WrmClient(base_url, session)
        await client.login(email, password)
        meters = await client.discover_meters()
        return meters, client.export_cookies()
    finally:
        await session.close()


class WrmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the WRM config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    def _uid_taken_by_other(self, unique_id: str, entry_id: str) -> bool:
        """True if a *different* entry already holds this unique_id."""
        return any(
            entry.unique_id == unique_id and entry.entry_id != entry_id
            for entry in self._async_current_entries()
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            email = user_input[CONF_EMAIL]
            try:
                base_url = validate_base_url(raw_base_url)
                meters, cookies = await _login_and_capture(
                    self.hass, base_url, email, user_input[CONF_PASSWORD]
                )
            except InvalidBaseUrl:
                errors[CONF_BASE_URL] = "invalid_base_url"
            except WrmAuthError:
                errors["base"] = "invalid_auth"
            except WrmError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during WRM login")
                errors["base"] = "unknown"
            else:
                # unique_id = portal (host + utility path) + account email, so
                # the same email on a *different* utility is a distinct entry,
                # while the same account on the same portal is rejected here.
                await self.async_set_unique_id(
                    entry_unique_id(base_url, email)
                )
                self._abort_if_unique_id_configured()
                # Note: password is intentionally NOT stored.
                return self.async_create_entry(
                    title=f"WRM Water {email}",
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_EMAIL: email,
                        CONF_METERS: meters,
                        CONF_SCAN_INTERVAL_HOURS: user_input.get(
                            CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
                        ),
                        "cookies": cookies,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL_HOURS, default=DEFAULT_SCAN_INTERVAL_HOURS
                ): vol.All(int, vol.Range(min=1, max=24)),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the portal URL / polling interval on an existing entry.

        The email is fixed; the password is required to re-authenticate against
        the — possibly changed — base URL and refresh the stored session cookies
        and meter list. Because the unique_id is derived from base URL + email,
        changing the URL recomputes it (aborting if it would collide with a
        different existing entry).
        """
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        assert entry is not None
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            try:
                base_url = validate_base_url(raw_base_url)
                new_uid = entry_unique_id(base_url, email)
                if self._uid_taken_by_other(new_uid, entry.entry_id):
                    # The new URL would duplicate another existing entry.
                    return self.async_abort(reason="already_configured")
                meters, cookies = await _login_and_capture(
                    self.hass, base_url, email, user_input[CONF_PASSWORD]
                )
            except InvalidBaseUrl:
                errors[CONF_BASE_URL] = "invalid_base_url"
            except WrmAuthError:
                errors["base"] = "invalid_auth"
            except WrmError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during WRM reconfigure")
                errors["base"] = "unknown"
            else:
                data = {
                    **entry.data,
                    CONF_BASE_URL: base_url,
                    CONF_SCAN_INTERVAL_HOURS: user_input[
                        CONF_SCAN_INTERVAL_HOURS
                    ],
                    "cookies": cookies,
                }
                if meters:
                    data[CONF_METERS] = meters
                self.hass.config_entries.async_update_entry(
                    entry, data=data, unique_id=new_uid
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_BASE_URL,
                    default=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL_HOURS,
                    default=entry.data.get(
                        CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
                    ),
                ): vol.All(int, vol.Range(min=1, max=24)),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"email": email},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the session expires."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._reauth_entry is not None
        entry = self._reauth_entry
        email = entry.data[CONF_EMAIL]
        base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                meters, cookies = await _login_and_capture(
                    self.hass, base_url, email, user_input[CONF_PASSWORD]
                )
            except WrmAuthError:
                errors["base"] = "invalid_auth"
            except WrmError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during WRM reauth")
                errors["base"] = "unknown"
            else:
                data = {**entry.data, "cookies": cookies}
                if meters:
                    data[CONF_METERS] = meters
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": email},
            errors=errors,
        )
