"""Calendar entity for the Voetbal.nl integration.

Each configured team gets one calendar that shows every match on both the
schedule (/programma/) and results (/uitslagen/) pages.  Upcoming matches
are shown as timed events; matches where no kick-off time is available are
shown as all-day events.  The `event` property always returns the next
match that has not yet ended.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MatchData, VoetbalNLApi
from .const import CONF_TEAM_NAME, DATA_COORDINATOR, DOMAIN, VERSION
from .coordinator import VoetbalNLCoordinator

_LOGGER = logging.getLogger(__name__)

# Typical futsal match duration in minutes
_MATCH_DURATION_MINUTES = 90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a Voetbal.nl calendar from a config entry."""
    coordinator: VoetbalNLCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    team_name = entry.data[CONF_TEAM_NAME]
    async_add_entities(
        [VoetbalNLCalendar(coordinator, team_name, entry.entry_id)]
    )


def _event_uid(match: MatchData) -> str | None:
    """Return a stable UID for a match.

    Consumers that mirror this calendar elsewhere (sync scripts, other
    integrations) key on the event UID, so every match needs its own stable
    one. Preferred source is the match ID in the voetbal.nl URL
    (``/wedstrijd/M79726132/programma``); otherwise a slug of date and teams.
    """
    found = re.search(r"/wedstrijd/(M\d+)", match.match_url or "")
    if found:
        return f"voetbalnl-{found.group(1)}"

    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        f"{match.date}-{match.home_team}-{match.away_team}".lower(),
    ).strip("-")
    return f"voetbalnl-{slug}" if slug else None


def _match_summary(match: MatchData) -> str:
    """Return the event title for a match.

    When voetbal.nl told us which side we are on (the "my-team" marker), the
    title names the side and the opponent — ``Uit: SJO JOY 26 O17-2`` — which
    reads better in a personal calendar than both team names. Without that
    marker we cannot tell the sides apart, so both teams are listed.
    """
    if match.my_team_is_home is None:
        summary = f"{match.home_team} vs {match.away_team}"
    else:
        opponent = match.away_team if match.my_team_is_home else match.home_team
        side = "Thuis" if match.my_team_is_home else "Uit"
        summary = f"{side}: {opponent}"

    if match.is_played and match.home_score is not None and match.away_score is not None:
        summary += f" ({match.home_score}-{match.away_score})"

    return summary


def _match_to_event(match: MatchData) -> CalendarEvent | None:
    """Convert a :class:`MatchData` to a HA :class:`CalendarEvent`.

    Returns ``None`` when the match date cannot be parsed.
    """
    if not match.home_team and not match.away_team:
        return None

    parsed = VoetbalNLApi._parse_match_datetime(match.date, match.time)
    if parsed is None:
        return None

    summary = _match_summary(match)

    # Build a description from available metadata. The full fixture goes in
    # first: the title only names the opponent, so this keeps both team names
    # available on the event itself.
    desc_parts: list[str] = []
    if match.home_team and match.away_team and match.my_team_is_home is not None:
        desc_parts.append(f"{match.home_team} – {match.away_team}")
    if match.competition:
        desc_parts.append(match.competition)
    if match.is_played and match.home_score is not None and match.away_score is not None:
        desc_parts.append(f"Result: {match.home_score}–{match.away_score}")

    description = "\n".join(desc_parts) if desc_parts else None
    location = match.location if match.location else None

    uid = _event_uid(match)

    if isinstance(parsed, datetime):
        # Timed event — make timezone-aware using the HA local timezone
        start_dt: datetime = dt_util.as_local(parsed)
        end_dt: datetime = start_dt + timedelta(minutes=_MATCH_DURATION_MINUTES)
        return CalendarEvent(
            summary=summary,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
            uid=uid,
        )

    # All-day event — end must be the day *after* the event (iCal spec)
    start_date: date = parsed
    end_date: date = start_date + timedelta(days=1)
    return CalendarEvent(
        summary=summary,
        start=start_date,
        end=end_date,
        description=description,
        location=location,
        uid=uid,
    )


def _event_start_as_datetime(evt: CalendarEvent) -> datetime:
    """Return the event start as a timezone-aware datetime for sorting/comparison."""
    start = evt.start
    if isinstance(start, datetime):
        return start if start.tzinfo else dt_util.as_local(start)
    # date-only → treat as midnight local time
    return dt_util.as_local(datetime.combine(start, datetime.min.time()))


class VoetbalNLCalendar(CoordinatorEntity[VoetbalNLCoordinator], CalendarEntity):
    """A calendar showing all scheduled and played matches for a voetbal.nl team."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VoetbalNLCoordinator,
        team_name: str,
        entry_id: str,
    ) -> None:
        """Initialise the calendar entity."""
        super().__init__(coordinator)
        self._team_name = team_name
        self._attr_unique_id = f"{entry_id}_calendar"
        self._attr_name = "Matches"
        self._attr_device_info: dict[str, Any] = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": f"Voetbal.nl – {team_name}",
            "manufacturer": "Voetbal.nl / KNVB",
            "model": "Futsal Team Tracker",
            "sw_version": VERSION,
        }

    # ------------------------------------------------------------------
    # CalendarEntity contract
    # ------------------------------------------------------------------

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming match (or the ongoing one if in progress)."""
        if not self.coordinator.data:
            return None

        now = dt_util.now()
        events = self._sorted_events()

        for evt in events:
            end = evt.end
            if isinstance(end, datetime):
                end_dt = end if end.tzinfo else dt_util.as_local(end)
            else:
                end_dt = dt_util.as_local(
                    datetime.combine(end, datetime.min.time())
                )
            if end_dt > now:
                return evt

        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all matches that overlap with the requested date range."""
        if not self.coordinator.data:
            return []

        result: list[CalendarEvent] = []
        for evt in self._sorted_events():
            evt_start = _event_start_as_datetime(evt)

            end = evt.end
            if isinstance(end, datetime):
                evt_end = end if end.tzinfo else dt_util.as_local(end)
            else:
                evt_end = dt_util.as_local(datetime.combine(end, datetime.min.time()))

            # Include event if it overlaps with [start_date, end_date)
            if evt_start < end_date and evt_end > start_date:
                result.append(evt)

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sorted_events(self) -> list[CalendarEvent]:
        """Return all matches as CalendarEvents sorted chronologically."""
        all_matches: list[MatchData] = (
            self.coordinator.data.get("all_matches", [])
            if self.coordinator.data
            else []
        )
        events: list[CalendarEvent] = []
        for match in all_matches:
            evt = _match_to_event(match)
            if evt is not None:
                events.append(evt)

        events.sort(key=_event_start_as_datetime)
        return events
