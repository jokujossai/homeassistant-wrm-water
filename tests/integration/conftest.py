"""Fixtures for full Home Assistant integration tests.

These require pytest-homeassistant-custom-component (see requirements_test.txt),
which provides the hass/aioclient_mock/recorder_mock fixtures. Without it this
whole directory is skipped, so the pure-logic suite in tests/ still runs in
minimal environments.

The portal is mocked at the HTTP layer (aioclient_mock) with the same payloads
the pure api tests use, so setup exercises the real client, coordinator,
sensor and statistics-import code paths.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.wrm_water.const import (  # noqa: E402
    CONF_BASE_URL,
    CONF_METERS,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)

BASE_URL = "https://portal.wrm-systems.fi/util"
ORIGIN = "https://portal.wrm-systems.fi"
SERIAL = "12345678"
EMAIL = "test@example.fi"


@pytest.fixture(autouse=True)
def _init_recorder_db_url(recorder_db_url):
    """Instantiate the recorder DB fixture before hass.

    The plugin asserts recorder_db_url is created before the hass instance of
    the test. Our autouse enable_custom_integrations fixture depends on hass,
    so without this explicit ordering the recorder-based statistics tests
    error out. Defined first so it runs before the other autouse fixtures.
    """
    return recorder_db_url


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let the test hass load custom_components/wrm_water."""
    return None


@pytest.fixture(autouse=True)
def _no_aiodns(monkeypatch):
    """Keep aiohttp off the pycares resolver in tests.

    Mocked sessions still construct a real TCPConnector; with aiodns installed
    its AsyncResolver spawns a global pycares shutdown thread that trips the
    plugin's lingering-thread check. Mock sessions never resolve DNS, so the
    threaded resolver is equivalent here.
    """
    import aiohttp.connector
    import aiohttp.resolver

    monkeypatch.setattr(
        aiohttp.connector,
        "DefaultResolver",
        aiohttp.resolver.ThreadedResolver,
        raising=False,
    )
    monkeypatch.setattr(
        aiohttp.resolver,
        "DefaultResolver",
        aiohttp.resolver.ThreadedResolver,
        raising=False,
    )


@pytest.fixture
def config_entry(hass):
    """A configured entry as the config flow would create it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"WRM Water {EMAIL}",
        unique_id=f"{BASE_URL}::{EMAIL}",
        data={
            CONF_BASE_URL: BASE_URL,
            "email": EMAIL,
            CONF_METERS: [
                {"serial": SERIAL, "location_id": None, "location_name": None}
            ],
            CONF_SCAN_INTERVAL_HOURS: 2,
            "cookies": {"sessionId": "sid", "_identity": "idc"},
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def portal(aioclient_mock, mocks):
    """Serve a logged-in portal; call again with rows=... to change data."""

    def _register(rows=None, dashboard=None):
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            f"{BASE_URL}/", text=dashboard or mocks.DASHBOARD
        )
        aioclient_mock.get(
            f"{ORIGIN}/cards/readings", text=mocks.READINGS_CARD
        )
        aioclient_mock.get(
            f"{ORIGIN}/data/readings",
            json=mocks.READINGS_JSON if rows is None else rows,
        )

    _register()
    return _register


@pytest.fixture
def setup_entry(hass):
    """Async helper: set up a config entry and settle all tasks."""

    async def _setup(entry) -> None:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return _setup
