"""Import meter readings into long-term statistics.

The portal publishes readings in bulk (e.g. a whole day of hourly values at
once), so the live sensor can never attribute consumption to the right hour.
Instead, every coordinator poll imports readings newer than the last imported
one under the meter sensor's statistic_id, bucketed by the readings' own
timestamps. Overlapping hours are overwritten, which also retroactively fixes
any misattributed spikes the live sensor produced.

The statistics `sum` column must stay on one scale across imports: every row
satisfies sum = reading - baseline (the recorder chains its own compiled rows
additively, which preserves that invariant). Incremental imports therefore
derive the baseline from the last recorded row (state - sum) instead of
restarting at the first fetched reading.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
)

try:  # has_mean is deprecated and stops working in HA 2026.11
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_KWARGS = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # older HA without StatisticMeanType
    _MEAN_KWARGS = {"has_mean": False}

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

_UNIT_CLASS_KWARGS = (
    {"unit_class": VolumeConverter.UNIT_CLASS}
    if "unit_class" in StatisticMetaData.__annotations__
    else {}
)

from .bucketing import hourly_statistics
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import WrmCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_import_history(
    hass: HomeAssistant,
    coordinator: WrmCoordinator,
    start: str | None = None,
    end: str | None = None,
    reset: bool = False,
) -> None:
    """Backfill hourly statistics for every meter (import_history service).

    With reset=True the recorded statistics rows in the date range are deleted
    first. A plain import only overwrites hours that have a reading, so reset
    is the way to get rid of stale rows at hours without one (e.g. spikes the
    live sensor recorded at poll time before the batch was imported).
    """
    _LOGGER.info(
        "WRM: history import starting (start=%s, end=%s, reset=%s)",
        start or "all",
        end or "all",
        reset,
    )
    if not coordinator.meters:
        coordinator.meters = await coordinator.client.discover_meters()

    for meter in coordinator.meters:
        serial = meter["serial"]
        statistic_id = _statistic_id(hass, serial)
        if statistic_id is None:
            _LOGGER.warning(
                "WRM: meter sensor for %s not found yet; let the "
                "integration finish setup before importing history",
                serial,
            )
            continue
        # Read the sum scale BEFORE any deletion: re-imported rows must stay
        # on the same scale as the recorder's live short-term chain, or the
        # next compiled row shows up as a huge negative consumption.
        baseline = await _existing_baseline(hass, statistic_id)
        if reset:
            start_ts = _day_start_ts(start, "2000-01-01")
            # +1 day so the whole (inclusive) end date is covered.
            end_ts = _day_start_ts(end, "2100-01-01", extra_days=1)
            removed = await get_instance(hass).async_add_executor_job(
                _clear_statistics_range, hass, statistic_id, start_ts, end_ts
            )
            _LOGGER.info(
                "WRM: reset removed %d statistics rows for %s",
                removed,
                statistic_id,
            )
        _, rows = await coordinator.client.get_readings(
            serial,
            start_date=start or "2000-01-01",
            end_date=end or "2100-01-01",
        )
        if not rows:
            _LOGGER.warning("WRM: no readings to import for %s", serial)
            continue
        _LOGGER.info(
            "WRM: fetched %d readings for meter %s (%s .. %s)",
            len(rows),
            serial,
            rows[0].get("timestamp"),
            rows[-1].get("timestamp"),
        )
        _import_rows(hass, coordinator, statistic_id, serial, rows, baseline)

    await coordinator._persist()  # keep the sliding session fresh


async def async_import_new_readings(
    hass: HomeAssistant, coordinator: WrmCoordinator
) -> None:
    """Import readings newer than the last imported one for each meter.

    Called on every coordinator poll. The first time (no import watermark yet)
    it backfills the full history; afterwards it fetches only from the day
    before the watermark and imports the strictly newer readings. Does NOT
    persist the config entry — the coordinator does that after polling.
    """
    for meter in coordinator.meters:
        serial = meter["serial"]
        statistic_id = _statistic_id(hass, serial)
        if statistic_id is None:
            _LOGGER.debug(
                "WRM: meter sensor for %s not registered yet; skipping import",
                serial,
            )
            continue
        last_epoch = coordinator.last_imported.get(serial)
        if last_epoch is None:
            _LOGGER.info(
                "WRM: no import watermark for meter %s; importing full history",
                serial,
            )
            _, rows = await coordinator.client.get_readings(serial)
        else:
            # Fetch from a day before the watermark: the portal filters by
            # (local) date while epochs are UTC, so pad the boundary.
            since = datetime.fromtimestamp(last_epoch, tz=timezone.utc)
            since -= timedelta(days=1)
            _, rows = await coordinator.client.get_readings(
                serial, start_date=since.date().isoformat()
            )
            rows = [r for r in rows if (r.get("epoch") or 0) > last_epoch]
            _LOGGER.debug(
                "WRM: meter %s: %d readings newer than watermark %s",
                serial,
                len(rows),
                datetime.fromtimestamp(last_epoch, tz=timezone.utc).isoformat(),
            )
        if not rows:
            continue
        baseline = await _existing_baseline(hass, statistic_id)
        _import_rows(hass, coordinator, statistic_id, serial, rows, baseline)


def _statistic_id(hass: HomeAssistant, serial: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{serial}_reading"
    )


def _day_start_ts(
    value: str | None, default: str, extra_days: int = 0
) -> float:
    """Epoch of local midnight for a yyyy-MM-dd or d.M.yyyy date string."""
    raw = (value or default).strip()
    day = dt_util.parse_date(raw)
    if day is None:
        try:
            day = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError as err:
            raise HomeAssistantError(f"Invalid date: {raw}") from err
    return dt_util.start_of_local_day(day + timedelta(days=extra_days)).timestamp()


def _clear_statistics_range(
    hass: HomeAssistant, statistic_id: str, start_ts: float, end_ts: float
) -> int:
    """Delete long-term statistics rows with start in [start_ts, end_ts).

    Runs on the recorder executor. There is no public range-delete API, so
    this works on the recorder's schema tables directly. Short-term (5-min)
    statistics are deliberately left alone: the recorder chains each compiled
    row's sum from the previous short-term row, and deleting those restarts
    the chain at zero — which renders as a large negative consumption at the
    reset time. They age out on their own retention anyway.
    """
    from homeassistant.components.recorder.db_schema import (
        Statistics,
        StatisticsMeta,
    )
    from homeassistant.components.recorder.util import session_scope

    with session_scope(session=get_instance(hass).get_session()) as session:
        metadata_id = (
            session.query(StatisticsMeta.id)
            .filter(StatisticsMeta.statistic_id == statistic_id)
            .scalar()
        )
        if metadata_id is None:
            return 0
        return (
            session.query(Statistics)
            .filter(
                Statistics.metadata_id == metadata_id,
                Statistics.start_ts >= start_ts,
                Statistics.start_ts < end_ts,
            )
            .delete(synchronize_session=False)
        )


async def _existing_baseline(
    hass: HomeAssistant, statistic_id: str
) -> float | None:
    """The sum scale of already-recorded statistics (state - sum), if any."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"state", "sum"}
    )
    rows = last.get(statistic_id) or []
    if rows and rows[0].get("state") is not None and rows[0].get("sum") is not None:
        return rows[0]["state"] - rows[0]["sum"]
    return None


def _import_rows(
    hass: HomeAssistant,
    coordinator: WrmCoordinator,
    statistic_id: str,
    serial: str,
    rows: list[dict],
    baseline: float | None,
) -> None:
    statistics = [
        StatisticData(start=ts, state=state, sum=total)
        for ts, state, total in hourly_statistics(rows, baseline)
    ]
    if not statistics:
        return
    metadata = StatisticMetaData(
        **_MEAN_KWARGS,
        **_UNIT_CLASS_KWARGS,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    )
    async_import_statistics(hass, metadata, statistics)
    coordinator.last_imported[serial] = max(
        r["epoch"] for r in rows if r.get("reading_m3") is not None
    )
    _LOGGER.info(
        "WRM: imported %d hourly statistics for %s",
        len(statistics),
        statistic_id,
    )
