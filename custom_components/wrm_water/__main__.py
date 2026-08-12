#!/usr/bin/env python3
"""Command-line client for the WRM portal.

Runs the same async client the Home Assistant integration uses. Auth is
cookie-based: `login` signs in once (password used transiently, never stored)
and saves the session cookies to a JSON file; other commands reuse and refresh
them, mirroring the integration's sliding-session model.

Run it directly (NOT via `-m`, which would import the HA package __init__):

    python3 custom_components/wrm_water/__main__.py login
    python3 custom_components/wrm_water/__main__.py readings -o readings.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import io
import json
import os
import re
import sys
from datetime import date, timedelta

import aiohttp

try:  # run as a script (sibling modules on sys.path)
    from api import (
        WrmAuthError,
        WrmClient,
        WrmError,
        SessionExpired,
    )
except ImportError:  # imported as part of the package
    from .api import (
        WrmAuthError,
        WrmClient,
        WrmError,
        SessionExpired,
    )

DEFAULT_BASE = "https://wmd.wrm-systems.fi/site"
DEFAULT_COOKIES = os.path.expanduser("~/.config/wrm-water/cookies.json")


# -- cookie persistence ----------------------------------------------------


def load_cookie_file(path: str) -> dict[str, str]:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cookie_file(path: str, cookies: dict[str, str]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cookies, fh)


# -- date helpers ----------------------------------------------------------


def _to_iso_date(s: str) -> str:
    """Normalize 'd.M.yyyy' or 'yyyy-MM-dd' to ISO 'yyyy-MM-dd'."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s


def _fi_date(d: date) -> str:
    return f"{d.day}.{d.month}.{d.year}"


def _default_consumption_range(period: str) -> tuple[str, str]:
    today = date.today()
    spans = {
        "hourly": timedelta(days=7),
        "daily": timedelta(days=30),
        "monthly": timedelta(days=365),
        "yearly": timedelta(days=365 * 5),
    }
    start = today - spans.get(period, timedelta(days=30))
    return _fi_date(start), _fi_date(today)


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Wrote {output}", file=sys.stderr)
    else:
        print(text)


# -- client wiring ---------------------------------------------------------


async def _with_client(args, coro):
    """Build a client from the saved cookie jar, run coro(client), persist."""
    session = aiohttp.ClientSession()
    try:
        client = WrmClient(args.base, session)
        client.load_cookies(load_cookie_file(args.cookies))
        result = await coro(client)
        save_cookie_file(args.cookies, client.export_cookies())
        return result
    finally:
        await session.close()


def resolve_credentials(args) -> tuple[str, str]:
    email = args.email or os.environ.get("WRM_WATER_EMAIL") or input("Email: ").strip()
    password = (
        args.password
        or os.environ.get("WRM_WATER_PASSWORD")
        or getpass.getpass("Password: ")
    )
    return email, password


# -- commands --------------------------------------------------------------


async def cmd_login(args) -> int:
    email, password = resolve_credentials(args)
    session = aiohttp.ClientSession()
    try:
        client = WrmClient(args.base, session)
        try:
            await client.login(email, password)
        except WrmAuthError as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1
        meters = await client.discover_meters()
        save_cookie_file(args.cookies, client.export_cookies())
    finally:
        await session.close()
    print(f"Logged in as {email}. Session saved to {args.cookies}")
    print(f"Meters: {', '.join(m['serial'] for m in meters) or 'none found'}")
    return 0


async def cmd_whoami(args) -> int:
    async def run(client):
        await client.verify_session()
    try:
        await _with_client(args, run)
    except SessionExpired:
        print("Not authenticated (session missing or expired).", file=sys.stderr)
        return 1
    print("Authenticated (session valid).")
    return 0


async def cmd_meters(args) -> int:
    try:
        meters = await _with_client(args, lambda c: c.discover_meters())
    except SessionExpired:
        print("Not authenticated. Run `login` first.", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(meters, ensure_ascii=False, indent=2))
    else:
        for m in meters:
            loc = m.get("location_name") or m.get("location_id") or "-"
            print(f"{m['serial']}\t{loc}")
    return 0


