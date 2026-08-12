"""Async client for the WRM portal.

Self-contained (regex parsing, no BeautifulSoup) so the integration only needs
aiohttp, which Home Assistant already ships. The caller supplies a dedicated
aiohttp.ClientSession (one per config entry) whose cookie jar holds the auth
session.

Auth model: the password is only used transiently to log in (config flow /
reauth). What we persist is the COOKIE JAR (a simple name->value dict). Yii keeps
the session alive with a sliding 24h `_identity` cookie re-issued on every
request, so as long as we poll within 24h and persist the cookies the login
stays valid without ever storing the password. When the session finally lapses
the portal serves the login page again and we raise SessionExpired so HA can ask
for the password via a reauth flow.

Login flow:
  1. GET <base>         -> sessionId + _csrf cookies + masked CSRF token
  2. POST <base>/login  with _csrf, mode=water, login-email, login-password,
                           login-by=email
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser

import aiohttp
from yarl import URL

USER_AGENT = "homeassistant-wrm-water/0.1"
BAD_LOGIN_MARKER = "Virheellinen käyttäjätunnus tai salasana"
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_LOGIN_PAGE_RE = re.compile(r'name=["\']login-password["\']', re.IGNORECASE)
_SERIAL_RE = re.compile(r'serialNumber\s*=\s*"(\d+)"')
_ALLOWED_HOST_SUFFIX = ".wrm-systems.fi"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
# A login with several usage locations renders a <select class=change-usage-location>
# with one <option value=locationId> each. Absent for single-location accounts.
_SELECT_RE = re.compile(
    r"<select[^>]*change-usage-location[^>]*>(.*?)</select>",
    re.DOTALL | re.IGNORECASE,
)
_OPTION_RE = re.compile(
    r'<option[^>]*value=["\']?(\d+)["\']?[^>]*>(.*?)</option>',
    re.DOTALL | re.IGNORECASE,
)


class WrmError(Exception):
    """Generic API error."""


class InvalidBaseUrl(WrmError):
    """The portal URL is not an allowed WRM HTTPS URL."""


class WrmAuthError(WrmError):
    """Bad credentials supplied during login."""


class SessionExpired(WrmError):
    """The persisted session is no longer valid; re-login required."""


def _is_allowed_portal_url(url: URL) -> bool:
    """Return whether URL uses HTTPS on a WRM Systems subdomain."""
    raw_host = url.raw_host
    return (
        url.scheme == "https"
        and raw_host is not None
        and raw_host.lower().endswith(_ALLOWED_HOST_SUFFIX)
        and url.user is None
        and url.password is None
    )


def validate_base_url(base_url: str) -> str:
    """Validate and normalize a WRM portal base URL."""
    try:
        url = URL(base_url.strip())
        # Accessing port also validates an explicitly supplied port value.
        _ = url.port
    except (AttributeError, TypeError, ValueError) as err:
        raise InvalidBaseUrl("Invalid portal URL") from err

    if not _is_allowed_portal_url(url) or url.query_string or url.fragment:
        raise InvalidBaseUrl(
            "Portal URL must use HTTPS on a subdomain of wrm-systems.fi"
        )
    return str(url).rstrip("/")


@dataclass
class _ParsedForm:
    """HTML form attributes and its input elements."""

    attributes: dict[str, str | None]
    inputs: dict[str, dict[str, str | None]]


class _FormParser(HTMLParser):
    """Collect forms and their inputs using the standard-library parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_ParsedForm] = []
        self._attributes: dict[str, str | None] | None = None
        self._inputs: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "form" and self._attributes is None:
            self._attributes = attributes
            self._inputs = {}
        elif tag == "input" and self._attributes is not None:
            if name := attributes.get("name"):
                self._inputs[name] = attributes

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._attributes is not None:
            self.forms.append(_ParsedForm(self._attributes, self._inputs))
            self._attributes = None
            self._inputs = {}


