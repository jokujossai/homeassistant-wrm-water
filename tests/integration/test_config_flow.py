"""Integration tests: config, reauth and reconfigure flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wrm_water.const import (
    CONF_BASE_URL,
    CONF_METERS,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)

BASE_URL = "https://portal.wrm-systems.fi/util"
EMAIL = "test@example.fi"
METERS = [{"serial": "12345678", "location_id": None, "location_name": None}]
COOKIES = {"sessionId": "new-sid", "_identity": "new-idc"}

LOGIN_OK = patch(
    "custom_components.wrm_water.config_flow._login_and_capture",
    return_value=(METERS, COOKIES),
)
SETUP_OK = patch(
    "custom_components.wrm_water.async_setup_entry", return_value=True
)


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    with LOGIN_OK, SETUP_OK:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: "Test@Example.fi",
                CONF_PASSWORD: "secret",
                CONF_BASE_URL: BASE_URL,
                CONF_SCAN_INTERVAL_HOURS: 6,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    # unique_id is normalized portal URL + lowercased email.
    assert entry.unique_id == f"{BASE_URL}::{EMAIL}"
    assert entry.data[CONF_METERS] == METERS
    assert entry.data["cookies"] == COOKIES
    assert entry.data[CONF_SCAN_INTERVAL_HOURS] == 6
    # The password must never be persisted.
    assert CONF_PASSWORD not in entry.data


async def test_user_flow_invalid_auth_then_recovers(hass, aioclient_mock, mocks):
    """Wrong password shows an error; the form stays open for retry."""
    aioclient_mock.get(f"{BASE_URL}/", text=mocks.LOGIN_PAGE)
    aioclient_mock.post(f"{BASE_URL}/login", text=mocks.LOGIN_PAGE_BAD)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong", CONF_BASE_URL: BASE_URL},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass, aioclient_mock):
    """A page without the expected login form means the portal is unusable."""
    aioclient_mock.get(f"{BASE_URL}/", text="<html>broken</html>")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_BASE_URL: BASE_URL},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "base_url",
    [
        "http://portal.wrm-systems.fi/util",
        "https://wrm-systems.fi/util",
        "https://portal.test/util",
    ],
)
async def test_user_flow_rejects_non_wrm_https_url(hass, base_url):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with LOGIN_OK as login:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_BASE_URL: base_url},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BASE_URL: "invalid_base_url"}
    login.assert_not_called()


async def test_duplicate_account_aborts(hass, config_entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with LOGIN_OK, SETUP_OK:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_BASE_URL: BASE_URL},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_same_email_different_portal_is_new_entry(hass, config_entry):
    other_url = "https://portal.wrm-systems.fi/othercity"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with LOGIN_OK, SETUP_OK:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_BASE_URL: other_url},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"{other_url}::{EMAIL}"


async def test_reauth_flow_refreshes_cookies(hass, config_entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": config_entry.entry_id},
        data=config_entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    with LOGIN_OK, SETUP_OK:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "newpw"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["cookies"] == COOKIES


async def test_reconfigure_updates_interval_and_url(hass, config_entry):
    new_url = "https://portal.wrm-systems.fi/newcity"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": config_entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"

    with LOGIN_OK, SETUP_OK:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PASSWORD: "pw",
                CONF_BASE_URL: new_url,
                CONF_SCAN_INTERVAL_HOURS: 12,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_BASE_URL] == new_url
    assert config_entry.data[CONF_SCAN_INTERVAL_HOURS] == 12
    assert config_entry.unique_id == f"{new_url}::{EMAIL}"
