"""Tests for the async WrmClient using a fake aiohttp session."""

import pytest

from api import (
    InvalidBaseUrl,
    WrmAuthError,
    WrmClient,
    WrmError,
    SessionExpired,
    validate_base_url,
)


# -- URL and login-form validation ----------------------------------------


def test_valid_base_url_is_normalized():
    assert (
        validate_base_url(" HTTPS://WMD.WRM-SYSTEMS.FI/site/ ")
        == "https://wmd.wrm-systems.fi/site"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://wmd.wrm-systems.fi/site",
        "https://wrm-systems.fi/site",
        "https://evilwrm-systems.fi/site",
        "https://wmd.wrm-systems.fi.example.test/site",
        "https://user:password@wmd.wrm-systems.fi/site",
        "https://wmd.wrm-systems.fi/site?redirect=evil",
        "not a URL",
    ],
)
def test_invalid_base_url_is_rejected(base_url):
    with pytest.raises(InvalidBaseUrl):
        validate_base_url(base_url)


def test_login_form_requires_expected_action_and_fields(make_client, mocks):
    client, _ = make_client(lambda *a: mocks.FakeResponse())
    assert client._login_form(
        mocks.LOGIN_PAGE, "https://example.wrm-systems.fi/util/"
    ) == ("https://example.wrm-systems.fi/util/login", "FORM_TOKEN")

    external_action = mocks.LOGIN_PAGE.replace(
        'action="/util/login"', 'action="https://evil.test/login"'
    )
    with pytest.raises(WrmError):
        client._login_form(
            external_action, "https://example.wrm-systems.fi/util/"
        )

    with pytest.raises(WrmError):
        client._login_form(
            '<meta name="csrf-token" content="META_ONLY">',
            "https://example.wrm-systems.fi/util/",
        )


def test_is_login_page(mocks):
    assert WrmClient._is_login_page(mocks.LOGIN_PAGE) is True
    assert WrmClient._is_login_page(mocks.DASHBOARD) is False


# -- auth ------------------------------------------------------------------


async def test_login_success_posts_expected_fields(make_client, mocks):
    def handler(method, path, params, data):
        if method == "GET" and path == "/util/":
            return mocks.FakeResponse(text=mocks.LOGIN_PAGE)
        if method == "POST" and path == "/util/login":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        raise AssertionError(f"unexpected {method} {path}")

    client, session = make_client(handler)
    await client.login("a@b.fi", "secret")

    post = next(c for c in session.calls if c[0] == "POST")
    body = post[2]
    assert body["_csrf"] == "FORM_TOKEN"
    assert body["login-email"] == "a@b.fi"
    assert body["login-password"] == "secret"
    assert body["login-by"] == "email"
    assert body["mode"] == "water"


async def test_login_bad_credentials_raises(make_client, mocks):
    def handler(method, path, params, data):
        if method == "GET":
            return mocks.FakeResponse(text=mocks.LOGIN_PAGE)
        return mocks.FakeResponse(text=mocks.LOGIN_PAGE_BAD)

    client, _ = make_client(handler)
    with pytest.raises(WrmAuthError):
        await client.login("a@b.fi", "wrong")


async def test_login_rejects_external_redirect(make_client, mocks):
    def handler(method, path, params, data):
        if method == "GET":
            return mocks.FakeResponse(text=mocks.LOGIN_PAGE)
        return mocks.FakeResponse(
            status=302, headers={"Location": "https://evil.test/capture"}
        )

    client, _ = make_client(handler)
    with pytest.raises(WrmError, match="outside wrm-systems.fi"):
        await client.login("a@b.fi", "secret")


async def test_verify_session_ok_and_expired(make_client, mocks):
    client_ok, _ = make_client(lambda *a: mocks.FakeResponse(text=mocks.DASHBOARD))
    await client_ok.verify_session()  # no raise

    client_exp, _ = make_client(lambda *a: mocks.FakeResponse(text=mocks.LOGIN_PAGE))
    with pytest.raises(SessionExpired):
        await client_exp.verify_session()


# -- discovery -------------------------------------------------------------


async def test_discover_serial(make_client, mocks):
    client, _ = make_client(lambda *a: mocks.FakeResponse(text=mocks.READINGS_CARD))
    assert await client.discover_serial() == "12345678"


async def test_discover_meters_single_location(make_client, mocks):
    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)  # no selector
        if path == "/cards/readings":
            return mocks.FakeResponse(text=mocks.READINGS_CARD)
        raise AssertionError(path)

    client, _ = make_client(handler)
    meters = await client.discover_meters()
    assert meters == [
        {"serial": "12345678", "location_id": None, "location_name": None}
    ]


