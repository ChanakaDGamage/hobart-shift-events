"""Retain HBA's published flights after they leave its rolling public feed."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE = "https://hobartairport.com.au/wp-json/hba/v1/timetable/"
HOBART = ZoneInfo("Australia/Hobart")


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp requires a time zone")
    return result.astimezone(timezone.utc)


def day(value):
    return instant(value).astimezone(HOBART).date()


def validate_row(row, direction, archived=False):
    if not isinstance(row, dict) or not isinstance(row.get("flight_number"), str) or not row["flight_number"].strip():
        raise ValueError("Invalid flight number")
    instant(row.get("scheduled_time"))
    if row.get("estimated_time"):
        instant(row["estimated_time"])
    if archived:
        instant(row.get("last_seen_at"))
    city = row.get("from" if direction == "arrivals" else "to")
    if not isinstance(city, str) or not city.strip():
        raise ValueError("Missing flight route")
    if not isinstance(row.get("status", ""), str):
        raise ValueError("Invalid flight status")
    return row


def key(row):
    return row["flight_number"].strip().upper(), instant(row["scheduled_time"])


def daily_key(row, direction):
    return (row["flight_number"].strip().upper(), day(row["scheduled_time"]),
            row["from" if direction == "arrivals" else "to"].strip().lower())


def merge_archive(previous, incoming, now):
    now = now.astimezone(timezone.utc)
    if not isinstance(incoming, dict):
        raise ValueError("Invalid airport feed")
    published = instant(incoming.get("last_refresh"))
    if now - published > timedelta(minutes=30) or published - now > timedelta(minutes=5):
        raise ValueError("Airport feed timestamp is stale or in the future")
    if previous:
        if previous.get("schema_version") != 1:
            raise ValueError("Unknown archive version")
        if instant(previous["last_refresh"]) > published:
            raise ValueError("Airport returned an older snapshot")
        started = instant(previous["tracking_started_at"])
    else:
        started = now
    today = now.astimezone(HOBART).date()
    oldest, newest = today - timedelta(days=1), today + timedelta(days=8)
    result = {
        "schema_version": 1,
        "source": SOURCE,
        "timezone": "Australia/Hobart",
        "tracking_started_at": started.isoformat(),
        "collected_at": now.isoformat(),
        "last_refresh": incoming["last_refresh"],
    }
    for direction in ("arrivals", "departures"):
        rows = incoming.get(direction)
        if not isinstance(rows, list):
            raise ValueError(f"Missing {direction}")
        old_rows = previous.get(direction, []) if previous else []
        if not isinstance(old_rows, list):
            raise ValueError("Invalid archive flights")
        records = {}
        for row in old_rows:
            validate_row(row, direction, archived=True)
            if oldest <= day(row["scheduled_time"]) <= newest:
                records[key(row)] = dict(row)
        for row in rows:
            validate_row(row, direction)
        # A changed scheduled time for the same daily service replaces its
        # earlier schedule. Separate services still present in a feed survive.
        fresh_keys = {key(row) for row in rows}
        fresh_days = {daily_key(row, direction) for row in rows}
        records = {k: row for k, row in records.items()
                   if k in fresh_keys or daily_key(row, direction) not in fresh_days}
        for row in rows:
            if oldest <= day(row["scheduled_time"]) <= newest:
                clean = {field: row[field] for field in (
                    "flight_number", "airline", "from", "to", "scheduled_time",
                    "estimated_time", "status", "gate", "carousel", "codeshare_flights"
                ) if field in row}
                clean["last_seen_at"] = published.isoformat()
                records[key(row)] = clean
        result[direction] = sorted(records.values(), key=lambda row: (instant(row["scheduled_time"]), row["flight_number"]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/flights.json"))
    args = parser.parse_args()
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    request = Request(SOURCE, headers={"User-Agent": "HobartShift/1.0 flight archive"})
    with urlopen(request, timeout=25) as response:
        incoming = json.load(response)
    archive = merge_archive(previous, incoming, datetime.now(timezone.utc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(archive, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(args.output)
    print(f"Saved {len(archive['arrivals'])} arrivals and {len(archive['departures'])} departures.")


if __name__ == "__main__":
    main()