class WrmClient:
    """Minimal async WRM client backed by an aiohttp cookie jar."""

    def __init__(self, base_url: str, session: aiohttp.ClientSession) -> None:
        self.base_url = validate_base_url(base_url)
        self._session = session
        self._headers = {"User-Agent": USER_AGENT}

    # -- helpers -----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _origin(self) -> str:
        return str(URL(self.base_url).origin())

    @staticmethod
    def _safe_redirect_url(current_url: str, location: str | None) -> str:
        """Resolve and validate a redirect before making another request."""
        if not location:
            raise WrmError("Portal returned a redirect without a location")
        try:
            target = URL(current_url).join(URL(location)).with_fragment(None)
        except (TypeError, ValueError) as err:
            raise WrmError("Portal returned an invalid redirect") from err
        if not _is_allowed_portal_url(target):
            raise WrmError("Portal redirected outside wrm-systems.fi")
        return str(target)

    async def _get_text_and_url(self, url: str, **kwargs) -> tuple[str, str]:
        """GET text, following only validated WRM HTTPS redirects."""
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            async with self._session.get(
                current_url,
                headers=self._headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            ) as resp:
                if resp.status in _REDIRECT_STATUSES:
                    current_url = self._safe_redirect_url(
                        current_url, resp.headers.get("Location")
                    )
                    continue
                resp.raise_for_status()
                return await resp.text(), current_url
        raise WrmError("Portal returned too many redirects")

    async def _get_text(self, url: str, **kwargs) -> str:
        text, _ = await self._get_text_and_url(url, **kwargs)
        return text

    def _login_form(self, html: str, page_url: str) -> tuple[str, str]:
        """Return the verified login action and its form CSRF token."""
        parser = _FormParser()
        parser.feed(html)
        expected_action = URL(self._url("/login"))
        required_inputs = {
            "_csrf": "hidden",
            "login-email": "email",
            "login-password": "password",
        }

        for form in parser.forms:
            if (form.attributes.get("method") or "get").lower() != "post":
                continue
            action = form.attributes.get("action")
            if not action:
                continue
            try:
                target = URL(page_url).join(URL(action))
            except (TypeError, ValueError):
                continue
            if target != expected_action:
                continue
            if any(
                name not in form.inputs
                or (form.inputs[name].get("type") or "text").lower() != input_type
                for name, input_type in required_inputs.items()
            ):
                continue
            token = form.inputs["_csrf"].get("value")
            if token:
                return str(target), token
        raise WrmError("Valid WRM login form not found")

    @staticmethod
    def _is_login_page(html: str) -> bool:
        return bool(_LOGIN_PAGE_RE.search(html))

    # -- cookie persistence ------------------------------------------------

    def export_cookies(self) -> dict[str, str]:
        """Current jar as a JSON-friendly name->value dict."""
        return {c.key: c.value for c in self._session.cookie_jar}

    def load_cookies(self, cookies: dict[str, str] | None) -> None:
        if cookies:
            self._session.cookie_jar.update_cookies(cookies, URL(self._origin()))

    # -- auth --------------------------------------------------------------

    async def login(self, email: str, password: str) -> None:
        """Authenticate with email+password (transient; not persisted).

        Raises WrmAuthError on bad credentials. On success the session
        jar holds the auth cookies; call export_cookies() to persist them.
        """
        html, page_url = await self._get_text_and_url(self._url("/"))
        login_url, token = self._login_form(html, page_url)
        redirect_url: str | None = None
        async with self._session.post(
            login_url,
            data={
                "_csrf": token,
                "mode": "water",
                "login-email": email,
                "login-password": password,
                "login-by": "email",
            },
            headers=self._headers,
            timeout=_TIMEOUT,
            allow_redirects=False,
        ) as resp:
            if resp.status in {307, 308}:
                # Replaying the POST would forward credentials in its body.
                raise WrmError("Portal returned an unsafe login redirect")
            if resp.status in _REDIRECT_STATUSES:
                redirect_url = self._safe_redirect_url(
                    login_url, resp.headers.get("Location")
                )
                text = ""
            else:
                resp.raise_for_status()
                text = await resp.text()
        if redirect_url is not None:
            text = await self._get_text(redirect_url)
        if BAD_LOGIN_MARKER in text or self._is_login_page(text):
            raise WrmAuthError("Invalid email or password")

    async def verify_session(self) -> None:
        """Confirm the cookie session is still authenticated.

        Raises SessionExpired if the portal returns the login page.
        """
        html = await self._get_text(self._url("/"))
        if self._is_login_page(html):
            raise SessionExpired("WRM session expired")

    async def logout(self) -> None:
        """End the session."""
        async with self._session.post(
            self._url("/logout"), headers=self._headers, timeout=_TIMEOUT
        ) as resp:
            resp.raise_for_status()

    # -- usage locations / meters -----------------------------------------

    async def set_location(self, location_id: str) -> None:
        """Switch the session's active usage location (stateful).

        The portal scopes the dashboard/cards (and possibly /data/readings) to
        the currently selected location, switched via ?locationId=.
        """
        async with self._session.get(
            self._url("/"),
            params={"locationId": location_id},
            headers=self._headers,
            timeout=_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
        if self._is_login_page(text):
            raise SessionExpired("WRM session expired")

    async def discover_serial(self) -> str | None:
        """Read the meter serial number embedded in the readings card.

        Reflects the session's currently active usage location.
        """
        html = await self._get_text(f"{self._origin()}/cards/readings")
        if self._is_login_page(html):
            raise SessionExpired("WRM session expired")
        m = _SERIAL_RE.search(html)
        return m.group(1) if m else None

    async def discover_meters(self) -> list[dict]:
        """Enumerate the meters this login can access.

        Returns a list of {serial, location_id, location_name}. For a
        single-location account there is no selector, so location_id/name are
        None. Best-effort: multi-location is derived from the usage-location
        <select> and per-location readings card.
        """
        html = await self._get_text(self._url("/"))
        if self._is_login_page(html):
            raise SessionExpired("WRM session expired")

        options: list[tuple[str, str]] = []
        sel = _SELECT_RE.search(html)
        if sel:
            for loc_id, label in _OPTION_RE.findall(sel.group(1)):
                name = re.sub(r"<[^>]+>", "", label).strip()
                options.append((loc_id, name))

        meters: list[dict] = []
        seen: set[str] = set()
        if options:
            for loc_id, name in options:
                await self.set_location(loc_id)
                serial = await self.discover_serial()
                if serial and serial not in seen:
                    seen.add(serial)
                    meters.append(
                        {
                            "serial": serial,
                            "location_id": loc_id,
                            "location_name": name or None,
                        }
                    )
        else:
            serial = await self.discover_serial()
            if serial:
                meters.append(
                    {"serial": serial, "location_id": None, "location_name": None}
                )
        return meters

    async def get_readings(
        self,
        serial_number: str | None = None,
        start_date: str = "2000-01-01",
        end_date: str = "2100-01-01",
    ) -> tuple[str, list[dict]]:
        """Meter readings for a date range (yyyy-MM-dd).

        Returns (serial_number, rows) oldest-first; each row is
        {timestamp, reading_m3, consumption_m3, epoch}. The endpoint returns the
        whole range in one response and needs a date range to return data.
        Raises SessionExpired if the cookie session is no longer valid.
        """
        await self.verify_session()
        if serial_number is None:
            serial_number = await self.discover_serial()
            if not serial_number:
                raise WrmError("Could not discover meter serial number")
        async with self._session.get(
            f"{self._origin()}/data/readings",
            params={
                "serialNumber": serial_number,
                "startDate": start_date,
                "endDate": end_date,
            },
            headers=self._headers,
            timeout=_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            raw = await resp.json(content_type=None) or []  # newest-first/null
        rows = [
            {
                "timestamp": r[0],
                "reading_m3": r[1],
                "consumption_m3": r[2],
                "epoch": r[3],
            }
            for r in raw
        ]
        rows.reverse()  # oldest-first
        return serial_number, rows

    # Hourly graph data is scaled to litres (unit "l/h") via multiplier 1000;
    # daily/monthly/yearly stay in m3. The API's unitText follows that scale, so
    # the multiplier must match or values are 1000x off their own unit label.
    _PERIOD_SCALE = {
        "hourly": (1000, 0),  # (multiplier, decimals)
        "daily": (1, 3),
        "monthly": (1, 3),
        "yearly": (1, 3),
    }

    async def graphdata(
        self,
        period: str = "daily",
        start: str | None = None,
        end: str | None = None,
        average: bool = False,
        decimals: int | None = None,
        multiplier: int | None = None,
    ) -> dict:
        """Bucketed consumption series from /graphdata/alltime.

        period: hourly | daily | monthly | yearly; start/end as 'd.M.yyyy'.
        multiplier/decimals default per period to match the portal's scaling.
        Returns the parsed JSON {status, unitText, decimals, series, ...}.
        """
        def_mult, def_dec = self._PERIOD_SCALE.get(period, (1, 3))
        if multiplier is None:
            multiplier = def_mult
        if decimals is None:
            decimals = def_dec
        await self.verify_session()
        params = {
            "type": period,
            "startTs": start or "",
            "endTs": end or "",
            "decimals": str(decimals),
            "multiplier": str(multiplier),
        }
        if average:
            params["average"] = "true"
        async with self._session.get(
            f"{self._origin()}/graphdata/alltime",
            params=params,
            headers=self._headers,
            timeout=_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if not data or data.get("status", 1) <= 0:
            note = (data or {}).get("note") or (data or {}).get("errorMessage")
            raise WrmError(f"graphdata returned no data: {note or 'empty'}")
        return data

    @staticmethod
    def flatten_series(data: dict) -> list[dict]:
        """Flatten /graphdata series into rows {timestamp, value, unit}.

        Each datum is [epoch_ms, value, iso_timestamp]; a null value means no
        reading for that bucket.
        """
        unit = data.get("unitText", "")
        rows: list[dict] = []
        for series in data.get("series", []):
            for point in series.get("data", []):
                rows.append(
                    {
                        "timestamp": point[2] if len(point) > 2 else None,
                        "value": point[1] if len(point) > 1 else None,
                        "unit": unit,
                    }
                )
        return rows

    async def latest_reading(self, serial_number: str | None = None) -> dict:
        """The most recent cumulative meter reading.

        Tries a short recent window first, widening if the meter has not
        transmitted lately.
        """
        today = date.today()
        windows = [
            today - timedelta(days=14),
            today - timedelta(days=120),
            date(2000, 1, 1),
        ]
        serial = serial_number
        for start in windows:
            serial, rows = await self.get_readings(
                serial, start_date=start.isoformat()
            )
            if rows:
                latest = dict(rows[-1])
                latest["serial_number"] = serial
                return latest
        raise WrmError("No readings available")
