"""Read a small, explicit set of public official event sources into one JSON feed.

No paid API, search service or AI service. Run with Python 3.12+.
Website HTML can change; parsing failures preserve prior data and mark it stale.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from lxml import html

if __package__:
    from .ticketmaster import collect_ticketmaster
else:
    from ticketmaster import collect_ticketmaster

ROOT = Path(__file__).resolve().parents[1]
HOBART = ZoneInfo("Australia/Hobart")
MONTHS = {name.lower(): i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
MONTH_PATTERN = "(?:" + "|".join(MONTHS) + ")"
VENUES = {
    "mystate bank arena": "MyState Bank Arena",
    "north hobart oval": "North Hobart Oval",
    "ninja stadium": "Ninja Stadium",
    "bellerive oval": "Ninja Stadium",
    "wrest point": "Wrest Point",
    "wrest point hotel": "Wrest Point",
    "hotel grand chancellor": "Hotel Grand Chancellor",
    "hotel grand chancellor hobart": "Hotel Grand Chancellor",
    "crowne plaza": "Crowne Plaza",
    "crowne plaza hobart": "Crowne Plaza",
    "n j edwards hub - the hutchins school": "N J Edwards Hub, The Hutchins School",
}


class SourceFormatError(ValueError):
    pass


def text(node):
    return " ".join(" ".join(node.itertext()).split())


def one(root, xpath):
    nodes = root.xpath(xpath)
    if len(nodes) != 1:
        raise SourceFormatError(f"Expected one event field; found {len(nodes)}")
    return text(nodes[0])


def require_match(pattern, value):
    match = re.search(pattern, value, re.I)
    if not match:
        raise SourceFormatError("Expected date or venue text is missing or has changed format")
    return match


def venue_name(value):
    name = value.strip().split(",")[0].strip()
    canonical = VENUES.get(name.lower())
    if not canonical:
        raise SourceFormatError(f"Venue needs review: {name[:100]}")
    return canonical


def day_range(value, year):
    """Only a complete same-month range is accepted; cross-month changes need review."""
    match = re.fullmatch(
        rf"\s*(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})\s+({MONTH_PATTERN})\s+(\d{{4}})\s*",
        value, re.I)
    if not match:
        raise SourceFormatError("Conference date range needs review")
    first, last, month, actual_year = match.groups()
    if int(actual_year) != year:
        raise SourceFormatError("Source now refers to another conference year; review before importing")
    start = date(year, MONTHS[month.lower()], int(first))
    end = date(year, MONTHS[month.lower()], int(last))
    if end < start or (end - start).days > 14:
        raise SourceFormatError("Conference date range is invalid")
    return start, end


def clock_at(day, clock):
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*(am|pm)\s*", clock, re.I)
    if not match:
        raise SourceFormatError("Start time needs review")
    hour, minute, period = match.groups()
    hour, minute = int(hour), int(minute)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise SourceFormatError("Invalid start time")
    hour = hour % 12 + (12 if period.lower() == "pm" else 0)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=HOBART)


def explicit_status(value, row=False):
    # The words in a cancellation policy or deadline do not describe event status.
    pattern = (r"\b(cancelled|canceled|postponed)\b" if row else
               r"(?:^|[.!?]\s+)this (?:conference|congress|meeting|event) (?:has been|is)\s+"
               r"(cancelled|canceled|postponed)\b")
    match = re.search(pattern, value, re.I)
    if not match:
        return "listed", None
    word = match.group(1).lower()
    return ("cancelled" if word in ("cancelled", "canceled") else "postponed"), match.group(0)


def event(source, key, title, venue, start, end, category, start_at=None,
          status="listed", evidence=None, url=None, attendance=None):
    return {
        "id": source["id"] + ":" + key,
        "source_id": source["id"], "source_name": source["name"],
        "source_url": url or source["url"], "category": category,
        "title": title, "venue": venue, "city": "Hobart",
        "timezone": "Australia/Hobart", "start_date": start.isoformat(),
        "end_date": end.isoformat(), "start_at": start_at.isoformat() if start_at else None,
        "end_at": None, "status": status, "status_evidence": evidence,
        "expected_attendance": attendance,
    }


def parse_jackjumpers(root, source):
    rows = root.xpath('//a[@data-id][.//*[@fs-list-field="venue"]]')
    if not rows:
        raise SourceFormatError("No fixture rows found")
    events, observed, issues = [], set(), []
    for row in rows:
        key = row.get("data-id")
        observed.add(source["id"] + ":" + key)
        try:
            venue = one(row, './/*[@fs-list-field="venue"]')
            if venue.lower() not in VENUES:
                continue  # Explicit whitelist excludes Launceston and away fixtures.
            venue = venue_name(venue)
            raw_date = one(row, './/*[@fs-list-field="date"]')
            match = require_match(r"^(\d{2}) (\w+) (\d{2})$", raw_date)
            d, month, year = match.groups()
            day = date(2000 + int(year), MONTHS[month.lower()], int(d))
            status, evidence = explicit_status(text(row), row=True)
            raw_time = one(row, './/*[@fs-list-field="time"]')
            start_at = None if raw_time.strip().upper() in ("TBC", "TBD", "") else clock_at(day, raw_time)
            teams = [text(n) for n in row.xpath('.//*[@fs-list-field="team"]')]
            if len(teams) != 2:
                raise SourceFormatError("Expected two teams")
            events.append(event(source, key, " v ".join(teams), venue, day, day, "NBL",
                                start_at, status, evidence, urljoin(source["url"], row.get("href", ""))))
        except (ValueError, KeyError) as error:
            issues.append(f"Fixture {key}: {error}")
    if not events:
        issues.append("No Hobart games recognised; check venues and the current season schedule")
    return events, observed, issues


def parse_aflw(root, source):
    heading = one(root, '//h1[contains(@class,"article__heading")]')
    if "AFLW fixture" not in heading:
        raise SourceFormatError("Expected AFLW fixture article not found")
    published = root.xpath('//time/@datetime')
    if not published or not published[0].startswith(str(source["year"]) + "-"):
        raise SourceFormatError("Fixture article publication year changed")
    rounds = root.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," article__body ")]//h4')
    rounds = [node for node in rounds if re.fullmatch(r"Round \d+", text(node))]
    if not rounds:
        raise SourceFormatError("No round headings found")
    events, observed, issues = [], set(), []
    for node in rounds:
        key = text(node).lower().replace(" ", "-")
        observed.add(source["id"] + ":" + key)
        paragraph = node.getnext()
        if paragraph is None or paragraph.tag != "p":
            issues.append(f"{key}: fixture paragraph missing")
            continue
        value = text(paragraph)
        if "Hobart" not in value:
            continue
        try:
            match = require_match(
                rf"^(.+?)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
                rf"({MONTH_PATTERN})\s+(\d{{1,2}})\s+(\d{{1,2}}[.:]\d{{2}}\s*[ap]m)\s+"
                r"(AE[DS]T)\s*\|\s*(.+)$", value)
            title, month, day, clock, abbreviation, raw_venue = match.groups()
            day = date(source["year"], MONTHS[month.lower()], int(day))
            start_at = clock_at(day, clock)
            if start_at.tzname() != abbreviation:
                raise SourceFormatError("Published timezone conflicts with Hobart daylight saving")
            status, evidence = explicit_status(value, row=True)
            events.append(event(source, key, title, venue_name(raw_venue), day, day,
                                "AFLW", start_at, status, evidence))
        except (ValueError, KeyError) as error:
            issues.append(f"{key}: {error}")
    return events, observed, issues


def parse_conference(root, source):
    adapter, year = source["adapter"], source["year"]
    body = text(root)
    attendance = None
    if adapter == "ancold":
        one(root, f'//h1[contains(.,"ANCOLD {year}")]')
        intro = one(root, '//p[contains(.,"between") and contains(.,"ANCOLD") and contains(.,"Conference")]')
        match = require_match(rf"between\s+(\d{{1,2}}\s*[-–—]\s*\d{{1,2}}\s+{MONTH_PATTERN})", intro)
        start, end = day_range(match.group(1) + f" {year}", year)
        where = one(root, '//p[contains(.,"conference will be held at")]')
        raw_venue = require_match(r"held at (?:the iconic |the )?(.+?) on the", where).group(1)
        count = re.search(r"expect over ([\d,]+) delegates", body, re.I)
        if count:
            attendance = {"value": int(count.group(1).replace(",", "")),
                          "qualifier": "more_than", "basis": "organiser_expectation"}
    elif adapter == "nedc":
        title = one(root, "//h1")
        if str(year) not in title:
            raise SourceFormatError("Conference year changed")
        candidates = [text(node) for node in root.xpath('//h2')
                      if re.fullmatch(rf"\d{{1,2}}\s*[-–—]\s*\d{{1,2}}\s+{MONTH_PATTERN}\s+\d{{4}}", text(node), re.I)]
        if len(candidates) != 1:
            raise SourceFormatError("Conference date heading needs review")
        start, end = day_range(candidates[0], year)
        raw_venue = one(root, '//h2[contains(.,"Hobart")]').split(",")[0]
    elif adapter == "ohaa":
        one(root, f'//h2[contains(.,"OHAA Congress {year}")]')
        when = one(root, '//*[substring(@id,string-length(@id)-7)="WhenData"]')
        match = require_match(r"^(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})$", when)
        start, end = [datetime.strptime(value, "%d/%m/%Y").date() for value in match.groups()]
        address_nodes = root.xpath('//*[substring(@id,string-length(@id)-10)="AddressData"]')
        if len(address_nodes) != 1 or "Hobart" not in text(address_nodes[0]):
            raise SourceFormatError("Congress venue needs review")
        raw_venue = address_nodes[0].text_content().strip().splitlines()[0]
    elif adapter == "hith":
        intro = one(root, '//p[contains(.,"will be held at") and contains(.,"Annual Scientific Meeting")]')
        match = require_match(rf"from\s+(\d{{1,2}}\s*[-–—]\s*\d{{1,2}}\s+{MONTH_PATTERN}\s+\d{{4}})", intro)
        start, end = day_range(match.group(1), year)
        raw_venue = require_match(r"held at (?:the )?(.+?), Hobart", intro).group(1)
    elif adapter == "ihhc":
        one(root, f'//h3[contains(.,"Conference Preview {year}")]')
        intro = one(root, '//p[contains(.,"packed program of events running")]')
        match = require_match(rf"running from\s+({MONTH_PATTERN})\s+(\d{{1,2}})\s+to\s+(\d{{1,2}})\s+at\s+(.+?), Hobart", intro)
        month, first, last, raw_venue = match.groups()
        start, end = day_range(f"{first}-{last} {month} {year}", year)
    else:
        raise SourceFormatError("Unknown source adapter")
    if start.year != year or end.year != year or not 0 <= (end - start).days <= 14:
        raise SourceFormatError("Conference dates need review")
    status, evidence = explicit_status(body)
    item = event(source, "conference", source["title"], venue_name(raw_venue), start, end,
                 "Conference", status=status, evidence=evidence, attendance=attendance)
    return [item], {item["id"]}, []


def parse_document(content, source):
    root = html.fromstring(content)
    for node in root.xpath('//script|//style|//nav|//footer'):
        node.drop_tree()
    if source["adapter"] == "jackjumpers":
        result = parse_jackjumpers(root, source)
    elif source["adapter"] == "aflw":
        result = parse_aflw(root, source)
    else:
        result = parse_conference(root, source)
    ids = [item["id"] for item in result[0]]
    if len(ids) != len(set(ids)):
        raise SourceFormatError("Duplicate event identifiers in source")
    return result


def fetch(source):
    req = Request(source["url"], headers={
        "User-Agent": "HobartShiftEvents/1.0 (public event timetable checker; four checks daily)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urlopen(req, timeout=25) as response:
        content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise SourceFormatError("Source page exceeds size limit")
        return content.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def collect(source, fixture_dir=None, now=None):
    if source["adapter"] == "ticketmaster":
        return collect_ticketmaster(source, now, fixture_dir)
    try:
        content = ((fixture_dir / (source["adapter"] + ".html")).read_text()
                   if fixture_dir else fetch(source))
        events, observed, issues = parse_document(content, source)
        return {"events": events, "observed": observed, "issues": issues,
                "status": "needs_review" if issues else "ok",
                "content_hash": hashlib.sha256(content.encode()).hexdigest()}
    except Exception as error:
        # No deletion, guessed event status, or false freshness after a failed check.
        return {"events": [], "observed": set(), "issues": [f"{type(error).__name__}: {str(error)[:220]}"],
                "status": "error"}


def merge_feed(previous, sources, results, now):
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    cutoff = (now.astimezone(HOBART).date() - timedelta(days=30)).isoformat()
    old_sources = {item["id"]: item for item in previous.get("sources", [])}
    records = {item["id"]: deepcopy(item) for item in previous.get("events", [])}
    old_records = deepcopy(records)
    changes = list(previous.get("changes", []))
    source_states = []
    fields = ("title", "venue", "start_date", "end_date", "start_at", "end_at", "status", "expected_attendance")
    for source, result in zip(sources, results, strict=True):
        issues = list(result["issues"])
        previous_source = old_sources.get(source["id"], {})
        parsed_ids = {item["id"] for item in result["events"]}
        for key, old in list(records.items()):
            if old["source_id"] != source["id"] or key in parsed_ids:
                continue
            old["last_checked_at"] = stamp
            if result["status"] == "error":
                old["verification"] = "source_unavailable"
            elif key in result["observed"]:
                old["verification"] = "needs_review"
                issues.append(f"Previously listed Hobart event {key} changed venue or could not be parsed")
            else:
                old["verification"] = "not_in_latest_listing"
                if old["end_date"] >= now.astimezone(HOBART).date().isoformat():
                    issues.append(f"Future event {key} is no longer listed; cancellation is not assumed")
        for item in result["events"]:
            old = old_records.get(item["id"])
            item = dict(item, verification="checked", last_seen_at=stamp, last_checked_at=stamp)
            if old and old["status"] in ("cancelled", "postponed") and item["status"] == "listed":
                # A removed label alone does not confirm reinstatement.
                item["status"] = old["status"]
                item["status_evidence"] = old.get("status_evidence")
                item["verification"] = "needs_review"
                issues.append(f"{item['id']}: previous {old['status']} status needs manual review")
            item["first_seen_at"] = old.get("first_seen_at", stamp) if old else stamp
            modified = {field: {"before": old.get(field), "after": item.get(field)}
                        for field in fields if old and old.get(field) != item.get(field)}
            if old is None or modified:
                changes.append({"at": stamp, "event_id": item["id"],
                                "kind": "added" if old is None else "changed", "fields": modified})
            records[item["id"]] = item
        state = {key: source[key] for key in ("id", "name", "url", "coverage")}
        state.update(last_attempt_at=stamp,
                     last_success_at=stamp if result["status"] == "ok" and not issues else previous_source.get("last_success_at"),
                     status="error" if result["status"] == "error" else "needs_review" if issues else "ok",
                     event_count=len(result["events"]), issues=issues)
        source_states.append(state)
    enabled = {source["id"] for source in sources}
    for item in records.values():
        if item["source_id"] not in enabled:
            item["verification"] = "source_removed"
    # The feed keeps a short past history; applications should filter end_date >= Hobart today.
    events = sorted((item for item in records.values() if item["end_date"] >= cutoff),
                    key=lambda item: (item["start_date"], item.get("start_at") or "", item["id"]))
    mark_duplicate_listings(events, source_states, stamp)
    return {
        "schema_version": 1, "generated_at": stamp, "timezone": "Australia/Hobart",
        "recommended_refresh_seconds": 21600, "stale_after_seconds": 43200,
        "coverage": "Connected sources only: named conferences, the current JackJumpers schedule, "
                    "Hobart entries in a 2026 AFLW article, and Ticketmaster listings within 30 km of Hobart "
                    "when the API key is configured. Ticketmaster search extends 365 days ahead. "
                    "Not all Hobart events or all AFL fixtures.",
        "status_note": "Listed means present in the source, not a guarantee the event will proceed. "
                       "Missing events are not treated as cancelled. Status detection is limited to explicit "
                       "text recognised by the adapters and Ticketmaster status codes; review official "
                       "announcements for urgent changes. Ticket sales status does not measure attendance.",
        "sources": source_states, "events": events, "changes": changes[-100:],
    }


def mark_duplicate_listings(events, sources, stamp):
    """Hide only exact, freshly checked matches. Conflicting sources remain visible."""
    def normal(value):
        value = re.sub(r"\b(?:vs|versus)\b\.?", "v", value.lower())
        return re.sub(r"[^a-z0-9]+", "", value)

    groups = {}
    for item in events:
        item.pop("duplicate_of", None)
        if item.get("verification") != "checked" or item.get("last_seen_at") != stamp or not item.get("start_at"):
            continue
        venue = VENUES.get(item["venue"].lower(), item["venue"])
        key = (normal(item["title"]), normal(venue), item["start_date"], item["end_date"],
               datetime.fromisoformat(item["start_at"]).astimezone(timezone.utc))
        groups.setdefault(key, []).append(item)
    for items in groups.values():
        if len(items) < 2 or not any(item["source_id"] == "ticketmaster" for item in items):
            continue
        if len({item["status"] for item in items}) > 1:
            for item in items:
                item["verification"] = "needs_review"
                for source in sources:
                    if source["id"] == item["source_id"]:
                        source["status"] = "needs_review"
                        source["issues"].append(f"{item['id']}: matching listings disagree on status")
            continue
        chosen = sorted(items, key=lambda item: (item["source_id"] == "ticketmaster", item["id"]))[0]
        for item in items:
            if item is not chosen:
                item["duplicate_of"] = chosen["id"]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "data/events.json")
    parser.add_argument("--fixture-dir", type=Path, help="Offline parser verification; never used by the scheduled job")
    parser.add_argument("--now", help="ISO timestamp for reproducible local verification")
    args = parser.parse_args()
    sources = json.loads((ROOT / "config/sources.json").read_text())
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        parser.error("--now must include a timezone")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda source: collect(source, args.fixture_dir, now), sources))
    feed = merge_feed(previous, sources, results, now)
    write_json(args.output, feed)
    health = {"generated_at": feed["generated_at"],
              "status": "ok" if all(s["status"] == "ok" for s in feed["sources"]) else "needs_review",
              "event_count": len(feed["events"]), "sources": feed["sources"]}
    write_json(args.output.with_name("health.json"), health)
    for source in feed["sources"]:
        print(f"{source['id']}: {source['status']}, {source['event_count']} parsed events")
        for issue in source["issues"]:
            print("  " + issue)
    print(f"Saved {len(feed['events'])} events to {args.output}")
    # Publish available data first. The workflow separately fails its health step to notify the owner.
    return 0


if __name__ == "__main__":
    sys.exit(main())
