"""Integration tests: statistics import (initial, incremental, reset).

These run against a real in-memory recorder (recorder_mock), exercising the
actual async_import_statistics / get_last_statistics plumbing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("homeassistant")

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)

try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _TEST_MEAN_KWARGS = {"mean_type": StatisticMeanType.NONE}
except ImportError:
    _TEST_MEAN_KWARGS = {"has_mean": False}

from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import VolumeConverter
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

_TEST_UNIT_CLASS_KWARGS = (
    {"unit_class": VolumeConverter.UNIT_CLASS}
    if "unit_class" in StatisticMetaData.__annotations__
    else {}
)

from custom_components.wrm_water.const import DOMAIN

SERIAL = "12345678"
UTC = timezone.utc

# READINGS_JSON buckets (timestamps are Finnish local time, UTC+3 in summer):
# 26.6. 8:00 -> 05:00Z (100.2, sum 0.0)
# 27.6. 7:00 -> 04:00Z (100.4, sum 0.2)
# 27.6. 8:00 -> 05:00Z (100.5, sum 0.3)
T1 = datetime(2026, 6, 26, 5, tzinfo=UTC).timestamp()
T2 = datetime(2026, 6, 27, 4, tzinfo=UTC).timestamp()
T3 = datetime(2026, 6, 27, 5, tzinfo=UTC).timestamp()
T4 = datetime(2026, 6, 27, 6, tzinfo=UTC).timestamp()  # incremental row


def _entity_id(hass) -> str:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_reading"
    )


async def _get_stats(hass, statistic_id: str) -> dict[float, tuple]:
    """{start_ts: (state, sum)} for all hourly rows of statistic_id."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 6, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return {
        row["start"]: (row["state"], row["sum"])
        for row in stats.get(statistic_id, [])
    }


async def _non_admin_context(hass) -> Context:
    """Create an explicitly non-admin user and return its service context."""
    # The first regular user becomes the owner, so create it before the
    # explicitly non-admin user whose service call is under test.
    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user(
        "Non-admin user", group_ids=[GROUP_ID_USER]
    )
    assert not user.is_admin
    return Context(user_id=user.id)


async def test_initial_backfill_on_setup(
    recorder_mock, hass, config_entry, portal, setup_entry
):
    """First setup imports the full history and sets the watermark."""
    await setup_entry(config_entry)
    await async_wait_recording_done(hass)

    rows = await _get_stats(hass, _entity_id(hass))
    assert set(rows) == {T1, T2, T3}
    assert rows[T1] == (pytest.approx(100.2), pytest.approx(0.0))
    assert rows[T2] == (pytest.approx(100.4), pytest.approx(0.2))
    assert rows[T3] == (pytest.approx(100.5), pytest.approx(0.3))

    assert config_entry.data["last_imported"][SERIAL] == 1782536400


async def test_incremental_import_on_poll(
    recorder_mock, hass, config_entry, portal, setup_entry, mocks
):
    """A poll imports only readings newer than the watermark, on the same
    sum scale as the existing statistics."""
    await setup_entry(config_entry)
    await async_wait_recording_done(hass)

    newer = [["27.6.2026 9:00", 100.900, 0.400, 1782540000]]
    portal(rows=newer + mocks.READINGS_JSON)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    rows = await _get_stats(hass, _entity_id(hass))
    assert set(rows) == {T1, T2, T3, T4}
    # Sum continues from 0.3, not restarting at 0.
    assert rows[T4] == (pytest.approx(100.9), pytest.approx(0.7))
    assert config_entry.data["last_imported"][SERIAL] == 1782540000


async def test_import_service_reset_removes_stale_rows(
    recorder_mock, hass, config_entry, portal, setup_entry
):
    """reset: true wipes rows a plain import cannot overwrite."""
    await setup_entry(config_entry)
    await async_wait_recording_done(hass)
    statistic_id = _entity_id(hass)

    # Inject a bogus row at an hour that has no meter reading (like a spike
    # the live sensor recorded at poll time).
    bogus_start = datetime(2026, 6, 26, 10, tzinfo=UTC)
    async_import_statistics(
        hass,
        StatisticMetaData(
            **_TEST_MEAN_KWARGS,
            **_TEST_UNIT_CLASS_KWARGS,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        ),
        [StatisticData(start=bogus_start, state=999.0, sum=999.0)],
    )
    await async_wait_recording_done(hass)
    rows = await _get_stats(hass, statistic_id)
    assert bogus_start.timestamp() in rows

    await hass.services.async_call(
        DOMAIN, "import_history", {"reset": True}, blocking=True
    )
    await async_wait_recording_done(hass)

    rows = await _get_stats(hass, statistic_id)
    assert bogus_start.timestamp() not in rows
    assert set(rows) == {T1, T2, T3}
    # The sum scale is captured before deletion and preserved, so the
    # re-import stays continuous with the recorder's live chain.
    assert rows[T3] == (pytest.approx(100.5), pytest.approx(0.3))


async def test_import_service_plain_import(
    recorder_mock, hass, config_entry, portal, setup_entry
):
    """The service without reset re-imports and stays idempotent."""
    await setup_entry(config_entry)
    await async_wait_recording_done(hass)
    context = await _non_admin_context(hass)

    await hass.services.async_call(
        DOMAIN, "import_history", {}, blocking=True, context=context
    )
    await async_wait_recording_done(hass)

    rows = await _get_stats(hass, _entity_id(hass))
    assert set(rows) == {T1, T2, T3}
    assert rows[T3] == (pytest.approx(100.5), pytest.approx(0.3))


async def test_import_service_reset_requires_admin(
    recorder_mock, hass, config_entry, portal, setup_entry
):
    """An authenticated non-admin cannot delete statistics rows."""
    await setup_entry(config_entry)
    context = await _non_admin_context(hass)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "import_history",
            {"reset": True},
            blocking=True,
            context=context,
        )
