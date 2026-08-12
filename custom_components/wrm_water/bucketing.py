"""Pure helpers for turning meter readings into hourly statistics.

Kept free of Home Assistant imports so it can be unit-tested standalone.
"""

from __future__ import annotations

from datetime import datetime, timezone


def hourly_statistics(
    rows: list[dict], baseline: float | None = None
) -> list[tuple[datetime, float, float]]:
    """Bucket readings into hour-aligned (start_utc, state, sum) tuples.

    rows: oldest-first dicts with 'reading_m3' (cumulative) and 'epoch' (unix
    seconds). Within an hour the latest reading wins. state is the cumulative
    meter value; sum is consumption since the baseline (what a total_increasing
    sensor accumulates). baseline defaults to the first reading, so sum starts
    at 0; pass an explicit baseline to keep incremental imports on the same sum
    scale as previously recorded statistics. Readings with a None value are
    skipped. Returns oldest-first.
    """
    valid = [r for r in rows if r.get("reading_m3") is not None]
    if not valid:
        return []
    if baseline is None:
        baseline = valid[0]["reading_m3"]
    hourly: dict[datetime, float] = {}
    for row in valid:
        bucket = datetime.fromtimestamp(row["epoch"], tz=timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        hourly[bucket] = row["reading_m3"]
    return [
        (ts, reading, reading - baseline) for ts, reading in sorted(hourly.items())
    ]
