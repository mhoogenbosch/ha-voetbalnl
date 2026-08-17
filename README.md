# ha-voetbalnl — Voetbal.nl Home Assistant Integration

![Version](https://img.shields.io/badge/version-1.2.0--mh.1-blue) ![HA](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-brightgreen)

> **Fork.** This is a fork of [rcAlexterNeuzen/ha-voetbalnl](https://github.com/rcAlexterNeuzen/ha-voetbalnl).
> It identifies your own team by the `my-team` marker voetbal.nl adds for the logged-in user
> (and by the team ID in the team URL) instead of by matching the team name you typed, so
> fixtures, results and the league position are found even when the configured name differs
> from the name voetbal.nl uses — e.g. `VVG 17-01` vs `VVG '25 O17-1`. See [CHANGELOG.md](CHANGELOG.md).

Track your team on [voetbal.nl](https://www.voetbal.nl) directly from Home Assistant — veldvoetbal and zaalvoetbal (futsal) alike. The integration authenticates with your voetbal.nl account and exposes three sensors and a calendar per configured team.

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

---

## Entities

Each configured team creates one **device** (`Voetbal.nl – <team name>`) with the following entities:

| Entity | Type | State | Key Attributes |
|--------|------|-------|----------------|
| **Standing** | Sensor | League position (integer, e.g. `4`) | `league`, `team`, `position`, `games_played`, `won`, `drawn`, `lost`, `goals_for`, `goals_against`, `goal_difference`, `points`, `full_standings` (list of all teams) |
| **Next Match** | Sensor | `Home vs Away (HH:MM)` | `home_team`, `away_team`, `date`, `time`, `competition`, `location`, `match_url`, `is_home_game`, `opponent` |
| **Last Result** | Sensor | `Home X-Y Away (WIN/LOSS/DRAW)` | `home_team`, `away_team`, `home_score`, `away_score`, `date`, `competition`, `location`, `result` (`win`/`loss`/`draw`), `goals_scored`, `goals_conceded`, `is_home_game`, `opponent` |
| **Matches** | Calendar | Next upcoming match | Only the configured team's fixtures — scheduled matches as timed events, results with score in description |

> **Note:** The `full_standings` attribute on the Standing sensor is a list of dicts with keys `position`, `team`, `played`, `won`, `drawn`, `lost`, `gf`, `ga`, `gd`, `points` — one entry per team in the league.
> The `location` attribute on the Next Match and Last Result sensors is fetched from the individual match detail page and contains the venue name, street, and city (e.g. `Sporthallen Zuid, Burgerweeshuispad 54, Amsterdam`). It may be empty if voetbal.nl has not yet published a venue for that fixture.
> The `match_url` attribute on the Next Match sensor contains the direct link to the match detail page on voetbal.nl.

---

## Installation

### HACS (recommended)

1. In HACS, go to **Integrations → Custom repositories** and add this repository (category: *Integration*).
2. Install *Voetbal.nl* and restart Home Assistant.

### Manual

1. Copy the `custom_components/voetbalnl` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for *Voetbal.nl*.

### Step 1 – Credentials

Enter your **voetbal.nl** email address and password. The integration:

- Logs in via the voetbal.nl login form (Drupal-based, no reCAPTCHA required server-side)
- Stores the session cookie in memory only — never written to disk
- Re-authenticates automatically every 6 days, or immediately if the session is rejected

Your password is stored in the HA config entry (`.storage/core.config_entries`, readable only by the HA process) and is never sent anywhere other than `voetbal.nl`.

> **Note:** You need a [voetbal.nl account](https://www.voetbal.nl/gebruiker/aanmaken) (free, separate from mijn.knvb.nl).

### Step 2 – Team

Type your **team name** as shown on voetbal.nl (e.g. `ASV Arsenal VR1`). The integration searches voetbal.nl and lets you pick from the results.

> **Tip:** If search doesn't find your team, navigate to the team page on voetbal.nl and copy the URL (e.g. `https://www.voetbal.nl/team/T1234567890/overzicht`). Paste it into the optional *Team URL* field to bypass search entirely.

You can track **multiple teams** by adding the integration more than once.

---

## Calendar

Each team exposes a `calendar.voetbal_nl_<team_slug>_matches` entity showing only **that team's** fixtures:

- **Upcoming matches** — timed events with a 90-minute duration, date taken from the voetbal.nl schedule page header
- **Played matches** — includes the final score in the event description
- Matches without a known kick-off time appear as **all-day events**

```yaml
type: calendar
entities:
  - calendar.voetbal_nl_asv_arsenal_vr1_matches
```

---

## Lovelace Dashboard

A ready-made dashboard YAML is included in [`lovelace_dashboard.yaml`](lovelace_dashboard.yaml). It contains:

- Team name + league header
- Positie / Punten / Gespeeld stat row
- W / GL / V / goals mini-table
- Next match card with THUIS/UIT badge
- Last result card with WIN (green) / LOSS (red) / DRAW (orange) badge
- Full poulestand markdown table with your team highlighted in bold

**To use it:**

1. Open a dashboard in HA → pencil icon → **Edit raw configuration**
2. Paste the contents of `lovelace_dashboard.yaml`
3. Replace all occurrences of `sensor.voetbal_nl_asv_arsenal_vr1` with your actual entity prefix (find it via **Developer Tools → States**, search `voetbal`)

---

## Options

After setup, click **Configure** on the integration card to adjust:

- **Update interval** — how often data is fetched (default: 30 minutes, minimum: 5 minutes)
- **Team URL override** — manually set the team page URL

---

## Troubleshooting

### Login fails
- Verify credentials at [voetbal.nl/inloggen](https://www.voetbal.nl/inloggen) in a browser.
- voetbal.nl uses a separate credential store from mijn.knvb.nl — [register here](https://www.voetbal.nl/gebruiker/aanmaken) if needed.

### Team not found
- Try a shorter search term (e.g. just the club abbreviation or `VR1`).
- Paste the team URL directly into the *Team URL* field.

### Wrong match shown / sensor shows "No upcoming matches"

Since `1.1.1-mh.1` the configured team name no longer has to match the name on voetbal.nl: the own team is recognised by the `my-team` marker voetbal.nl adds for the logged-in user, and in the standings by the team ID from the configured team URL. Name matching is only used as a fallback.

If the sensors still stay empty:

- Make sure the **Team URL** points at the right team (`https://www.voetbal.nl/team/T…/overzicht`); the team ID in it is what identifies your team in the standings.
- Check that the poule actually has fixtures published — before the season starts the schedule page can legitimately be empty.
- Enable debug logging and look for `Resolved configured team '…' to voetbal.nl team '…'` (the name that was resolved) and `No upcoming match found for` (the team names present in the schedule).

### No data / empty sensors

- The integration scrapes voetbal.nl HTML. If the site changes its layout the parsers may need updating — please open an issue.
- Enable debug logging:

```yaml
logger:
  logs:
    custom_components.voetbalnl: debug
```

---

## Requirements

- Home Assistant 2024.1 or newer
- `beautifulsoup4 >= 4.12.0` (installed automatically)
- A free [voetbal.nl account](https://www.voetbal.nl/gebruiker/aanmaken)

---

## License

MIT — see [LICENSE](LICENSE).
