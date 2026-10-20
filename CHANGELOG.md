# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.4.0-mh.1] - 2026-10-20

### Added
- **Diagnostic sensor *Competition* (*Competitie*).** State is the class and phase (`1e klasse (2e fase)`); attributes give the competition, phase, poule code and number, period, poule ID and name, a link to the standings, and every phase of the season.
- `phase` attribute on *Next match* and *Last result*; *Last result* also gets `time` and `match_url`.

### Fixed
- **Last result and the calendar emptied at a phase change.** The KNVB splits the autumn into phases with a new poule each; the plain results page only covers the current phase, which has no results right after the switch ("Er is geen laatste uitslag bekend"). Results now come from every phase listed in the team's *Indeling* menu, and *Last result* is the most recent of them.
- **Played matches lost their kick-off time** and became all-day events, because a results page shows the score instead of the time and the detail page of a played match shows neither. A calendar mirror then created a second event next to the timed one. Kick-off time and venue are now remembered while a match is scheduled and kept across restarts (`.storage/voetbalnl.<entry_id>`), so a match stays one timed event that gains its score.
- Matches are de-duplicated on their match ID: a match links to `…/programma` while scheduled and to `…/uitslag` once played.

---

## [1.3.0-mh.1] - 2026-10-20

### Changed
- **Dutch names and texts.** Entity names now come from translations (`translation_key`), so they follow the Home Assistant language: *Stand*, *Volgende wedstrijd*, *Laatste uitslag* and *Wedstrijden* in Dutch. Entity IDs do not change. The match sensor states are Dutch (`Geen wedstrijden gepland`, `Nog geen uitslag`, `… (winst/verlies/gelijk)`, `Home – Away`) and the calendar description says `Uitslag:`. The `result` attribute keeps `win`/`loss`/`draw`/`unknown`.
- **Device model** is now *Team (KNVB)* (manufacturer *KNVB / voetbal.nl*) instead of *Futsal Team Tracker*; the setup text no longer says futsal either.
- **HTML parsing runs in the executor** instead of on the event loop. Every refresh parses five to seven pages.
- The re-authentication form fills in the e-mail address.

### Fixed
- **A voetbal.nl outage no longer asks you to log in again.** A failed login or session check counted as wrong credentials, whatever the reason: a timeout, an HTTP error or the site's own "Inloggen is momenteel niet mogelijk". Home Assistant then started a re-authentication flow and stopped polling until you logged in again. Now only an explicit credentials error from voetbal.nl does that; everything else is a connection error that is retried automatically.
- **A partial fetch no longer empties the calendar.** When the standings, schedule, results or "full schedule" (ajax) page failed, the integration logged a warning and published what was left: sensors fell back to "no matches" and a calendar mirror read the missing fixtures as deletions (and re-created them on the next run). Any page failure now keeps the previous data, as Home Assistant does for a failed update.
- **Timeouts are handled as connection errors.** aiohttp's total timeout raises `TimeoutError`, which is not an `aiohttp.ClientError`, so it slipped past every handler (setup failed instead of retrying).
- **Clearing the team-URL option broke the entry**: the empty value overrode the URL from setup. An empty option now means "use the setup URL".
- **No leaked HTTP session** when the first refresh fails during setup.
- A venue that fails to load keeps its last known value instead of disappearing from the event.
- The standings matched the team ID as a substring (`T123` also matched `T1234`).

### Security
- **Team URLs are validated.** Setup and options only accept `https://www.voetbal.nl/team/T…` URLs; URLs read from pages (match links, the ajax endpoint) are only followed when they point at voetbal.nl.
- **Removed dead code**: the unused KNVB Account OAuth login (which logged its OAuth URL with state and nonce at debug level), several unused parsers, `validate_team_url` and an unused options schema. The e-mail address is no longer written to the debug log.

---

## [1.2.1-mh.1] - 2026-10-20

### Added
- **Integration icon.** The integration now ships its own icon in `custom_components/voetbalnl/brand/` (`icon.png`, `icon@2x.png`), taken from the voetbal.nl favicon set. Home Assistant 2026.3+ uses local brand images for custom integrations, so the repairs list, the integrations page and HACS no longer show the "icon not available" placeholder. Older Home Assistant versions keep showing the placeholder; nothing else changes.
- **CI validation.** A GitHub Actions workflow runs the HACS and hassfest validators on every push and pull request, and weekly.

### Changed
- `manifest.json` keys sorted as hassfest requires. (`categories` is not added: hassfest still rejects it, HACS only warns about it missing.)
- Removed the unsupported `description` key from `hacs.json`; the description now lives on the GitHub repository.