async def test_discover_meters_multiple_locations(make_client, mocks):
    active = {"loc": None}
    serials = {"101": "12345678", "202": "11112222"}

    def handler(method, path, params, data):
        if path == "/util/":
            if params and "locationId" in params:
                active["loc"] = params["locationId"]
                return mocks.FakeResponse(text=mocks.DASHBOARD)
            return mocks.FakeResponse(text=mocks.DASHBOARD_MULTI)
        if path == "/cards/readings":
            return mocks.FakeResponse(text=mocks.readings_card(serials[active["loc"]]))
        raise AssertionError(path)

    client, _ = make_client(handler)
    meters = await client.discover_meters()
    assert meters == [
        {"serial": "12345678", "location_id": "101", "location_name": "Kotikatu 1"},
        {"serial": "11112222", "location_id": "202", "location_name": "Mokkitie 5"},
    ]


async def test_set_location_switches_and_detects_expiry(make_client, mocks):
    client, session = make_client(lambda *a: mocks.FakeResponse(text=mocks.DASHBOARD))
    await client.set_location("202")
    assert ("GET", "/util/", {"locationId": "202"}) in session.calls

    expired, _ = make_client(lambda *a: mocks.FakeResponse(text=mocks.LOGIN_PAGE))
    with pytest.raises(SessionExpired):
        await expired.set_location("202")


# -- readings --------------------------------------------------------------


async def test_get_readings_parses_and_orders_oldest_first(make_client, mocks):
    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        if path == "/data/readings":
            return mocks.FakeResponse(json_data=mocks.READINGS_JSON)
        raise AssertionError(path)

    client, _ = make_client(handler)
    serial, rows = await client.get_readings("12345678", "2026-06-26", "2026-06-27")
    assert serial == "12345678"
    # newest-first input becomes oldest-first output
    assert rows[0]["timestamp"] == "26.6.2026 8:00"
    assert rows[-1]["timestamp"] == "27.6.2026 8:00"
    assert rows[-1]["reading_m3"] == 100.500
    assert set(rows[0]) == {"timestamp", "reading_m3", "consumption_m3", "epoch"}


async def test_get_readings_handles_null(make_client, mocks):
    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        return mocks.FakeResponse(json_data=None)

    client, _ = make_client(handler)
    serial, rows = await client.get_readings("12345678", "2026-06-26", "2026-06-27")
    assert rows == []


async def test_latest_reading_widens_window(make_client, mocks):
    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        if path == "/data/readings":
            # Only the widest window has data, forcing latest_reading to widen.
            if params["startDate"] == "2000-01-01":
                return mocks.FakeResponse(json_data=mocks.READINGS_JSON)
            return mocks.FakeResponse(json_data=None)
        raise AssertionError(path)

    client, _ = make_client(handler)
    latest = await client.latest_reading("12345678")
    assert latest["reading_m3"] == 100.500  # newest row
    assert latest["serial_number"] == "12345678"


# -- consumption / graphdata ----------------------------------------------


def test_flatten_series(mocks):
    rows = WrmClient.flatten_series(mocks.GRAPHDATA_JSON)
    assert rows[0] == {
        "timestamp": "2025-06-01T12:00:00+03:00",
        "value": 12.345,
        "unit": "m3/kk",
    }
    assert rows[1]["value"] is None  # null bucket preserved


async def test_graphdata_returns_data_and_defaults_period_scale(make_client, mocks):
    seen = {}

    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        if path == "/graphdata/alltime":
            seen.update(params)
            return mocks.FakeResponse(json_data=mocks.GRAPHDATA_JSON)
        raise AssertionError(path)

    client, _ = make_client(handler)
    data = await client.graphdata(period="hourly", start="1.6.2026", end="7.6.2026")
    assert data["unitText"] == "m3/kk"
    # hourly defaults to multiplier 1000 / decimals 0
    assert seen["multiplier"] == "1000"
    assert seen["decimals"] == "0"
    assert seen["type"] == "hourly"


async def test_graphdata_empty_raises(make_client, mocks):
    def handler(method, path, params, data):
        if path == "/util/":
            return mocks.FakeResponse(text=mocks.DASHBOARD)
        return mocks.FakeResponse(json_data={"status": 0, "note": "no data"})

    client, _ = make_client(handler)
    with pytest.raises(WrmError):
        await client.graphdata(period="daily", start="1.6.2026", end="7.6.2026")


# -- cookies ---------------------------------------------------------------


async def test_cookie_roundtrip(make_client, mocks):
    client, _ = make_client(lambda *a: mocks.FakeResponse(text=mocks.DASHBOARD))
    client.load_cookies({"sessionId": "abc", "_identity": "xyz"})
    assert client.export_cookies() == {"sessionId": "abc", "_identity": "xyz"}
