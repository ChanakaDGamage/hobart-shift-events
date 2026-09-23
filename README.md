# Hobart Shift event updater

Collects public event facts into `data/events.json` for a Flutter app. The GitHub
workflow requests an update every six hours. No paid event API or AI service is used.

Start with [START_HERE.txt](START_HERE.txt). Upload this folder's contents into a
public GitHub repository named `hobart-shift-events`, on the `main` branch.
The included files alone do not activate a schedule until uploaded to GitHub.

## Included coverage

| Source | What the updater reads | Coverage limit |
| --- | --- | --- |
| [JackJumpers](https://www.jackjumpers.com.au/schedule) | Recognised Hobart venues, teams, dates and tip-off times | Current published schedule; excludes Launceston and away venues |
| [North Melbourne AFLW](https://www.nmfc.com.au/news/2027559/aflw-fixture-roos-to-launch-flag-defence-with-marvel-stadium-double-header) | Hobart fixtures from the 2026 announcement | One club article; not a live match feed or all AFL fixtures |
| [ANCOLD](https://ancold.org.au/ancold-2026-conference-hobart-tasmania/) | Conference dates, venue and published expected attendance | One 2026 announcement |
| [NEDC](https://www.edaconference.com.au/) | Main conference dates and venue | Optional tours and dinner times excluded |
| [OHAA](https://oralhealthcongress.com.au/OralHealthCongress2025/iCore/Events/Event_display.aspx?EventKey=CON261015) | Congress dates and venue | One congress |
| [HITH](https://hithsocietyconference.com.au/) | Main meeting dates and venue | Session and dinner times excluded |
| [Hospitality in Healthcare](https://www.hospitalityinhealthcaremag.org.au/49-conference-preview-2026) | Conference dates and venue | Magazine preview; later programme changes may appear elsewhere |

This is an updater for connected sources. It does not discover every new Hobart
conference, predict Uber bookings, or provide real-time event status. Organisers
may publish changes on a different page. Some sources are announcements that may
not be maintained after publication.

Only event facts and short status evidence are saved. Full source pages, images,
attendee identities and copied descriptions are not included.

## Files

- `config/sources.json`: The seven connected sources and their coverage.
- `scripts/update_events.py`: Source parsers, update handling and feed generation.
- `tests/test_updater.py`: Checks important accuracy and failure cases.
- `.github/workflows/update-events.yml`: GitHub's scheduled and manual job.
- `data/events.json`: Event feed, source health and recent detected changes.
- `data/health.json`: Compact status for troubleshooting.

The included JSON was generated from live source reads on 23 September 2026. It
is an initial snapshot, not proof that your GitHub schedule has run.

## Connect the app after the first GitHub run

In GitHub open `data/events.json`, click Raw, and copy its URL. It should resemble:

```
https://raw.githubusercontent.com/YOUR-USERNAME/hobart-shift-events/main/data/events.json
```

The Flutter Events page can request this public HTTPS URL using the existing
`http` package. No GitHub token belongs in the app. This package prepares the
feed only; it does not replace the working airport page or add a Flutter page.

The app must use the following rules:

- Decode UTF-8 JSON and require `schema_version` equal to 1.
- Filter on `end_date` being today or later in `Australia/Hobart`.
- `start_date` and `end_date` are local calendar dates, inclusive.
- `start_at` includes its UTC offset. Display it in `Australia/Hobart`, including
  daylight saving. A null start time means the source did not provide a time.
- `end_at` is null because a reliable finish time has not been imported. Do not
  invent a final whistle time or a conference closing time.
- `generated_at` is when the collector ran, not when all events were confirmed.
- Each event's `last_seen_at` records its last successful source read. Mark it
  stale after 12 hours, or whenever `verification` is not `checked`.
- Also flag the whole feed if `generated_at` is older than 12 hours. This catches
  missed jobs, failed dependency installation and disabled schedules.
- `status: listed` means the source lists the event. It is not a promise that the
  event is going ahead. Show `status` and freshness separately.
- Preserve source links so drivers can check the original listing.
- Do not treat a missing event as cancelled. `not_in_latest_listing` needs review.
- Expected attendance is an organiser estimate, not a registered delegate count,
  a count of interstate travellers or a forecast of Uber demand.
- Refresh on opening the Events page and periodically while visible. The source
  collection runs every six hours, so a minute-by-minute app request will not
  make event information real time.

## Reliability

The updater checks that expected page fields, dates, venue names and identifiers
are present. A failed source retains earlier records with a review flag. Stable
fixture identifiers prevent duplicate records after date changes. The job saves
available data before making its final health step fail when review is needed.
Open the Actions run summary to see which source needs attention.

Cancellation detection is deliberately limited. It accepts an explicit cancelled
or postponed label in a fixture row, or a direct statement about the conference.
It does not infer cancellation from a missing page, a refund policy, a registration
deadline or a failed request. Other wording can require manual review. Removing
a cancellation label does not automatically reinstate an event.

If an event was reinstated, confirm it on the official source. Back up the feed,
then remove that event record from `data/events.json` and run the updater again
so the current listing is imported. Do not clear a cancellation merely to make
the health check green.

The workflow can be delayed or skipped during GitHub load. GitHub also disables
schedules in public repositories after 60 days without repository activity.
Check the Actions tab periodically and re-enable a disabled schedule. See
[GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Standard GitHub-hosted runners in public repositories are free under the current
[GitHub Actions billing rules](https://docs.github.com/en/actions/concepts/billing-and-usage).
The job uses a standard Ubuntu runner. It does not enable paid runner types.

## Maintenance

After a conference is over, retire its source from `config/sources.json` when it
no longer needs checking. Conference homepages often switch to the following
year. The updater refuses to silently assign a new year's dates to an old event.

To add a conference with a different website format, add an explicit source
entry and a parser that extracts its published dates and venue. Validate it
against the organiser's page before enabling it. Adding a URL alone is not
enough. Add supported Hobart venues to `VENUES` after verifying their location.

This first release does not include AFL men's fixtures. Add the relevant official
2027 sources when confirmed; do not assume every Tasmania fixture is in Hobart.

## Local developer check

Python 3.12 or later is required. The scheduled Ubuntu runner includes timezone
data. On Windows, install `tzdata` as well if testing the script locally.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/update_events.py
```

The supplied checks cover stale-data retention, date changes, daylight saving,
venue filtering, duplicate identifiers, missing fixtures, explicit cancellations,
conditional policy wording, reinstatement review and conference year changes.
The GitHub-hosted workflow still needs its first run in your account to verify
repository permissions and publishing there.
