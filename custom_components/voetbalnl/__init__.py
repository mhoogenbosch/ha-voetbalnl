"""The Voetbal.nl integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from .api import (
    VoetbalNLApi,
    VoetbalNLAuthError,
    VoetbalNLConnectionError,
    normalize_team_url,
)
from .const import (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TEAM_NAME,
    CONF_TEAM_URL,
    DATA_API,
    DATA_COORDINATOR,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import VoetbalNLCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Voetbal.nl config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    if not username or not password:
        raise ConfigEntryAuthFailed(
            "No credentials configured. Please re-add the integration."
        )
    team_name = entry.data[CONF_TEAM_NAME]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    # The options override the team URL only when filled in: a cleared
    # options field ("") must fall back to the URL from setup.
    raw_url = entry.options.get(CONF_TEAM_URL) or entry.data[CONF_TEAM_URL]
    team_url = normalize_team_url(raw_url)
    if not team_url:
        raise ConfigEntryError(f"Not a voetbal.nl team URL: {raw_url}")

    api = VoetbalNLApi()

    try:
        await api.login(username, password)
    except VoetbalNLAuthError as err:
        await api.close()
        raise ConfigEntryAuthFailed(
            f"Invalid credentials for voetbal.nl: {err}"
        ) from err
    except VoetbalNLConnectionError as err:
        await api.close()
        raise ConfigEntryNotReady(
            f"Cannot connect to voetbal.nl: {err}"
        ) from err

    coordinator = VoetbalNLCoordinator(
        hass,
        api=api,
        team_name=team_name,
        team_url=team_url,
        scan_interval=int(scan_interval),
    )

    # Do an initial data fetch; raises ConfigEntryNotReady on failure. Close
    # the session on any failure, or every setup retry leaks one.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await api.close()
        raise

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_API: api,
        DATA_COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when options change
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Voetbal.nl config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        api: VoetbalNLApi | None = entry_data.get(DATA_API)
        if api:
            await api.close()

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
