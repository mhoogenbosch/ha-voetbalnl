"""Voetbal.nl API client - handles authentication and data scraping.

Network I/O runs on the event loop; every BeautifulSoup parse runs in the
executor, because a voetbal.nl page is large enough for html.parser to hold
the loop for tens of milliseconds per page.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    BASE_URL,
    DEFAULT_TIMEOUT,
    HEADERS,
    LOGIN_URL,
    PROFILE_URL,
    VOETBALNL_HOSTS,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Network failures: aiohttp's total timeout raises a bare TimeoutError, which
# is not an aiohttp.ClientError.
_NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError)

# Error text voetbal.nl shows when the credentials are wrong. Only this turns
# a failed login into an auth error (= reauth in HA); anything else (an
# outage such as "Inloggen is momenteel niet mogelijk", a missing message) is
# treated as a connection problem that HA retries by itself.
_CREDENTIALS_ERROR = re.compile(
    r"wachtwoord|password|gebruikersnaam|username|e-?mail|onbekend|ongeldig"
    r"|onjuist|incorrect|unrecognized|invalid",
    re.I,
)
_SERVICE_UNAVAILABLE = re.compile(
    r"momenteel niet|probeer het later|niet mogelijk|storing|onderhoud"
    r"|temporarily|try again later",
    re.I,
)

_TEAM_PATH = re.compile(r"^/team/(T\d+)(?:/[a-z-]*)?/?$")


@dataclass
class TeamInfo:
    """Basic information about a team."""

    name: str
    url: str
    team_id: str = ""


@dataclass
class StandingEntry:
    """A single row in the standings table."""

    position: int
    team_name: str
    games_played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    goals_for: int = 0
    goals_against: int = 0
    goal_difference: int = 0
    points: int = 0


@dataclass
class StandingData:
    """Full standings data for a team's competition."""

    league_name: str = ""
    team_position: int = 0
    team_name: str = ""
    entries: list[StandingEntry] = field(default_factory=list)
    team_entry: StandingEntry | None = None


@dataclass
class MatchData:
    """Data for a single match."""

    date: str = ""
    time: str = ""
    home_team: str = ""
    away_team: str = ""
    home_score: int | None = None
    away_score: int | None = None
    competition: str = ""
    location: str = ""
    match_url: str = ""
    is_played: bool = False
    # True/False when voetbal.nl marked this row with the "my-team" class
    # (only available for authenticated sessions); None when unknown.
    my_team_is_home: bool | None = None

    @property
    def is_my_team(self) -> bool:
        """Return True if voetbal.nl marked this match as involving my team."""
        return self.my_team_is_home is not None

    def result_for_team(self, team_name: str) -> str:
        """Return "win", "loss", "draw" or "unknown" for our team."""
        if self.home_score is None or self.away_score is None:
            return "unknown"
        if self.my_team_is_home is not None:
            # Authoritative "my-team" marker from voetbal.nl
            we_are_home = self.my_team_is_home
        else:
            normalized = team_name.lower().strip()
            home_normalized = self.home_team.lower().strip()
            we_are_home = normalized in home_normalized or home_normalized in normalized
        ours, theirs = (
            (self.home_score, self.away_score)
            if we_are_home
            else (self.away_score, self.home_score)
        )
        if ours > theirs:
            return "win"
        if ours < theirs:
            return "loss"
        return "draw"


class VoetbalNLAuthError(Exception):
    """Raised when voetbal.nl rejects the credentials."""


class VoetbalNLConnectionError(Exception):
    """Raised when voetbal.nl cannot be reached or answers unexpectedly."""


def absolute_voetbalnl_url(href: str) -> str | None:
    """Return *href* as an absolute https URL on voetbal.nl, or None.

    Relative paths are resolved against voetbal.nl. Absolute URLs are only
    accepted when they point at voetbal.nl itself, so a URL taken from a page
    or typed by a user can never send this client (and its session) to
    another host.
    """
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("/") and not href.startswith("//"):
        return f"{BASE_URL}{href}"
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https") or parts.hostname not in VOETBALNL_HOSTS:
        return None
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{BASE_URL}{path}{query}"


