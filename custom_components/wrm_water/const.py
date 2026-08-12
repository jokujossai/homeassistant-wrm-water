"""Constants for the WRM Water Consumption integration."""

DOMAIN = "wrm_water"

CONF_BASE_URL = "base_url"
CONF_SERIAL = "serial_number"
CONF_METERS = "meters"
CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"
# Per-serial epoch of the newest reading imported into statistics.
CONF_LAST_IMPORTED = "last_imported"

# Portal base URL: WRM host + the utility's path segment. Users must change the
# "site" segment to their own utility, e.g. https://wmd.wrm-systems.fi/<utility>.
DEFAULT_BASE_URL = "https://wmd.wrm-systems.fi/site"
DEFAULT_SCAN_INTERVAL_HOURS = 2

# Service to backfill historical readings into long-term statistics.
SERVICE_IMPORT_HISTORY = "import_history"
ATTR_START = "start"
ATTR_END = "end"
ATTR_RESET = "reset"