---

## [1.2.0-mh.1] - 2026-08-17

### Changed
- **Calendar event titles now name the side and the opponent** — `Uit: SJO JOY 26 O17-2`, `Thuis: VIOD O17-1`, with the score appended once played (`Uit: FC Bergh O17-1 (1-3)`). In a personal calendar that reads better than both team names, one of which is always your own. Titles are Dutch, matching the site. When the `my-team` marker is absent the sides cannot be told apart and the old `Home vs Away` title is used.
- The full fixture (`FC Bergh O17-1 – VVG '25 O17-1`) moved to the event description, so both team names stay available on the event.

---

## [1.1.3-mh.1] - 2026-08-17

### Fixed
- **Calendar events had no UID.** Anything that mirrors this calendar elsewhere (sync scripts, other integrations) keys on the event UID, and without one every fixture collapsed onto the same key. Each event now carries a stable UID derived from the match ID in its voetbal.nl URL (`voetbalnl-M79726132`), falling back to a slug of date and teams.

---

## [1.1.2-mh.1] - 2026-08-17

### Added
- **The calendar now holds the whole published schedule instead of only the next round.** A schedule or results page renders one round; the remaining rounds sit behind the *"Toon volledig programma"* button, which loads them from an ajax endpoint named in the page's `ScheduleResults` component (`data-options` → `{"url": "/team/ajax/T…/programma/competitie-najaar"}`). That endpoint is now followed and its fixtures merged in, de-duplicated on match URL. The URL is read from the page, never hard-coded, and an empty URL (no rounds published yet) is handled as "nothing extra".

---

## [1.1.1-mh.2] - 2026-08-17

### Changed
- Versienummer in `const.py` (device `sw_version`) gelijkgetrokken met `manifest.json`. Geen functionele wijzigingen ten opzichte van `1.1.1-mh.1`.

---

## [1.1.1-mh.1] - 2026-08-17

First release of the [mhoogenbosch](https://github.com/mhoogenbosch/ha-voetbalnl) fork.

### Fixed
- **Fixtures, results and league position are no longer lost when the configured team name differs from the name voetbal.nl uses.** The integration matched everything on a substring of the team name, so a config entry named `VVG 17-01` never matched `VVG '25 O17-1` on voetbal.nl: Next Match stayed on `No upcoming matches`, Last Result on `No results yet`, the calendar stayed empty and the Standing sensor had no position (while `full_standings` was filled — proof the pages were fetched fine). The own team is now identified by the **`my-team` class** voetbal.nl adds for the logged-in user, and in the standings additionally by the **team ID from the configured team URL** (`/team/T…`). Name matching remains as a fallback.
- **Home/away and win/loss were derived from the same name match** and therefore silently assumed "away" for every match. Both now come from the `my-team` marker on the home/away cell.
- **Session check hit a 404 page.** `PROFILE_URL` was `/mijnprofiel`, which does not exist on voetbal.nl; it only worked because that 404 page happens to contain a logout link. Now `/profiel/overzicht` (anonymous → redirects to `/inloggen`, authenticated → HTTP 200).

### Added
- The team name as voetbal.nl spells it is resolved from the standings page and exposed as the `team` attribute on the Standing sensor; the sensors use it instead of the configured name.

### Changed
- League name is read from the page header (`.PageCover-titleWrap h3`), e.g. `Onder 17 Zaterdag`, instead of falling back to the `<h1>` which holds the team name.
- Committed `__pycache__/*.pyc` files removed and a `.gitignore` added.

---

## [1.1.0] - 2026-05-19

### Added
- **Location attribute** on Next Match and Last Result sensors. The venue is fetched from the individual match detail page on voetbal.nl and formatted as `<name>, <street>, <city>` (e.g. `Sporthallen Zuid, Burgerweeshuispad 54, Amsterdam`).
- **`match_url` attribute** on the Next Match sensor — direct link to the match detail page on voetbal.nl.

### Fixed
- Next Match and Last Result sensors no longer fall back to showing a random fixture when the configured team name does not match any team name in the voetbal.nl schedule. The sensor now correctly returns `No upcoming matches` / `No results yet` in that case. A debug log entry lists the team names actually present in the schedule to help correct the configuration.

---

## [1.0.0] - 2026-05-01

### Added
- Initial release.
- Standing sensor with league position and full standings table.
- Next Match sensor with date, time, home/away indicator, and opponent.
- Last Result sensor with score and win/loss/draw result.
- Calendar entity showing only the configured team's fixtures.
- Lovelace dashboard example (`lovelace_dashboard.yaml`).
- HACS support.
