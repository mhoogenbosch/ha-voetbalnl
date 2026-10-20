"""Sensors for the Voetbal.nl integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_TEAM_NAME,
    DATA_COORDINATOR,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    SENSOR_ICONS,
    SENSOR_LAST_RESULT,
    SENSOR_NEXT_MATCH,
    SENSOR_STANDING,
    VERSION,
)
from .coordinator import VoetbalNLCoordinator

_LOGGER = logging.getLogger(__name__)

# Shown in the "Last Result" state; the "result" attribute keeps the English
# key (win/loss/draw/unknown) so automations do not depend on the wording.
_RESULT_LABELS = {
    "win": "winst",
    "loss": "verlies",
    "draw": "gelijk",
    "unknown": "onbekend",
}


@dataclass(frozen=True, kw_only=True)
class VoetbalNLSensorDescription(SensorEntityDescription):
    """Describes a Voetbal.nl sensor."""


SENSOR_DESCRIPTIONS: tuple[VoetbalNLSensorDescription, ...] = (
    VoetbalNLSensorDescription(
        key=SENSOR_STANDING,
        translation_key=SENSOR_STANDING,
        icon=SENSOR_ICONS[SENSOR_STANDING],
        native_unit_of_measurement=None,
    ),
    VoetbalNLSensorDescription(
        key=SENSOR_NEXT_MATCH,
        translation_key=SENSOR_NEXT_MATCH,
        icon=SENSOR_ICONS[SENSOR_NEXT_MATCH],
        native_unit_of_measurement=None,
    ),
    VoetbalNLSensorDescription(
        key=SENSOR_LAST_RESULT,
        translation_key=SENSOR_LAST_RESULT,
        icon=SENSOR_ICONS[SENSOR_LAST_RESULT],
        native_unit_of_measurement=None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Voetbal.nl sensors from a config entry."""
    coordinator: VoetbalNLCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    team_name = entry.data[CONF_TEAM_NAME]

    entities = [
        VoetbalNLSensor(coordinator, description, team_name, entry.entry_id)
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class VoetbalNLSensor(CoordinatorEntity[VoetbalNLCoordinator], SensorEntity):
    """A sensor representing a data point for a voetbal.nl team."""

    entity_description: VoetbalNLSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VoetbalNLCoordinator,
        description: VoetbalNLSensorDescription,
        team_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._team_name = team_name
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": f"Voetbal.nl – {team_name}",
            "manufacturer": DEVICE_MANUFACTURER,
            "model": DEVICE_MODEL,
            "sw_version": VERSION,
        }

    @property
    def native_value(self) -> str | int | None:
        """Return the primary state value for this sensor."""
        if self.coordinator.data is None:
            return None

        key = self.entity_description.key
        data = self.coordinator.data

        if key == SENSOR_STANDING:
            return self._standing_value(data)
        if key == SENSOR_NEXT_MATCH:
            return self._next_match_value(data)
        if key == SENSOR_LAST_RESULT:
            return self._last_result_value(data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed attributes for this sensor."""
        if self.coordinator.data is None:
            return {}

        key = self.entity_description.key
        data = self.coordinator.data

        if key == SENSOR_STANDING:
            return self._standing_attributes(data)
        if key == SENSOR_NEXT_MATCH:
            return self._next_match_attributes(data)
        if key == SENSOR_LAST_RESULT:
            return self._last_result_attributes(data)
        return {}

    # ------------------------------------------------------------------ #
    # Team-name / side helpers                                             #
    # ------------------------------------------------------------------ #

    def _resolved_team_name(self, data: dict[str, Any]) -> str:
        """Return the team name as voetbal.nl spells it, if we resolved it.

        The configured name (e.g. "VVG 17-01") often differs from the name on
        voetbal.nl (e.g. "VVG '25 O17-1"); the coordinator resolves it from
        the standings page.
        """
        return data.get("team_name") or self._team_name

    @staticmethod
    def _is_home_game(match: Any, team_name: str) -> bool:
        """Return True if our team plays at home in *match*."""
        flag = getattr(match, "my_team_is_home", None)
        if flag is not None:
            return bool(flag)
        return team_name.lower() in (getattr(match, "home_team", "") or "").lower()

    # ------------------------------------------------------------------ #
    # Standing sensor helpers                                              #
    # ------------------------------------------------------------------ #

    def _standing_value(self, data: dict[str, Any]) -> int | str | None:
        """Return the team's current league position."""
        standing = data.get(SENSOR_STANDING)
        if standing is None:
            return None
        entry = getattr(standing, "team_entry", None)
        if entry:
            return entry.position
        pos = getattr(standing, "team_position", None)
        return pos if pos else None

    def _standing_attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return full standings attributes."""
        standing = data.get(SENSOR_STANDING)
        if not standing:
            return {}

        attrs: dict[str, Any] = {
            "team": self._resolved_team_name(data),
            "league": getattr(standing, "league_name", ""),
        }

        entry = getattr(standing, "team_entry", None)
        if entry:
            attrs.update(
                {
                    "position": entry.position,
                    "games_played": entry.games_played,
                    "won": entry.won,
                    "drawn": entry.drawn,
                    "lost": entry.lost,
                    "goals_for": entry.goals_for,
                    "goals_against": entry.goals_against,
                    "goal_difference": entry.goal_difference,
                    "points": entry.points,
                }
            )

        # Include full standings table as list of dicts
        entries = getattr(standing, "entries", [])
        if entries:
            attrs["full_standings"] = [
                {
                    "position": e.position,
                    "team": e.team_name,
                    "played": e.games_played,
                    "won": e.won,
                    "drawn": e.drawn,
                    "lost": e.lost,
                    "gf": e.goals_for,
                    "ga": e.goals_against,
                    "gd": e.goal_difference,
                    "points": e.points,
                }
                for e in entries
            ]

        return attrs

    # ------------------------------------------------------------------ #
    # Next match sensor helpers                                            #
    # ------------------------------------------------------------------ #

    def _next_match_value(self, data: dict[str, Any]) -> str | None:
        """Return a human-readable description of the next match."""
        match = data.get(SENSOR_NEXT_MATCH)
        if not match:
            return "Geen wedstrijden gepland"
        home = getattr(match, "home_team", "")
        away = getattr(match, "away_team", "")
        date = getattr(match, "date", "")
        time = getattr(match, "time", "")
        date_str = f"{date} {time}".strip()
        return f"{home} – {away} ({date_str})" if date_str else f"{home} – {away}"

    def _next_match_attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return full next match attributes."""
        match = data.get(SENSOR_NEXT_MATCH)
        if not match:
            return {}

        home_team = getattr(match, "home_team", "")
        away_team = getattr(match, "away_team", "")
        is_home = self._is_home_game(match, self._resolved_team_name(data))

        return {
            "home_team": home_team,
            "away_team": away_team,
            "date": getattr(match, "date", ""),
            "time": getattr(match, "time", ""),
            "competition": getattr(match, "competition", ""),
            "location": getattr(match, "location", ""),
            "match_url": getattr(match, "match_url", ""),
            "is_home_game": is_home,
            "opponent": away_team if is_home else home_team,
        }

    # ------------------------------------------------------------------ #
    # Last result sensor helpers                                           #
    # ------------------------------------------------------------------ #

    def _last_result_value(self, data: dict[str, Any]) -> str | None:
        """Return a human-readable last result string."""
        match = data.get(SENSOR_LAST_RESULT)
        if not match:
            return "Nog geen uitslag"

        home_score = getattr(match, "home_score", None)
        away_score = getattr(match, "away_score", None)
        home = getattr(match, "home_team", "")
        away = getattr(match, "away_team", "")

        if home_score is not None and away_score is not None:
            result = match.result_for_team(self._resolved_team_name(data))
            label = _RESULT_LABELS.get(result, result)
            return f"{home} {home_score}-{away_score} {away} ({label})"

        return f"{home} – {away}"

    def _last_result_attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return full last result attributes."""
        match = data.get(SENSOR_LAST_RESULT)
        if not match:
            return {}

        home_team = getattr(match, "home_team", "")
        away_team = getattr(match, "away_team", "")
        home_score = getattr(match, "home_score", None)
        away_score = getattr(match, "away_score", None)
        resolved_name = self._resolved_team_name(data)
        is_home = self._is_home_game(match, resolved_name)

        attrs: dict[str, Any] = {
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "date": getattr(match, "date", ""),
            "competition": getattr(match, "competition", ""),
            "location": getattr(match, "location", ""),
            "is_home_game": is_home,
            "opponent": away_team if is_home else home_team,
        }

        if home_score is not None and away_score is not None:
            attrs["result"] = match.result_for_team(resolved_name)
            if is_home:
                attrs["goals_scored"] = home_score
                attrs["goals_conceded"] = away_score
            else:
                attrs["goals_scored"] = away_score
                attrs["goals_conceded"] = home_score

        return attrs
