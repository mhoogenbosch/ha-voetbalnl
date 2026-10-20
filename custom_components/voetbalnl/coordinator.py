"""Data update coordinator for Voetbal.nl integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    VoetbalNLApi,
    VoetbalNLAuthError,
    VoetbalNLConnectionError,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class VoetbalNLCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches data for a single followed team."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: VoetbalNLApi,
        team_name: str,
        team_url: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{team_name}",
            update_interval=timedelta(minutes=scan_interval),
        )
        self.api = api
        self.team_name = team_name
        self.team_url = team_url

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch updated data from voetbal.nl for the configured team."""
        try:
            # Re-authenticates automatically if session is old or expired
            await self.api.ensure_authenticated()
            data = await self.api.get_team_data(self.team_url, self.team_name)
        except VoetbalNLAuthError as err:
            # Trigger re-authentication flow in HA
            raise ConfigEntryAuthFailed(
                f"Authentication failed for voetbal.nl: {err}"
            ) from err
        except VoetbalNLConnectionError as err:
            raise UpdateFailed(
                f"Could not connect to voetbal.nl: {err}"
            ) from err
        except Exception as err:  # noqa: BLE001
            # A parser bug must not silently empty the calendar either: keep
            # the previous data, like a connection error does.
            raise UpdateFailed(
                f"Unexpected error fetching team data: {err}"
            ) from err

        _LOGGER.debug(
            "Updated voetbal.nl data for team '%s': standing=%s, next=%s, last=%s",
            self.team_name,
            data.get("standing"),
            data.get("next_match"),
            data.get("last_result"),
        )
        return data