async def cmd_readings(args) -> int:
    start = _to_iso_date(args.start) if args.start else "2000-01-01"
    end = _to_iso_date(args.end) if args.end else "2100-01-01"

    async def run(client):
        return await client.get_readings(args.serial, start, end)

    try:
        serial, rows = await _with_client(args, run)
    except SessionExpired:
        print("Not authenticated. Run `login` first.", file=sys.stderr)
        return 1
    except WrmError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    if args.newest_first:
        rows = list(reversed(rows))

    if args.format == "json":
        text = json.dumps(
            {"serialNumber": serial, "rows": rows}, ensure_ascii=False, indent=2
        )
        _write_or_print(text, args.output)
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "reading_m3", "consumption_m3"])
        for r in rows:
            w.writerow([r["timestamp"], r["reading_m3"], r["consumption_m3"]])
        _write_or_print(buf.getvalue().rstrip("\n"), args.output)
    span = f"{rows[0]['timestamp']} .. {rows[-1]['timestamp']}" if rows else "none"
    print(f"{len(rows)} readings, meter {serial}, {span}", file=sys.stderr)
    return 0


async def cmd_consumption(args) -> int:
    start, end = args.start, args.end
    if not start or not end:
        ds, de = _default_consumption_range(args.type)
        start = start or ds
        end = end or de

    async def run(client):
        return await client.graphdata(
            period=args.type, start=start, end=end, average=args.average
        )

    try:
        data = await _with_client(args, run)
    except SessionExpired:
        print("Not authenticated. Run `login` first.", file=sys.stderr)
        return 1
    except WrmError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    rows = WrmClient.flatten_series(data)

    if args.format == "json":
        payload = data if args.raw else {
            "type": args.type,
            "unit": data.get("unitText"),
            "rows": rows,
        }
        _write_or_print(json.dumps(payload, ensure_ascii=False, indent=2), args.output)
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "value", "unit"])
        for r in rows:
            w.writerow([r["timestamp"], r["value"], r["unit"]])
        _write_or_print(buf.getvalue().rstrip("\n"), args.output)
    n = sum(1 for r in rows if r["value"] is not None)
    print(
        f"{n}/{len(rows)} {args.type} points, unit {data.get('unitText')}, "
        f"{start}..{end}",
        file=sys.stderr,
    )
    return 0


async def cmd_logout(args) -> int:
    async def run(client):
        await client.logout()
    try:
        await _with_client(args, run)
    except WrmError:
        pass
    print("Logged out.")
    return 0


# -- argument parser -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wrm-water", description="WRM Systems water-consumption CLI client"
    )
    p.add_argument("--base", default=DEFAULT_BASE, help="Portal base URL")
    p.add_argument("--cookies", default=DEFAULT_COOKIES, help="Cookie jar (JSON)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("login", help="Authenticate and save the session")
    sp.add_argument("--email")
    sp.add_argument("--password", help="Omit to be prompted securely (getpass)")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("whoami", help="Check if the saved session is valid")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("meters", help="List accessible meters")
    sp.add_argument("--format", default="text", choices=["text", "json"])
    sp.set_defaults(func=cmd_meters)

    sp = sub.add_parser("readings", help="Fetch raw meter reading history")
    sp.add_argument("--serial", help="Meter serial (default: auto-discover)")
    sp.add_argument("--start", help="Start date yyyy-MM-dd or d.M.yyyy")
    sp.add_argument("--end", help="End date yyyy-MM-dd or d.M.yyyy")
    sp.add_argument("--newest-first", action="store_true")
    sp.add_argument("--format", default="csv", choices=["csv", "json"])
    sp.add_argument("--output", "-o")
    sp.set_defaults(func=cmd_readings)

    sp = sub.add_parser("consumption", help="Fetch bucketed consumption")
    sp.add_argument(
        "--type", default="daily",
        choices=["hourly", "daily", "monthly", "yearly"],
    )
    sp.add_argument("--start", help="Start date d.M.yyyy (default: period-based)")
    sp.add_argument("--end", help="End date d.M.yyyy (default: today)")
    sp.add_argument("--average", action="store_true")
    sp.add_argument("--format", default="csv", choices=["csv", "json"])
    sp.add_argument("--raw", action="store_true", help="json: emit raw API response")
    sp.add_argument("--output", "-o")
    sp.set_defaults(func=cmd_consumption)

    sp = sub.add_parser("logout", help="End the session")
    sp.set_defaults(func=cmd_logout)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
