"""Voetbal.nl API client - handles authentication and data scraping."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    BASE_URL,
    DEFAULT_TIMEOUT,
    HEADERS,
    KNVB_MODAL_URL,
    KNVB_OAUTH_HOST,
    KNVB_OAUTH_LOGIN_URL,
    LOGIN_URL,
    PROFILE_URL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class TeamInfo:
    """Basic information about a team."""

    name: str
    url: str
    club: str = ""
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

    @property
    def score_display(self) -> str:
        """Return score as display string."""
        if self.home_score is not None and self.away_score is not None:
            return f"{self.home_score}-{self.away_score}"
        return f"{self.time}" if self.time else "TBD"

    def result_for_team(self, team_name: str) -> str:
        """Return result string for a specific team."""
        if self.home_score is None or self.away_score is None:
            return "unknown"
        # Prefer the authoritative "my-team" marker from voetbal.nl
        if self.my_team_is_home is not None:
            if self.my_team_is_home:
                if self.home_score > self.away_score:
                    return "win"
                if self.home_score < self.away_score:
                    return "loss"
                return "draw"
            if self.away_score > self.home_score:
                return "win"
            if self.away_score < self.home_score:
                return "loss"
            return "draw"
        normalized = team_name.lower().strip()
        home_normalized = self.home_team.lower().strip()
        if normalized in home_normalized or home_normalized in normalized:
            if self.home_score > self.away_score:
                return "win"
            if self.home_score < self.away_score:
                return "loss"
            return "draw"
        if self.away_score > self.home_score:
            return "win"
        if self.away_score < self.home_score:
            return "loss"
        return "draw"


class VoetbalNLAuthError(Exception):
    """Raised when authentication fails."""


class VoetbalNLConnectionError(Exception):
    """Raised when connection to voetbal.nl fails."""


class VoetbalNLTeamNotFoundError(Exception):
    """Raised when a team cannot be found."""


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

    async def login(self, username: str, password: str) -> bool:
        """
        Log in to voetbal.nl.

        Uses the native voetbal.nl Drupal form login (email + password).
        reCAPTCHA is enforced client-side only and does not block server-side auth.

        Raises VoetbalNLAuthError on bad credentials.
        Raises VoetbalNLConnectionError on network failure.
        """
        self._username = username
        self._password = password

        # Use the native Drupal form login (works for all voetbal.nl accounts).
        # voetbal.nl does not expose a functional OAuth redirect_uri for
        # third-party clients, so the Drupal form is the only viable path.
        return await self._login_drupal_form(username, password)

    async def _login_oauth(self, username: str, password: str) -> bool:
        """
        Log in using the KNVB Account OAuth flow (login.knvbaccount.nl).

        Flow:
          1. GET knvb.nl/sportlink_account/modal/login/always_link
             → parse data-iframe-src for the OAuth URL (fresh nonce + state)
          2. GET the OAuth URL → receive login form + JSESSIONID cookie
          3. POST credentials to /oauth/login
          4. Follow redirects: login.knvbaccount.nl → knvb.nl callback → voetbal.nl
          5. The voetbal.nl Drupal session cookie is now set

        Raises VoetbalNLAuthError on wrong credentials.
        Raises VoetbalNLConnectionError / generic Exception on infrastructure errors.
        """
        session = await self._get_session()

        # -- Step 1: get a fresh OAuth URL from the KNVB modal endpoint ----------
        try:
            async with session.get(KNVB_MODAL_URL, allow_redirects=True) as resp:
                modal_html = await resp.text()
        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Cannot reach KNVB modal endpoint: {err}"
            ) from err

        match = re.search(r'data-iframe-src="([^"]+)"', modal_html)
        if not match:
            raise VoetbalNLConnectionError(
                "Could not find OAuth URL in KNVB modal response"
            )
        oauth_url = match.group(1).replace("&amp;", "&")
        _LOGGER.debug("OAuth URL: %s", oauth_url)

        # -- Step 2: load the OAuth login page (sets JSESSIONID cookie) ----------
        try:
            async with session.get(oauth_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"OAuth login page returned HTTP {resp.status}"
                    )
                login_page_html = await resp.text()
        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Cannot reach login.knvbaccount.nl: {err}"
            ) from err

        # Verify the login form is present
        if 'id="form-login"' not in login_page_html and "form-login" not in login_page_html:
            raise VoetbalNLConnectionError(
                "Expected login form not found at login.knvbaccount.nl"
            )

        # -- Step 3: POST credentials to /oauth/login ----------------------------
        post_data = {"username": username, "password": password}
        try:
            async with session.post(
                KNVB_OAUTH_LOGIN_URL,
                data=post_data,
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                resp_text = await resp.text()
        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Network error during OAuth credential POST: {err}"
            ) from err

        # Check for authentication failure (wrong credentials)
        # login.knvbaccount.nl returns to the login page with an error on failure
        if KNVB_OAUTH_HOST in final_url:
            # Still on the OAuth server — credentials were rejected
            raise VoetbalNLAuthError(
                "Invalid username or password (KNVB Account)."
            )

        # -- Step 4: verify we landed on voetbal.nl with a valid session ----------
        if not await self._is_authenticated(session):
            _LOGGER.debug(
                "OAuth flow completed but voetbal.nl session not established "
                "(final URL: %s)", final_url
            )
            raise VoetbalNLConnectionError(
                "OAuth login completed but voetbal.nl session was not set."
            )

        self._authenticated = True
        self._login_time = datetime.now()
        _LOGGER.debug("Successfully logged in via KNVB Account OAuth as %s", username)
        return True

    async def _login_drupal_form(self, username: str, password: str) -> bool:
        """
        Log in using the native voetbal.nl Drupal form (legacy/fallback path).

        Only works for accounts that have a voetbal.nl-specific password.
        Accounts created via the KNVB Account SSO button have no Drupal password
        and will always fail here — use _login_oauth() for those accounts.
        """
        session = await self._get_session()

        try:
            # Step 1 – load the login page to get CSRF form tokens
            async with session.get(LOGIN_URL, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise VoetbalNLConnectionError(
                        f"Login page returned HTTP {resp.status}"
                    )
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Cannot reach voetbal.nl login page: {err}"
            ) from err

        soup = BeautifulSoup(html, "html.parser")

        # Find the login form (id="login-form" or contains email + password fields)
        form = (
            soup.find("form", id="login-form")
            or soup.find("form", {"action": re.compile(r"inloggen", re.I)})
        )
        if not form:
            raise VoetbalNLConnectionError(
                "Could not locate the login form on voetbal.nl"
            )

        # Collect all hidden fields (form_build_id, form_id, …)
        form_data: dict[str, str] = {}
        for inp in form.find_all("input", type="hidden"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")

        # Credentials — field names confirmed from page source
        form_data["email"] = username
        form_data["password"] = password
        # Submit button value (Drupal expects the op field)
        submit_btn = form.find("button", attrs={"name": "op"}) or form.find(
            "input", attrs={"name": "op"}
        )
        if submit_btn:
            form_data["op"] = submit_btn.get("value", "Inloggen")

        action = form.get("action", "/inloggen")
        if not action.startswith("http"):
            action = f"{BASE_URL}{action}"

        try:
            async with session.post(
                action, data=form_data, allow_redirects=True
            ) as resp:
                if resp.status not in (200, 302):
                    raise VoetbalNLConnectionError(
                        f"Login POST returned HTTP {resp.status}"
                    )
                html_after = await resp.text()
        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Network error during login POST: {err}"
            ) from err

        if not await self._is_authenticated(session):
            # Detect the specific error message voetbal.nl shows
            err_soup = BeautifulSoup(html_after, "html.parser")
            error_el = err_soup.find(
                class_=re.compile(r"messages?.*error|alert.*error|form.*error", re.I)
            )
            detail = error_el.get_text(strip=True) if error_el else ""
            raise VoetbalNLAuthError(
                f"Invalid username or password for voetbal.nl. {detail}".strip()
            )

        self._authenticated = True
        self._login_time = datetime.now()
        _LOGGER.debug(
            "Successfully logged in to voetbal.nl as %s", username
        )
        return True

    async def ensure_authenticated(self) -> None:
        """
        Ensure the session is still valid; re-login automatically if needed.

        Called before every data fetch. Re-authenticates when:
        - The session has never been authenticated
        - The session is older than SESSION_REFRESH_DAYS days
        - The session cookie has expired (verified by a profile page check)
        """
        if not self._username:
            raise VoetbalNLAuthError(
                "No credentials stored — call login() first."
            )

        # Proactive refresh: re-login shortly before the Drupal session expires
        if self._login_time is not None:
            age = datetime.now() - self._login_time
            if age >= timedelta(days=self.SESSION_REFRESH_DAYS):
                _LOGGER.debug(
                    "Session is %d days old — proactively refreshing login", age.days
                )
                await self.login(self._username, self._password)
                return

        # Reactive check: verify the cookie still works
        if not self._authenticated or not self._session:
            await self.login(self._username, self._password)
            return

        session = await self._get_session()
        if not await self._is_authenticated(session):
            _LOGGER.debug("Session cookie expired — re-logging in")
            await self.login(self._username, self._password)

    async def _is_authenticated(self, session: aiohttp.ClientSession) -> bool:
        """Return True if the current session has authenticated access.

        Note: voetbal.nl returns HTTP 404 for /mijnprofiel even when the user is
        logged in (the page simply doesn't exist for all accounts). A redirect to
        /inloggen means the session is NOT authenticated. We therefore skip the
        status check and look for a logout link or the logged-in body class.
        """
        try:
            async with session.get(PROFILE_URL, allow_redirects=True) as resp:
                # A redirect to /inloggen means not authenticated
                if "inloggen" in str(resp.url):
                    return False
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                body = soup.find("body")
                if body and "logged-in" in body.get("class", []):
                    return True
                # voetbal.nl shows a logout link for authenticated sessions
                if soup.find("a", href=re.compile(r"uitloggen|logout", re.I)):
                    return True
                return False
        except aiohttp.ClientError:
            return False

    async def search_team(self, team_name: str) -> list[TeamInfo]:
        """
        Search for a team by name using the voetbal.nl JSON search API.

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
        except (aiohttp.ClientError, ValueError) as err:
            raise VoetbalNLConnectionError(
                f"Connection error during team search: {err}"
            ) from err

        return self._parse_team_search_results(data, team_name)

    def _parse_team_search_results(
        self, data: dict, search_term: str
    ) -> list[TeamInfo]:
        """Parse team search results from the JSON search API response."""
        teams: list[TeamInfo] = []

        items = data.get("results", {}).get("items", [])
        for item in items:
            href = item.get("href", "")
            title = item.get("title", "")
            item_type = item.get("type", "")

            # Team items link to /team/T{id}/... or have type="team"
            if not ("/team/" in href or item_type == "team"):
                continue
            if not href or not title:
                continue

            team_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            id_match = re.search(r"/team/(T\d+)", href)
            team_id = id_match.group(1) if id_match else ""
            teams.append(TeamInfo(name=title, url=team_url, team_id=team_id))

        _LOGGER.debug(
            "Found %d team(s) matching '%s'", len(teams), search_term
        )
        return teams

    async def get_team_data(
        self, team_url: str, team_name: str
    ) -> dict[str, Any]:
        """
        Fetch all data for a team: standings, next match, last result.

        Args:
            team_url: The base URL of the team page on voetbal.nl.
            team_name: The team name (used to identify the team in results).

        Returns:
            Dictionary with keys 'standing', 'next_match', 'last_result'.
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
        """Fetch standings, schedule, and results concurrently."""
        session = await self._get_session()

        # Build sub-page URLs.
        # team_url is typically .../team/T{id}/overzicht — strip last path
        # segment to reach the team's base URL, then append sub-page names.
        base = team_url.rstrip("/")
        for _suffix in ("/overzicht", "/programma", "/uitslagen", "/stand"):
            if base.endswith(_suffix):
                base = base[: -len(_suffix)]
                break
        standing_url = f"{base}/stand"
        schedule_url = f"{base}/programma"
        results_url = f"{base}/uitslagen"

        # Team ID from the URL (e.g. "T2070245537"); used to recognise our own
        # row in the standings regardless of how the team is named.
        team_id_match = re.search(r"/team/(T\d+)", team_url)
        team_id = team_id_match.group(1) if team_id_match else ""

        # Name used for matching; replaced by the canonical voetbal.nl name as
        # soon as the standings page has been parsed (the standings are fetched
        # first below).
        effective_name = team_name

        standing_data: StandingData | None = None
        next_match: MatchData | None = None
        last_result: MatchData | None = None
        all_matches: list[MatchData] = []

        try:
            # Fetch all three pages
            for url, label in [
                (standing_url, "standing"),
                (schedule_url, "schedule"),
                (results_url, "results"),
            ]:
                try:
                    async with session.get(url) as response:
                        if response.status != 200:
                            _LOGGER.warning(
                                "Failed to fetch %s page: HTTP %s",
                                label,
                                response.status,
                            )
                            continue
                        html = await response.text()

                    if label == "standing":
                        standing_data = self._parse_standings(
                            html, team_name, team_id
                        )
                        if standing_data and standing_data.team_name:
                            effective_name = standing_data.team_name
                            _LOGGER.debug(
                                "Resolved configured team '%s' to voetbal.nl "
                                "team '%s'",
                                team_name,
                                effective_name,
                            )
                    elif label in ("schedule", "results"):
                        played = label == "results"
                        rows = self._parse_match_list(
                            BeautifulSoup(html, "html.parser"), played=played
                        )
                        # The page itself only renders the next round; the rest
                        # sits behind "Toon volledig programma", which loads the
                        # remaining rounds from an ajax endpoint named in the
                        # page. Merge those in so the calendar holds the whole
                        # published schedule instead of a single fixture.
                        extra_html = await self._fetch_schedule_ajax(
                            session, html, label
                        )
                        if extra_html:
                            rows.extend(
                                self._parse_match_list(
                                    BeautifulSoup(extra_html, "html.parser"),
                                    played=played,
                                )
                            )
                        rows = self._dedupe_matches(rows)
                        picked = self._pick_match(rows, effective_name)
                        if played:
                            last_result = picked
                        else:
                            next_match = picked
                        all_matches.extend(
                            m for m in rows
                            if self._match_involves_team(m, effective_name)
                        )

                except aiohttp.ClientError as err:
                    _LOGGER.warning("Error fetching %s: %s", label, err)

        except aiohttp.ClientError as err:
            raise VoetbalNLConnectionError(
                f"Connection error fetching team data: {err}"
            ) from err

        # Enrich next match and last result with location from their detail pages
        if next_match and next_match.match_url and not next_match.location:
            next_match.location = await self.get_match_location(next_match.match_url)
        if last_result and last_result.match_url and not last_result.location:
            last_result.location = await self.get_match_location(last_result.match_url)

        return standing_data, next_match, last_result, all_matches

    def _parse_standings(
        self, html: str, team_name: str, team_id: str = ""
    ) -> StandingData | None:
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

        entries: list[StandingEntry] = []

        # Each team is an <a class="row"> linking to /team/...
        for row in soup.select('a.row[href*="/team/"]'):
            try:
                # voetbal.nl marks the logged-in user's own team with the
                # "my-team" class; the row href also carries the team ID.
                row_href = row.get("href", "")
                is_own_team = "my-team" in (row.get("class") or []) or bool(
                    team_id and team_id in row_href
                )
                vals = [el.get_text(strip=True) for el in row.select(".value")]
                # Need at least: position, logo, team, and some stats
                if len(vals) < 4 or not vals[0].isdigit():
                    continue

                # index: 0=pos, 1=logo(skip), 2=name, 3=G, 4=W, 5=GL, 6=V,
                #        7=P(points), 8=goals_for, 9=goals_against, 10=PM
                entry = StandingEntry(
                    position=int(vals[0]),
                    team_name=vals[2] if len(vals) > 2 else "",
                    games_played=_int(vals[3]) if len(vals) > 3 else 0,
                    won=_int(vals[4]) if len(vals) > 4 else 0,
                    drawn=_int(vals[5]) if len(vals) > 5 else 0,
                    lost=_int(vals[6]) if len(vals) > 6 else 0,
                    points=_int(vals[7]) if len(vals) > 7 else 0,
                    goals_for=_int(vals[8]) if len(vals) > 8 else 0,
                    goals_against=_int(vals[9]) if len(vals) > 9 else 0,
                )
                entry.goal_difference = entry.goals_for - entry.goals_against
                entries.append(entry)

                if is_own_team or (
                    not data.team_entry
                    and team_name.lower().strip() in entry.team_name.lower()
                ):
                    data.team_entry = entry
                    data.team_position = entry.position
                    data.team_name = entry.team_name
            except (ValueError, IndexError) as err:
                _LOGGER.debug("Skipping standing row: %s", err)

        data.entries = entries

        if not data.team_entry and entries:
            normalized = team_name.lower().strip()
            for entry in entries:
                if normalized in entry.team_name.lower():
                    data.team_entry = entry
                    data.team_position = entry.position
                    data.team_name = entry.team_name
                    break

        return data

    @staticmethod
    def _parse_standing_row(cells: list) -> StandingEntry | None:
        """Parse a single row from the standings table."""
        # Extract text from each cell
        values = [c.get_text(strip=True) for c in cells]

        if len(values) < 4:
            return None

        # Skip if first cell is not a number (header row protection)
        if not values[0].isdigit():
            return None

        entry = StandingEntry(position=int(values[0]), team_name=values[1])

        # Try to parse remaining columns (order may vary)
        # Common column orders: Pos, Team, G, W, G, V, Dv, Dt, Ds, Ptn
        # or: Pos, Team, Ptn, G, W, G, V, Dv, Dt, Ds
        col_count = len(values)

        if col_count >= 10:
            # Standard format: Pos, Team, G, W, G(elijk), V, +, -, Ds, Ptn
            try:
                entry.games_played = int(values[2]) if values[2].lstrip("-").isdigit() else 0
                entry.won = int(values[3]) if values[3].lstrip("-").isdigit() else 0
                entry.drawn = int(values[4]) if values[4].lstrip("-").isdigit() else 0
                entry.lost = int(values[5]) if values[5].lstrip("-").isdigit() else 0
                entry.goals_for = int(values[6]) if values[6].lstrip("-").isdigit() else 0
                entry.goals_against = int(values[7]) if values[7].lstrip("-").isdigit() else 0
                # values[8] might be goal difference
                entry.points = int(values[-1]) if values[-1].lstrip("-").isdigit() else 0
                entry.goal_difference = entry.goals_for - entry.goals_against
            except (ValueError, IndexError):
                pass
        elif col_count >= 4:
            # Minimal: Pos, Team, ?, Points
            try:
                entry.points = int(values[-1]) if values[-1].lstrip("-").isdigit() else 0
            except ValueError:
                pass

        return entry

    async def _fetch_schedule_ajax(
        self, session: aiohttp.ClientSession, page_html: str, label: str
    ) -> str:
        """Return the extra schedule HTML behind "Toon volledig programma".

        A schedule or results page renders only the next round; the remaining
        rounds are loaded by the page's ``ScheduleResults`` component from an
        ajax endpoint whose URL sits in its ``data-options`` attribute, e.g.
        ``{"url": "/team/ajax/T2070245537/programma/competitie-najaar"}``. The
        endpoint answers with ``{"html": "<div class=\\"table-wrapper\\">…"}``.

        Returns an empty string when there is nothing extra to fetch — the URL
        is legitimately empty before a competition has any published rounds.
        """
        soup = BeautifulSoup(page_html, "html.parser")
        el = soup.select_one('[data-component="ScheduleResults"][data-options]')
        if not el:
            return ""

        try:
            options = json.loads(el.get("data-options") or "{}")
        except ValueError:
            _LOGGER.debug("Could not parse ScheduleResults data-options (%s)", label)
            return ""

        url = (options.get("url") or "").strip() if isinstance(options, dict) else ""
        if not url:
            return ""
        if not url.startswith("http"):
            url = f"{BASE_URL}{url}"

        try:
            async with session.get(
                url, headers={"X-Requested-With": "XMLHttpRequest"}
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Failed to fetch full %s (ajax): HTTP %s", label, resp.status
                    )
                    return ""
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.warning("Error fetching full %s (ajax): %s", label, err)
            return ""

        if not isinstance(payload, dict):
            return ""
        return payload.get("html") or ""

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

    def _parse_next_match(self, html: str, team_name: str) -> MatchData | None:
        """
        Parse the next upcoming match for the tracked team from a programma page.

        The page shows all matches in the competition poule. Filters to the first
        match where home_team or away_team contains *team_name*. Falls back to
        the first match in the list if no match for the team is found.
        """
        soup = BeautifulSoup(html, "html.parser")
        matches = self._parse_match_list(soup, played=False)

        if not matches:
            _LOGGER.debug("No upcoming matches found for %s", team_name)
            return None

        picked = self._pick_match(matches, team_name)
        if picked:
            return picked

        _LOGGER.debug(
            "No upcoming match found for '%s' — team names in schedule: %s",
            team_name,
            [f"{m.home_team} vs {m.away_team}" for m in matches[:5]],
        )
        return None

    def _parse_last_result(self, html: str, team_name: str) -> MatchData | None:
        """
        Parse the most recent result for the tracked team from a uitslagen page.

        The page shows all results in the competition poule. Filters to the first
        match (newest) where home_team or away_team contains *team_name*.
        Falls back to the first match in the list if no team-specific match is found.
        """
        soup = BeautifulSoup(html, "html.parser")
        matches = self._parse_match_list(soup, played=True)

        if not matches:
            _LOGGER.debug("No results found for %s", team_name)
            return None

        picked = self._pick_match(matches, team_name)
        if picked:
            return picked

        _LOGGER.debug(
            "No result found for '%s' — team names in results: %s",
            team_name,
            [f"{m.home_team} vs {m.away_team}" for m in matches[:5]],
        )
        return None

    @staticmethod
    def _match_involves_team(match: "MatchData", team_name: str) -> bool:
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

    def _parse_match_list(
        self, soup: BeautifulSoup, played: bool | None = None
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
                current_date = self._parse_dutch_date(title_el.get_text()) if title_el else ""

                for row in table_div.select('a.row[href*="wedstrijd"]'):
                    match = self._parse_match_row_div(row)
                    if match:
                        if current_date and not match.date:
                            match.date = current_date
                        if played is None or match.is_played == played:
                            matches.append(match)
        else:
            # Fallback: no div.table wrappers — parse all match rows directly
            for row in soup.select('a.row[href*="wedstrijd"]'):
                match = self._parse_match_row_div(row)
                if match and (played is None or match.is_played == played):
                    matches.append(match)

        return matches

    @staticmethod
    def _parse_match_row_div(row) -> MatchData | None:
        """Parse a single <a class="row"> match element on voetbal.nl."""
        m = MatchData()

        href = row.get("href", "")
        if href:
            m.match_url = href if href.startswith("http") else f"{BASE_URL}{href}"

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
    def _parse_match_row(cells: list) -> MatchData | None:
        """Parse a single match row from a table."""
        values = [c.get_text(strip=True) for c in cells]

        if len(values) < 3:
            return None

        match = MatchData()

        # Try to identify columns by content pattern
        for i, val in enumerate(values):
            # Date pattern: dd-mm-yyyy or dd/mm/yyyy
            if re.match(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", val):
                match.date = val
            # Time pattern: HH:MM
            elif re.match(r"\d{1,2}:\d{2}", val) and not match.time:
                match.time = val
            # Score pattern: X-Y
            elif re.match(r"^\d+\s*-\s*\d+$", val):
                parts = re.split(r"\s*-\s*", val)
                try:
                    match.home_score = int(parts[0])
                    match.away_score = int(parts[1])
                    match.is_played = True
                except (ValueError, IndexError):
                    pass

        # Home and away teams are typically in adjacent cells
        # after date/time columns. Heuristic: find the cell before the score/time cell
        team_cells = [
            c.get_text(strip=True) for c in cells
            if not re.match(
                r"^\d{1,2}[-/]\d{1,2}|^\d{1,2}:\d{2}|^\d+\s*-\s*\d+$",
                c.get_text(strip=True),
            )
            and len(c.get_text(strip=True)) > 1
        ]

        if len(team_cells) >= 2:
            match.home_team = team_cells[0]
            match.away_team = team_cells[1]

        # Competition may appear in later columns
        if len(team_cells) >= 3:
            match.competition = team_cells[2]
        if len(team_cells) >= 4:
            match.location = team_cells[3]

        return match if (match.home_team or match.away_team) else None

    @staticmethod
    def _parse_match_item(item) -> MatchData | None:
        """Parse a match from a non-table HTML element."""
        match = MatchData()

        # Extract date
        date_el = item.find(class_=re.compile(r"date|datum", re.I))
        if date_el:
            match.date = date_el.get_text(strip=True)

        # Extract time
        time_el = item.find(class_=re.compile(r"time|tijd", re.I))
        if time_el:
            match.time = time_el.get_text(strip=True)

        # Extract teams
        home_el = item.find(class_=re.compile(r"home|thuis", re.I))
        away_el = item.find(class_=re.compile(r"away|uit", re.I))
        if home_el:
            match.home_team = home_el.get_text(strip=True)
        if away_el:
            match.away_team = away_el.get_text(strip=True)

        # Extract score
        score_el = item.find(class_=re.compile(r"score|uitslag", re.I))
        if score_el:
            score_text = score_el.get_text(strip=True)
            score_match = re.match(r"(\d+)\s*-\s*(\d+)", score_text)
            if score_match:
                match.home_score = int(score_match.group(1))
                match.away_score = int(score_match.group(2))
                match.is_played = True

        return match if (match.home_team or match.away_team) else None

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
        """Fetch the location/venue of a match from its detail page."""
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
                    return ""
                html = await resp.text()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching match detail for location: %s", err)
            return ""

        soup = BeautifulSoup(html, "html.parser")

        park = soup.select_one(".LocationDetails-infoPark")
        if not park:
            _LOGGER.debug("No LocationDetails found on match page: %s", match_url)
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

    async def validate_team_url(self, team_url: str) -> bool:
        """Check if a team URL is accessible and returns a valid page."""
        session = await self._get_session()
        try:
            async with session.get(team_url) as response:
                if response.status != 200:
                    return False
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                # Check if it looks like a team page (has some team-related content)
                return bool(
                    soup.find(class_=re.compile(r"team|standen|programma", re.I))
                    or soup.find(string=re.compile(r"standen|programma|uitslagen", re.I))
                )
        except aiohttp.ClientError:
            return False
