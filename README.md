# WRM Water Consumption

Home Assistant integration for water meters available through a WRM Systems
KulutusWeb portal, such as `https://wmd.wrm-systems.fi/<utility>`.

> This is an unofficial, reverse-engineered integration. It is not affiliated
> with or endorsed by WRM Systems or any water utility.

## Features

- Creates an Energy dashboard compatible cumulative water sensor for each
  discovered meter.
- Supports multiple meters and usage locations on one account.
- Imports historical readings into Home Assistant long-term statistics with
  the `wrm_water.import_history` action.
- Requires Home Assistant 2024.4.0 or newer.

## Installation

### HACS

1. In HACS, open **Custom repositories** from the top-right menu.
2. Add this repository as an **Integration**.
3. Install **WRM Water Consumption** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration**, select
   **WRM Water Consumption**, and enter your portal credentials.

### Manual

Copy `custom_components/wrm_water/` to
`config/custom_components/wrm_water/`, restart Home Assistant, and add the
integration from **Settings → Devices & services**.

The password is used only during sign-in and is not stored. Home Assistant
stores the portal session cookies and requests the password again if the
session expires.

## Importing history

Once the meter sensors exist, call:

```yaml
action: wrm_water.import_history
# Optional date range:
data:
  start: "2023-10-24"
  end: "2026-06-28"
```

Omit `data` to import all available history. Re-running the action is safe: it
updates the same hourly statistics. Add the meter sensor as a **Water** source
in the Energy dashboard after the import.

## Command-line client

The integration also includes a standalone client for testing and exporting
readings:

```sh
cd custom_components/wrm_water
python3 __main__.py login
python3 __main__.py whoami
python3 __main__.py meters
python3 __main__.py readings -o readings.csv
python3 __main__.py consumption --type monthly
```

Session cookies are stored in `~/.config/wrm-water/cookies.json`. Use
`--cookies PATH` or `--base URL` to override the defaults. The client requires
`aiohttp` and `yarl`.

## Notes

- Home Assistant must be able to reach the utility's portal.
- Each login is one config entry and may expose multiple meters.
- Multi-location discovery is implemented but has not been verified against a
  real multi-location account.
- Portal changes may break the integration. Please report problems through
  GitHub Issues without attaching credentials, session cookies, or personal
  meter data.

## Development

Run the local test suite with:

```sh
./run-tests.sh
```

GitHub Actions also runs the Home Assistant integration tests against the
minimum supported and latest Home Assistant versions.