def normalize_team_url(url: str) -> str | None:
    """Return the canonical team URL, or None when *url* is not a team page.

    Accepts ``https://www.voetbal.nl/team/T123/overzicht`` and its sub-pages
    (``/programma``, ``/stand``, ...), with or without ``www.`` or ``https``.
    """
    url = (url or "").strip()
    if url and "://" not in url and not url.startswith("/"):
        url = f"https://{url}"
    absolute = absolute_voetbalnl_url(url)
    if not absolute:
        return None
    found = _TEAM_PATH.match(urlsplit(absolute).path)
    if not found:
        return None
    return f"{BASE_URL}/team/{found.group(1)}/overzicht"


class VoetbalNLApi:
    """Client for voetbal.nl that handles form-based login and data retrieval."""

    # Proactively re-login after this many days to keep the session fresh
    SESSION_REFRESH_DAYS = 6

    def __init__(self) -> None:
        """Initialize the API client."""
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self._login_time: datetime | None = None
        self._username: str = ""
        self._password: str = ""
        # Last known venue per match URL, used when a detail page fails to load
        self._location_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Plumbing                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _run(func: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking parse function in the executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args))

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session with cookie support."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=True)
            timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=HEADERS,
                cookie_jar=aiohttp.CookieJar(),
            )
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._authenticated = False
        self._login_time = None

    async def _get_text(self, url: str, label: str) -> str:
        """GET *url* and return the body; raise on anything but HTTP 200."""
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"{label} page returned HTTP {resp.status}"
                    )
                return await resp.text()
        except _NETWORK_ERRORS as err:
            raise VoetbalNLConnectionError(
                f"Error fetching {label} page: {err!r}"
            ) from err

    # ------------------------------------------------------------------ #
    # Authentication                                                       #
    # ------------------------------------------------------------------ #

    async def login(self, username: str, password: str) -> bool:
        """Log in to voetbal.nl with the native Drupal form (email + password).

        reCAPTCHA is enforced client-side only and does not block server-side
        auth. voetbal.nl offers no OAuth redirect for third-party clients, so
        the form is the only path.

        Raises VoetbalNLAuthError when voetbal.nl rejects the credentials.
        Raises VoetbalNLConnectionError on network failure or an outage.
        """
        self._username = username
        self._password = password
        session = await self._get_session()

        # Step 1 – load the login page to get the CSRF form tokens
        try:
            async with session.get(LOGIN_URL, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"Login page returned HTTP {resp.status}"
                    )
                html = await resp.text()
        except _NETWORK_ERRORS as err:
            raise VoetbalNLConnectionError(
                f"Cannot reach voetbal.nl login page: {err!r}"
            ) from err

        form = await self._run(self._parse_login_form, html)
        if form is None:
            raise VoetbalNLConnectionError(
                "Could not locate the login form on voetbal.nl"
            )
        action, form_data = form
        form_data["email"] = username
        form_data["password"] = password

        # Step 2 – submit the credentials
        try:
            async with session.post(
                action, data=form_data, allow_redirects=True
            ) as resp:
                if resp.status not in (200, 302):
                    raise VoetbalNLConnectionError(
                        f"Login POST returned HTTP {resp.status}"
                    )
                html_after = await resp.text()
        except _NETWORK_ERRORS as err:
            raise VoetbalNLConnectionError(
                f"Network error during login POST: {err!r}"
            ) from err

        # Step 3 – verify, and tell wrong credentials apart from an outage
        if not await self._is_authenticated(session):
            message = await self._run(self._parse_login_error, html_after)
            if message and _CREDENTIALS_ERROR.search(message) and not (
                _SERVICE_UNAVAILABLE.search(message)
            ):
                raise VoetbalNLAuthError(
                    f"voetbal.nl rejected the credentials: {message}"
                )
            raise VoetbalNLConnectionError(
                "Login did not result in a session"
                + (f": {message}" if message else " (no error message shown)")
            )

        self._authenticated = True
        self._login_time = datetime.now()
        _LOGGER.debug("Successfully logged in to voetbal.nl")
        return True

    @staticmethod
    def _parse_login_form(html: str) -> tuple[str, dict[str, str]] | None:
        """Return (action URL, hidden fields + op) of the login form."""
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id="login-form") or soup.find(
            "form", {"action": re.compile(r"inloggen", re.I)}
        )
        if not form:
            return None

        # Hidden fields (form_build_id, form_id, …)
        form_data: dict[str, str] = {}
        for inp in form.find_all("input", type="hidden"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")

        # Submit button value (Drupal expects the op field)
        submit_btn = form.find("button", attrs={"name": "op"}) or form.find(
            "input", attrs={"name": "op"}
        )
        if submit_btn:
            form_data["op"] = submit_btn.get("value", "Inloggen")

        action = absolute_voetbalnl_url(form.get("action") or "/inloggen")
        return (action or LOGIN_URL), form_data

    @staticmethod
    def _parse_login_error(html: str) -> str:
        """Return the error message voetbal.nl shows after a failed login."""
        soup = BeautifulSoup(html, "html.parser")
        error_el = soup.find(
            class_=re.compile(r"messages?.*error|alert.*error|form.*error", re.I)
        )
        return error_el.get_text(" ", strip=True)[:300] if error_el else ""

    async def ensure_authenticated(self) -> None:
        """Ensure the session is still valid; re-login automatically if needed.

        Called before every data fetch. Re-authenticates when:
        - The session has never been authenticated
        - The session is older than SESSION_REFRESH_DAYS days
        - The session cookie has expired (verified by a profile page check)
        """
        if not self._username:
            raise VoetbalNLAuthError("No credentials stored — call login() first.")

        # Proactive refresh: re-login shortly before the Drupal session expires
        if self._login_time is not None:
            age = datetime.now() - self._login_time
            if age >= timedelta(days=self.SESSION_REFRESH_DAYS):
                _LOGGER.debug(
                    "Session is %d days old — proactively refreshing login", age.days
                )
                await self.login(self._username, self._password)
                return

        if not self._authenticated or not self._session:
            await self.login(self._username, self._password)
            return

        # Reactive check: verify the cookie still works
        session = await self._get_session()
        if not await self._is_authenticated(session):
            _LOGGER.debug("Session cookie expired — re-logging in")
            await self.login(self._username, self._password)

    async def _is_authenticated(self, session: aiohttp.ClientSession) -> bool:
        """Return True if the session is logged in, False if it is not.

        A redirect to /inloggen means not logged in. A network error or an
        unexpected HTTP status says nothing about the session, so it raises
        VoetbalNLConnectionError instead of reporting "not logged in" (which
        would end up as a bogus reauth request).
        """
        try:
            async with session.get(PROFILE_URL, allow_redirects=True) as resp:
                if "inloggen" in str(resp.url):
                    return False
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"Profile page returned HTTP {resp.status}"
                    )
                html = await resp.text()
        except _NETWORK_ERRORS as err:
            raise VoetbalNLConnectionError(
                f"Cannot verify the voetbal.nl session: {err!r}"
            ) from err
        return await self._run(self._page_is_logged_in, html)

    @staticmethod
    def _page_is_logged_in(html: str) -> bool:
        """Return True if *html* is a page rendered for a logged-in user."""
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body")
        if body and "logged-in" in (body.get("class") or []):
            return True
        # voetbal.nl shows a logout link for authenticated sessions
        return bool(soup.find("a", href=re.compile(r"uitloggen|logout", re.I)))

    # ------------------------------------------------------------------ #
    # Team search                                                          #
    # ------------------------------------------------------------------ #

    async def search_team(self, team_name: str) -> list[TeamInfo]:
        """Search for a team by name using the voetbal.nl JSON search API.

        Endpoint: GET /zoeken/json?page=voetbal&keywords={term}
        Returns items where href contains /team/ (team pages).
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{BASE_URL}/zoeken/json",
                params={"page": "voetbal", "keywords": team_name},
            ) as response:
                if response.status != 200:
                    _LOGGER.warning("Search returned HTTP %s", response.status)
                    return []
                data = await response.json(content_type=None)
        except (*_NETWORK_ERRORS, ValueError) as err:
            raise VoetbalNLConnectionError(
                f"Connection error during team search: {err!r}"
            ) from err

        return self._parse_team_search_results(data, team_name)

    @staticmethod
    def _parse_team_search_results(data: Any, search_term: str) -> list[TeamInfo]:
        """Parse team search results from the JSON search API response."""
        teams: list[TeamInfo] = []
        if not isinstance(data, dict):
            return teams

        for item in data.get("results", {}).get("items", []):
            href = item.get("href", "")
            title = item.get("title", "")
            if not ("/team/" in href or item.get("type", "") == "team"):
                continue
            team_url = normalize_team_url(href)
            if not team_url or not title:
                continue
            id_match = re.search(r"/team/(T\d+)", team_url)
            teams.append(
                TeamInfo(
                    name=title,
                    url=team_url,
                    team_id=id_match.group(1) if id_match else "",
                )
            )

        _LOGGER.debug("Found %d team(s) matching '%s'", len(teams), search_term)
        return teams

    # ------------------------------------------------------------------ #
    # Team data                                                            #
    # ------------------------------------------------------------------ #

    async def get_team_data(self, team_url: str, team_name: str) -> dict[str, Any]:
        """Fetch all data for a team: standings, next match, last result.

        Raises VoetbalNLConnectionError when any page fails to load, so the
        coordinator keeps the previous data instead of publishing a partial
        or empty schedule (which a calendar mirror would read as deletions).
        """
        standing, next_match, last_result, all_matches = await self._fetch_team_all(
            team_url, team_name
        )
        # The standings page tells us the exact name voetbal.nl uses for our
        # team (e.g. "VVG '25 O17-1" for a config entry named "VVG 17-01").
        resolved_name = (standing.team_name if standing else "") or team_name
        return {
            "standing": standing,
            "next_match": next_match,
            "last_result": last_result,
            "all_matches": all_matches,
            "team_name": resolved_name,
        }

    async def _fetch_team_all(
        self, team_url: str, team_name: str
    ) -> tuple[StandingData | None, MatchData | None, MatchData | None, list[MatchData]]:
        """Fetch standings, schedule and results."""
        canonical = normalize_team_url(team_url)
        if not canonical:
            raise VoetbalNLConnectionError(f"Not a voetbal.nl team URL: {team_url}")
        base = canonical.removesuffix("/overzicht")
        # Team ID (e.g. "T2070245537"); recognises our own standings row
        # regardless of how the team is named.
        team_id = base.rsplit("/", 1)[-1]

        html = await self._get_text(f"{base}/stand", "standing")
        standing_data = await self._run(
            self._parse_standings, html, team_name, team_id
        )
        # Match on the canonical voetbal.nl name once the standings know it
        effective_name = standing_data.team_name or team_name
        if standing_data.team_name:
            _LOGGER.debug(
                "Resolved configured team '%s' to voetbal.nl team '%s'",
                team_name,
                effective_name,
            )

        next_match: MatchData | None = None
        last_result: MatchData | None = None
        all_matches: list[MatchData] = []

        for page, played in (("programma", False), ("uitslagen", True)):
            label = "results" if played else "schedule"
            html = await self._get_text(f"{base}/{page}", label)
            rows, ajax_url = await self._run(self._parse_schedule_page, html, played)
            # The page only renders the next round; the rest sits behind "Toon
            # volledig programma", loaded from an ajax endpoint named in the
            # page. Merge it in so the calendar holds the whole schedule.
            if ajax_url:
                extra_html = await self._fetch_schedule_ajax(ajax_url, label)
                if extra_html:
                    rows.extend(
                        await self._run(self._parse_match_html, extra_html, played)
                    )
            rows = self._dedupe_matches(rows)
            picked = self._pick_match(rows, effective_name)
            if played:
                last_result = picked
            else:
                next_match = picked
            all_matches.extend(
                m for m in rows if self._match_involves_team(m, effective_name)
            )

        # Enrich next match and last result with the venue from their detail pages
        for match in (next_match, last_result):
            if match and match.match_url and not match.location:
                match.location = await self.get_match_location(match.match_url)

        return standing_data, next_match, last_result, all_matches

    @staticmethod
    def _parse_standings(html: str, team_name: str, team_id: str = "") -> StandingData:
        """
        Parse standings from a team's /stand page on voetbal.nl.

        Voetbal.nl uses <a class="row"> elements instead of a <table>.
        Each team row has 11 .value divs in this order:
          0: position  1: logo (empty text)  2: team name
          3: G (played)  4: W (won)  5: GL (drawn)  6: V (lost)
          7: P (points)  8: + (goals for)  9: - (goals against)  10: PM

        Example row:
            <a href="/team/T98386305/overzicht" class="row">
                <div class="value position"><span>1</span></div>
                <div class="value logo">...</div>
                <div class="value team"><span>KDO sv VR2</span></div>
                <div class="value"><span>25</span></div>  <!-- G -->
                ...
                <div class="value points"><span>63</span></div>  <!-- P -->
                ...
            </a>
        """
        soup = BeautifulSoup(html, "html.parser")
        data = StandingData()

        # Try to find the league / poule name
        for selector in [
            # voetbal.nl page header: <h1>team</h1><h3>competition</h3>
            ".PageCover-titleWrap h3",
            "h1.competition-title",
            ".competition-name h2",
            ".poule-title",
            "h2.poule-name",
            ".league-title",
            "h1",
            "h2",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                if text and len(text) > 3:
                    data.league_name = text
                    break

        def _int(s: str) -> int:
            s = s.strip()
            return int(s) if s.lstrip("-").isdigit() else 0

        normalized = team_name.lower().strip()
        entries: list[StandingEntry] = []
        name_match: StandingEntry | None = None

        # Each team is an <a class="row"> linking to /team/...
        for row in soup.select('a.row[href*="/team/"]'):
            # voetbal.nl marks the logged-in user's own team with the
            # "my-team" class; the row href also carries the team ID.
            row_href = row.get("href", "")
            is_own_team = "my-team" in (row.get("class") or []) or bool(
                team_id and f"/{team_id}/" in f"{row_href}/"
            )
            vals = [el.get_text(strip=True) for el in row.select(".value")]
            # Need at least: position, logo, team, and some stats
            if len(vals) < 4 or not vals[0].isdigit():
                continue

            # index: 0=pos, 1=logo(skip), 2=name, 3=G, 4=W, 5=GL, 6=V,
            #        7=P(points), 8=goals_for, 9=goals_against, 10=PM
            def _val(i: int) -> int:
                return _int(vals[i]) if len(vals) > i else 0

            entry = StandingEntry(
                position=int(vals[0]),
                team_name=vals[2],
                games_played=_val(3),
                won=_val(4),
                drawn=_val(5),
                lost=_val(6),
                points=_val(7),
                goals_for=_val(8),
                goals_against=_val(9),
            )
            entry.goal_difference = entry.goals_for - entry.goals_against
            entries.append(entry)

            if is_own_team:
                data.team_entry = entry
            elif name_match is None and normalized and normalized in entry.team_name.lower():
                name_match = entry

        data.entries = entries
        if data.team_entry is None:
            data.team_entry = name_match
        if data.team_entry:
            data.team_position = data.team_entry.position
            data.team_name = data.team_entry.team_name
        return data

    async def _fetch_schedule_ajax(self, url: str, label: str) -> str:
        """Return the extra schedule HTML behind "Toon volledig programma".

        The endpoint answers with ``{"html": "<div class=\\"table-wrapper\\">…"}``.
        A failure raises: a schedule without its remaining rounds would drop
        those matches from the calendar.
        """
        session = await self._get_session()
        try:
            async with session.get(
                url, headers={"X-Requested-With": "XMLHttpRequest"}
            ) as resp:
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"Full {label} (ajax) returned HTTP {resp.status}"
                    )
                payload = await resp.json(content_type=None)
        except (*_NETWORK_ERRORS, ValueError) as err:
            raise VoetbalNLConnectionError(
                f"Error fetching full {label} (ajax): {err!r}"
            ) from err

        if not isinstance(payload, dict):
            raise VoetbalNLConnectionError(f"Unexpected full {label} (ajax) payload")
        return payload.get("html") or ""

    @classmethod
    def _parse_schedule_page(
        cls, html: str, played: bool
    ) -> tuple[list[MatchData], str | None]:
        """Return the match rows on a schedule/results page and its ajax URL.

        The ajax URL sits in the ``ScheduleResults`` component, e.g.
        ``data-options='{"url": "/team/ajax/T2070245537/programma/competitie-najaar"}'``.
        It is legitimately empty before a competition has published rounds.
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = cls._parse_match_list(soup, played=played)

        ajax_url: str | None = None
        el = soup.select_one('[data-component="ScheduleResults"][data-options]')
        if el:
            try:
                options = json.loads(el.get("data-options") or "{}")
            except ValueError:
                _LOGGER.warning("Could not parse ScheduleResults data-options")
                options = {}
            url = (options.get("url") or "").strip() if isinstance(options, dict) else ""
            if url:
                ajax_url = absolute_voetbalnl_url(url)
                if not ajax_url:
                    _LOGGER.warning("Ignoring off-site schedule URL: %s", url)
        return rows, ajax_url

    @classmethod
    def _parse_match_html(cls, html: str, played: bool) -> list[MatchData]:
        """Parse match rows from an HTML fragment."""
        return cls._parse_match_list(BeautifulSoup(html, "html.parser"), played=played)

    @staticmethod
    def _dedupe_matches(matches: list[MatchData]) -> list[MatchData]:
        """Drop duplicates while preserving order (page rows + ajax rows overlap)."""
        seen: set[tuple] = set()
        unique: list[MatchData] = []
        for m in matches:
            key = (
                (m.match_url,)
                if m.match_url
                else (m.date, m.time, m.home_team, m.away_team)
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        return unique

    @staticmethod
    def _pick_match(matches: list[MatchData], team_name: str) -> MatchData | None:
        """Return the first match involving our own team, or None."""
        # Preferred: the "my-team" marker voetbal.nl puts on our own rows
        for m in matches:
            if m.is_my_team:
                return m

        normalized = (team_name or "").lower().strip()
        if normalized:
            for m in matches:
                if (
                    normalized in m.home_team.lower()
                    or normalized in m.away_team.lower()
                ):
                    return m
        return None

    @staticmethod
    def _match_involves_team(match: MatchData, team_name: str) -> bool:
        """Return True if this match involves our own team.

        Prefers the "my-team" class voetbal.nl adds for the logged-in user;
        falls back to a substring match on *team_name* when that marker is
        absent (e.g. anonymous sessions or layout changes).
        """
        if match.is_my_team:
            return True
        needle = (team_name or "").lower().strip()
        if not needle:
            return False
        return (
            needle in (match.home_team or "").lower()
            or needle in (match.away_team or "").lower()
        )

    # Dutch month names → month number
    _DUTCH_MONTHS: dict[str, int] = {
        "januari": 1, "februari": 2, "maart": 3, "april": 4,
        "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
        "september": 9, "oktober": 10, "november": 11, "december": 12,
    }

    @classmethod
    def _parse_dutch_date(cls, text: str) -> str:
        """
        Convert a Dutch date string like 'Woensdag 29 april 2026' to 'dd-mm-yyyy'.
        Returns an empty string if parsing fails.
        """
        # Normalise: lowercase, strip, collapse whitespace
        clean = " ".join(text.lower().split())
        # Pattern: optional weekday, day, month-name, year
        m = re.search(
            r"(\d{1,2})\s+(" + "|".join(cls._DUTCH_MONTHS) + r")\s+(\d{4})",
            clean,
        )
        if not m:
            return ""
        day = int(m.group(1))
        month = cls._DUTCH_MONTHS[m.group(2)]
        year = int(m.group(3))
        return f"{day:02d}-{month:02d}-{year}"

    @classmethod
    def _parse_match_list(
        cls, soup: BeautifulSoup, played: bool | None = None
    ) -> list[MatchData]:
        """
        Parse match rows from a voetbal.nl schedule or results page.

        Each date group is wrapped in a ``<div class="table">`` element:
            <div class="table">
                <div class="header">
                    <span class="title"><span> Woensdag 29 april 2026 </span></span>
                    <span class="subtitle"><span>16e ronde</span></span>
                </div>
                <a href="/wedstrijd/M{id}/programma" class="row [my-team]">
                    <div class="value home [my-team]"><div class="team">Home</div>…</div>
                    <div class="value center">19:00</div>
                    <div class="value away [my-team]">…<div class="team">Away</div></div>
                </a>
            </div>

        The center div contains a kickoff time ("19:00") for upcoming matches
        and a score ("3 - 1") for played matches.
        When *played* is None all matches are returned regardless of status.
        """
        matches: list[MatchData] = []

        table_divs = soup.select("div.table")
        if table_divs:
            # Structured layout: one div.table per date group
            for table_div in table_divs:
                # Extract date from the group header
                title_el = table_div.select_one("div.header span.title span")
                current_date = cls._parse_dutch_date(title_el.get_text()) if title_el else ""

                for row in table_div.select('a.row[href*="wedstrijd"]'):
                    match = cls._parse_match_row_div(row)
                    if match:
                        if current_date and not match.date:
                            match.date = current_date
                        if played is None or match.is_played == played:
                            matches.append(match)
        else:
            # Fallback: no div.table wrappers — parse all match rows directly
            for row in soup.select('a.row[href*="wedstrijd"]'):
                match = cls._parse_match_row_div(row)
                if match and (played is None or match.is_played == played):
                    matches.append(match)

        return matches

    @staticmethod
    def _parse_match_row_div(row) -> MatchData | None:
        """Parse a single <a class="row"> match element on voetbal.nl."""
        m = MatchData()

        m.match_url = absolute_voetbalnl_url(row.get("href", "")) or ""

        home_cell = row.select_one(".value.home")
        if home_cell:
            home_el = home_cell.select_one(".team")
            if home_el:
                m.home_team = home_el.get_text(strip=True)
            if "my-team" in (home_cell.get("class") or []):
                m.my_team_is_home = True

        away_cell = row.select_one(".value.away")
        if away_cell:
            away_el = away_cell.select_one(".team")
            if away_el:
                m.away_team = away_el.get_text(strip=True)
            if "my-team" in (away_cell.get("class") or []):
                m.my_team_is_home = False

        center_el = row.select_one(".value.center")
        if center_el:
            txt = center_el.get_text(strip=True)
            score_m = re.match(r"^(\d+)\s*-\s*(\d+)$", txt)
            if score_m:
                m.home_score = int(score_m.group(1))
                m.away_score = int(score_m.group(2))
                m.is_played = True
            elif re.match(r"^\d{1,2}:\d{2}$", txt):
                m.time = txt

        return m if (m.home_team or m.away_team) else None

    @staticmethod
    def _parse_match_datetime(
        date_str: str, time_str: str
    ) -> datetime | date | None:
        """
        Parse voetbal.nl date/time strings into a Python datetime or date.

        Handles Dutch formats like ``dd-mm-yyyy`` and ``dd/mm/yyyy``,
        as well as ISO ``yyyy-mm-dd``. Returns a timezone-naive ``datetime``
        when a time is present, or a ``date`` when no time is available.
        Returns ``None`` when parsing fails.
        """
        clean = date_str.strip().replace("/", "-")
        parsed: date | None = None
        for fmt in ("%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(clean, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        if time_str:
            try:
                t = datetime.strptime(time_str.strip(), "%H:%M").time()
                return datetime.combine(parsed, t)
            except ValueError:
                pass
        return parsed

    async def get_match_location(self, match_url: str) -> str:
        """Return the venue of a match from its detail page.

        Falls back to the last known venue when the page cannot be loaded, so
        a hiccup does not wipe the location from the calendar event.
        """
        if not match_url:
            return ""
        session = await self._get_session()
        try:
            async with session.get(match_url) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Match detail page returned HTTP %s for %s",
                        resp.status, match_url,
                    )
                    return self._location_cache.get(match_url, "")
                html = await resp.text()
        except _NETWORK_ERRORS as err:
            _LOGGER.debug("Error fetching match detail for location: %r", err)
            return self._location_cache.get(match_url, "")

        location = await self._run(self._parse_location, html)
        if location:
            self._location_cache[match_url] = location
        else:
            _LOGGER.debug("No LocationDetails found on match page: %s", match_url)
        return location

    @staticmethod
    def _parse_location(html: str) -> str:
        """Return "park, street, city" from a match detail page."""
        soup = BeautifulSoup(html, "html.parser")

        park = soup.select_one(".LocationDetails-infoPark")
        if not park:
            return ""

        parts = [park.get_text(strip=True)]

        street_el = soup.select_one(".LocationDetails-infoStreet")
        if street_el:
            parts.append(street_el.get_text(strip=True))

        zip_el = soup.select_one(".LocationDetails-infoZip")
        if zip_el:
            # "1076EP Amsterdam" — grab only the city (everything after the postcode)
            zip_text = zip_el.get_text(strip=True)
            city_match = re.search(r"[A-Z]{2}\s+(.+)$", zip_text)
            if city_match:
                parts.append(city_match.group(1).strip())

        return ", ".join(parts)
