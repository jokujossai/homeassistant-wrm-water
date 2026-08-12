"""Tests for the pure hourly-bucketing helper."""

from datetime import datetime, timezone

import pytest

from bucketing import hourly_statistics

UTC = timezone.utc


def test_empty():
    assert hourly_statistics([]) == []


def test_all_none_values():
    rows = [{"reading_m3": None, "epoch": 3600}]
    assert hourly_statistics(rows) == []


def test_latest_reading_wins_within_hour():
    rows = [
        {"reading_m3": 10.0, "epoch": 3600 * 10 + 1800},  # 10:30
        {"reading_m3": 10.5, "epoch": 3600 * 10 + 3000},  # 10:50 same hour
        {"reading_m3": 11.0, "epoch": 3600 * 12 + 60},    # 12:01
        {"reading_m3": None, "epoch": 3600 * 13},          # skipped
    ]
    result = hourly_statistics(rows)
    assert [ts for ts, _, _ in result] == [
        datetime(1970, 1, 1, 10, tzinfo=UTC),
        datetime(1970, 1, 1, 12, tzinfo=UTC),
    ]
    # 10:00 bucket keeps the later 10.5 reading.
    assert result[0][1] == pytest.approx(10.5)
    assert result[1][1] == pytest.approx(11.0)


def test_sum_is_consumption_since_baseline():
    rows = [
        {"reading_m3": 18.855, "epoch": 3600 * 1},
        {"reading_m3": 18.958, "epoch": 3600 * 2},
        {"reading_m3": 20.0, "epoch": 3600 * 3},
    ]
    result = hourly_statistics(rows)
    sums = [total for _, _, total in result]
    assert sums[0] == pytest.approx(0.0)
    assert sums[1] == pytest.approx(0.103)
    assert sums[2] == pytest.approx(1.145)


def test_explicit_baseline_keeps_sum_scale():
    # Incremental imports pass baseline = last recorded state - sum so the
    # new rows continue the existing sum scale instead of restarting at 0.
    rows = [
        {"reading_m3": 100.0, "epoch": 3600 * 1},
        {"reading_m3": 101.5, "epoch": 3600 * 2},
    ]
    result = hourly_statistics(rows, baseline=90.0)
    assert [total for _, _, total in result] == [
        pytest.approx(10.0),
        pytest.approx(11.5),
    ]


def test_hour_aligned_and_monotonic():
    rows = [
        {"reading_m3": float(i), "epoch": 3600 * i + 137}  # +137s offset
        for i in range(1, 25)
    ]
    result = hourly_statistics(rows)
    assert all(ts.minute == 0 and ts.second == 0 for ts, _, _ in result)
    sums = [total for _, _, total in result]
    assert sums == sorted(sums)
