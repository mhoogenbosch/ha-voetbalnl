# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
