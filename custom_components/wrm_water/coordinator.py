"""Data update coordinator for WRM Water Consumption."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WrmClient, WrmError, SessionExpired
from .const import (
    CONF_LAST_IMPORTED,
    CONF_METERS,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class WrmCoordinator(DataUpdateCoordinator[dict]):
    """Polls each meter's latest reading and keeps the cookie session fresh.

    coordinator.data is keyed by meter serial -> reading dict. /data/readings is
    global per serial, so meters are fetched without switching usage location.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: WrmClient
    ) -> None:
        hours = entry.data.get(
            CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=hours),
        )
        self.entry = entry
        self.client = client
        # [{serial, location_id, location_name}, ...]
        self.meters: list[dict] = list(entry.data.get(CONF_METERS) or [])
        # serial -> epoch of the newest reading imported into statistics.
        self.last_imported: dict[str, int] = dict(
            entry.data.get(CONF_LAST_IMPORTED) or {}
        )
        # The dedicated aiohttp session, set by async_setup_entry; closed on
        # unload.
        self.session = None

    async def _async_update_data(self) -> dict:
        try:
            if not self.meters:
                self.meters = await self.client.discover_meters()
            result: dict[str, dict] = {}
            for meter in self.meters:
                reading = await self.client.latest_reading(meter["serial"])
                reading["location_id"] = meter.get("location_id")
                reading["location_name"] = meter.get("location_name")
                result[meter["serial"]] = reading
        except SessionExpired as err:
            # Surface as a reauth flow: HA shows "session expired" and asks for
            # the password to re-login.
            raise ConfigEntryAuthFailed(str(err)) from err
        except WrmError as err:
            raise UpdateFailed(f"WRM error: {err}") from err

        # The portal publishes readings in bulk, so hourly attribution must
        # come from importing timestamped statistics, not from sensor states.
        # A failed import must not take the sensors down with it.
        try:
            from .statistics_import import async_import_new_readings

            await async_import_new_readings(self.hass, self)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Importing new readings into statistics failed")

        await self._persist()
        return result

    async def _persist(self) -> None:
        """Persist cookies (sliding session), meters and import watermarks."""
        cookies = self.client.export_cookies()
        data = self.entry.data
        if (
            cookies == data.get("cookies")
            and self.meters == data.get(CONF_METERS)
            and self.last_imported == data.get(CONF_LAST_IMPORTED)
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **data,
                "cookies": cookies,
                CONF_METERS: self.meters,
                CONF_LAST_IMPORTED: dict(self.last_imported),
            },
        )
