"""Constants for the Voetbal.nl integration."""

DOMAIN = "voetbalnl"
VERSION = "1.1.0-mh.1"

# Configuration keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TEAM_NAME = "team_name"
CONF_TEAM_URL = "team_url"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 30  # minutes
DEFAULT_TIMEOUT = 30  # seconds

# URLs
BASE_URL = "https://www.voetbal.nl"
LOGIN_URL = f"{BASE_URL}/inloggen"
SEARCH_URL = f"{BASE_URL}/zoeken"
PROFILE_URL = f"{BASE_URL}/profiel/overzicht"

# KNVB Account OAuth URLs (used for accounts created via SSO)
# The modal endpoint on knvb.nl returns an iframe URL with a fresh nonce+state
KNVB_MODAL_URL = "https://www.knvb.nl/sportlink_account/modal/login/always_link"
KNVB_OAUTH_HOST = "https://login.knvbaccount.nl"
KNVB_OAUTH_LOGIN_URL = f"{KNVB_OAUTH_HOST}/oauth/login"

# Sensor types
SENSOR_STANDING = "standing"
SENSOR_NEXT_MATCH = "next_match"
SENSOR_LAST_RESULT = "last_result"

# Sensor names
SENSOR_NAMES = {
    SENSOR_STANDING: "Standing",
    SENSOR_NEXT_MATCH: "Next Match",
    SENSOR_LAST_RESULT: "Last Result",
}

# Sensor icons
SENSOR_ICONS = {
    SENSOR_STANDING: "mdi:trophy",
    SENSOR_NEXT_MATCH: "mdi:calendar-clock",
    SENSOR_LAST_RESULT: "mdi:scoreboard",
}

# Data keys
DATA_COORDINATOR = "coordinator"
DATA_API = "api"

# HTTP Headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Error messages
ERROR_AUTH_FAILED = "auth_failed"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_TEAM_NOT_FOUND = "team_not_found"
ERROR_UNKNOWN = "unknown"
