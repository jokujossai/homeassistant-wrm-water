"""Test fixtures for the wrm_water integration.

Exercises the pure modules (api, bucketing) without Home Assistant or network:
the fake session mimics just the aiohttp surface WrmClient uses. The
integration package dir is on sys.path via `pythonpath` in pytest.ini, so
`api`/`bucketing` import directly without importing the HA __init__.py.

Mock payloads and fakes are exposed via the `mocks` fixture.
"""

from __future__ import annotations

import json as jsonlib
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


# -- mock portal payloads --------------------------------------------------

LOGIN_PAGE = """<!DOCTYPE html><html><head>
<meta name="csrf-token" content="META_TOKEN">
</head><body>
<form id="login-form" action="/util/login" method="post">
<input type="hidden" name="_csrf" value="FORM_TOKEN">
<input type="email" name="login-email">
<input type="password" name="login-password">
</form></body></html>"""

LOGIN_PAGE_BAD = LOGIN_PAGE.replace(
    "<body>",
    "<body><div class='alert alert-danger'>"
    "Virheellinen käyttäjätunnus tai salasana!</div>",
)

DASHBOARD = """<!DOCTYPE html><html><head>
<meta name="csrf-token" content="META_TOKEN">
</head><body><nav>Vedenkulutus</nav>
<a onclick="logout();">Kirjaudu ulos</a></body></html>"""

DASHBOARD_MULTI = """<!DOCTYPE html><html><body>
<select class="form-control change-usage-location">
  <option value="101">Kotikatu 1</option>
  <option value="202" selected>Mokkitie 5</option>
</select>
<a onclick="logout();">Kirjaudu ulos</a></body></html>"""

READINGS_CARD = '<script>const serialNumber = "12345678";</script><div>card</div>'


def readings_card(serial: str) -> str:
    return f'<script>const serialNumber = "{serial}";</script>'


# /data/readings returns newest-first [timestamp, reading_m3, consumption_m3, epoch]
READINGS_JSON = [
    ["27.6.2026 8:00", 100.500, 0.100, 1782536400],
    ["27.6.2026 7:00", 100.400, 0.200, 1782532800],
    ["26.6.2026 8:00", 100.200, 0.300, 1782450000],
]

# /graphdata/alltime: series data points are [epoch_ms, value, iso_ts]
GRAPHDATA_JSON = {
    "status": 1,
    "unitText": "m3/kk",
    "decimals": 3,
    "series": [
        {
            "type": 0,
            "data": [
                [1748779200000, 12.345, "2025-06-01T12:00:00+03:00"],
                [1751371200000, None, "2025-07-01T12:00:00+03:00"],
            ],
        }
    ],
}


# -- fake aiohttp ----------------------------------------------------------

_UNSET = object()


class FakeResponse:
    def __init__(self, status=200, text="", json_data=_UNSET, headers=None):
        self.status = status
        self._text = text
        self._json = json_data  # _UNSET = parse text; None = JSON null
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._json is not _UNSET:
            return self._json
        return jsonlib.loads(self._text)


class FakeCookieJar:
    def __init__(self):
        self._cookies: dict[str, str] = {}

    def update_cookies(self, cookies, response_url=None):
        self._cookies.update(dict(cookies))

    def __iter__(self):
        for key, value in self._cookies.items():
            yield SimpleNamespace(key=key, value=value)


class FakeSession:
    """Routes get()/post() to a handler(method, path, params, data)."""

    def __init__(self, handler):
        self._handler = handler
        self.cookie_jar = FakeCookieJar()
        self.calls: list[tuple] = []

    def get(
        self, url, params=None, headers=None, timeout=None, allow_redirects=True
    ):
        path = urlsplit(url).path
        self.calls.append(("GET", path, params))
        return self._handler("GET", path, params, None)

    def post(
        self, url, data=None, headers=None, timeout=None, allow_redirects=True
    ):
        path = urlsplit(url).path
        self.calls.append(("POST", path, data))
        return self._handler("POST", path, data, None)


MOCKS = SimpleNamespace(
    LOGIN_PAGE=LOGIN_PAGE,
    LOGIN_PAGE_BAD=LOGIN_PAGE_BAD,
    DASHBOARD=DASHBOARD,
    DASHBOARD_MULTI=DASHBOARD_MULTI,
    READINGS_CARD=READINGS_CARD,
    READINGS_JSON=READINGS_JSON,
    GRAPHDATA_JSON=GRAPHDATA_JSON,
    readings_card=readings_card,
    FakeResponse=FakeResponse,
    FakeSession=FakeSession,
    FakeCookieJar=FakeCookieJar,
)


@pytest.fixture
def mocks():
    """Mock payloads and aiohttp fakes for this component's tests."""
    return MOCKS


@pytest.fixture
def make_client():
    """Factory: build a WrmClient wired to a routing handler."""
    from api import WrmClient

    def _make(handler, base_url="https://example.wrm-systems.fi/util"):
        session = FakeSession(handler)
        return WrmClient(base_url, session), session

    return _make
