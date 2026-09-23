"""Ticketmaster Discovery events for the Hobart area. The key stays in the runner.

Only selected public event fields are emitted. Raw API responses, request URLs
and exception messages are never written to the feed or logs.
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
import re
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

HOBART = ZoneInfo("Australia/Hobart")
ENDPOINT = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 200
MAX_PAGES = 5  # Discovery supports at most 1,000 results per search.
LOCAL_CITIES = {"hobart", "north hobart", "south hobart", "west hobart", "sandy bay",
               "battery point", "glenorchy", "moonah", "bellerive", "rosny park",
               "new town", "derwent park", "claremont", "kingston", "cambridge"}
STATUSES = {"onsale": "listed", "offsale": "listed", "canceled": "cancelled",
            "cancelled": "cancelled", "postponed": "postponed", "rescheduled": "rescheduled"}


class TicketmasterError(ValueError):
    """Messages are fixed strings, never request URLs or provider response text."""


def geohash(latitude, longitude, precision=7):
    alphabet = "0123456789bcdefghjkmnpqrstuvwxyz"
    ranges = [[-180.0, 180.0], [-90.0, 90.0]]
    coords = [longitude, latitude]
    bits, value, output = 0, 0, ""
    for index in range(precision * 5):
        axis = index % 2
        mid = sum(ranges[axis]) / 2
        high = coords[axis] >= mid
        value = (value << 1) | int(high)
        ranges[axis][0 if high else 1] = mid
        bits += 1
        if bits == 5:
            output += alphabet[value]
            bits, value = 0, 0
    return output


def nearby(venue, source):
    if venue.get("country", {}).get("countryCode") != "AU":
        return False
    location = venue.get("location", {})
    if location.get("latitude") is not None and location.get("longitude") is not None:
        lat, lon = float(location["latitude"]), float(location["longitude"])
        if not math.isfinite(lat + lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise TicketmasterError("Invalid venue coordinates")
        a, b = math.radians(source["latitude"]), math.radians(lat)
        delta = math.radians(lon - source["longitude"])
        h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(delta / 2) ** 2
        distance = 6371 * 2 * math.asin(math.sqrt(min(1, max(0, h))))
        return distance <= source["radius_km"]
    state = venue.get("state", {})
    city = venue.get("city", {}).get("name", "").strip().lower()
    if (state.get("stateCode", "").upper() == "TAS" or
            state.get("name", "").lower() == "tasmania") and city in LOCAL_CITIES:
        return True
    raise TicketmasterError("Venue location cannot be verified as Hobart")


def calendar_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TicketmasterError("Event date missing or invalid")
    return date.fromisoformat(value)


def event_clock(part, day):
    if part.get("timeTBA") or part.get("noSpecificTime") or part.get("approximate"):
        return None
    if part.get("dateTime"):
        result = datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise TicketmasterError("Event timestamp has no timezone")
        result = result.astimezone(HOBART)
        if part.get("localTime") and result.time() != time.fromisoformat(part["localTime"]):
            raise TicketmasterError("Local clock disagrees with event timestamp")
    elif part.get("localTime"):
        clock = time.fromisoformat(part["localTime"])
        if clock.tzinfo is not None:
            raise TicketmasterError("Unexpected local time offset")
        result = datetime.combine(day, clock, HOBART)
        if (result.utcoffset() != result.replace(fold=1).utcoffset() or
                result.astimezone(timezone.utc).astimezone(HOBART) != result):
            raise TicketmasterError("Ambiguous daylight saving time needs a UTC timestamp")
    else:
        return None
    if result.date() != day:
        raise TicketmasterError("Local date disagrees with event timestamp")
    return result


def classification(raw):
    items = raw.get("classifications", [])
    primary = next((c for c in items if c.get("primary")), items[0] if items else {})
    names = {str(primary.get(field, {}).get("name", "")).lower()
             for field in ("segment", "genre", "subGenre", "type", "subType")}
    if names & {"parking", "merchandise", "donation", "upsell"}:
        return None
    if "comedy" in names:
        return "Comedy"
    if "music" in names:
        return "Music"
    if primary.get("family"):
        return "Family"
    if names & {"arts & theatre", "theatre", "theater"}:
        return "Arts & theatre"
    if "sports" in names:
        title = raw.get("name", "").lower()
        if "basketball" in names and ("jackjumpers" in title or re.search(r"\bnbl\b", title)):
            return "NBL"
        if re.search(r"\baflw\b", title):
            return "AFLW"
        if "afl" in names or "australian rules football" in names:
            return "AFL"
        return "Sport"
    return "Other"


def parse_event(raw, source):
    if raw.get("test") or raw.get("type", "event") != "event":
        return None
    category = classification(raw)
    if category is None:
        return None
    venues = raw.get("_embedded", {}).get("venues", [])
    if len(venues) != 1:
        raise TicketmasterError("Expected one physical event venue")
    venue = venues[0]
    if not nearby(venue, source):
        return None
    title = raw.get("name")
    venue_name = venue.get("name")
    if not isinstance(title, str) or not title.strip() or not isinstance(venue_name, str) or not venue_name.strip():
        raise TicketmasterError("Event title or venue missing")
    dates = raw["dates"]
    start = dates["start"]
    if start.get("dateTBA") or start.get("dateTBD"):
        raise TicketmasterError("Event date is to be confirmed; previous details need review")
    day = calendar_day(start.get("localDate"))
    starts = event_clock(start, day)
    end = dates.get("end", {})
    end_day, ends = day, None
    if end and not end.get("approximate"):
        end_day = calendar_day(end["localDate"]) if end.get("localDate") else day
        ends = event_clock(end, end_day)
    elif dates.get("spanMultipleDays"):
        raise TicketmasterError("Multi-day event has no confirmed end date")
    if end_day < day or (ends and starts and ends < starts):
        raise TicketmasterError("Event finish precedes its start")
    status = dates.get("status", {}).get("code")
    if status not in STATUSES:
        raise TicketmasterError("Unrecognised event status")
    link = urlsplit(raw.get("url", ""))
    if link.scheme != "https" or not link.hostname or link.username or link.password:
        raise TicketmasterError("Public event link missing or invalid")
    # Only a public event URL is saved; remove query parameters and fragments.
    public_url = urlunsplit(("https", link.netloc, link.path, "", ""))
    return {
        "id": source["id"] + ":" + raw["id"], "source_id": source["id"],
        "source_name": source["name"], "source_url": public_url,
        "category": category, "title": title.strip(), "venue": venue_name.strip(),
        "city": "Hobart", "locality": venue.get("city", {}).get("name"),
        "timezone": "Australia/Hobart", "start_date": day.isoformat(), "end_date": end_day.isoformat(),
        "start_at": starts.isoformat() if starts else None, "end_at": ends.isoformat() if ends else None,
        "status": STATUSES[status], "status_evidence": "Ticketmaster dates.status.code=" + status,
        "ticketmaster_status": status, "expected_attendance": None,
    }


def fetch_page(parameters, key):
    query = urlencode(dict(parameters, apikey=key))
    request = Request(ENDPOINT + "?" + query, headers={
        "User-Agent": "HobartShiftEvents/1.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise TicketmasterError("Ticketmaster response exceeds size limit")
        # Never retain reflected credentials in provider errors or event fields.
        cleaned = content.decode("utf-8").replace(key, "[redacted]")
        return json.loads(cleaned)
    except HTTPError as error:
        raise TicketmasterError(f"Ticketmaster HTTP {error.code}; check key, quota and provider status") from None
    except TicketmasterError:
        raise
    except Exception:
        raise TicketmasterError("Ticketmaster request failed or returned invalid JSON") from None


def collect_ticketmaster(source, now=None, fixture_dir=None):
    try:
        key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
        if not key and fixture_dir is None:
            raise TicketmasterError("Add the TICKETMASTER_API_KEY repository secret to enable this source")
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(HOBART).date()
        lower = datetime.combine(today - timedelta(days=30), time.min, HOBART)
        upper = datetime.combine(today + timedelta(days=source.get("lookahead_days", 365) + 1), time.min, HOBART)
        utc_text = lambda value: value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parameters = {"countryCode": "AU", "geoPoint": geohash(source["latitude"], source["longitude"]),
                      "radius": source["radius_km"], "unit": "km", "size": PAGE_SIZE, "sort": "date,asc",
                      "startDateTime": utc_text(lower), "endDateTime": utc_text(upper),
                      "includeTBA": "no", "includeTBD": "no", "includeTest": "no"}
        fixtures = json.loads((fixture_dir / "ticketmaster.json").read_text()) if fixture_dir else None
        raw_events, total, pages = {}, None, None
        for page in range(MAX_PAGES):
            if page and not fixtures:
                sleep(0.25)
            payload = fixtures[page] if fixtures else fetch_page(dict(parameters, page=page), key)
            if not isinstance(payload, dict) or "fault" in payload or "errors" in payload:
                raise TicketmasterError("Ticketmaster returned an API error")
            meta = payload.get("page", {})
            current_total, current_pages = meta.get("totalElements"), meta.get("totalPages")
            if (type(current_total) is not int or type(current_pages) is not int or
                    not 0 <= current_total <= 1000 or not 0 <= current_pages <= MAX_PAGES or
                    meta.get("number") != page or meta.get("size") != PAGE_SIZE or
                    current_pages != math.ceil(current_total / PAGE_SIZE)):
                raise TicketmasterError("Ticketmaster paging is invalid or exceeds 1,000 results; narrow the search")
            if total is not None and (total, pages) != (current_total, current_pages):
                raise TicketmasterError("Ticketmaster listings changed during paging; retry next update")
            total, pages = current_total, current_pages
            batch = payload.get("_embedded", {}).get("events", [])
            if not isinstance(batch, list) or len(batch) != min(PAGE_SIZE, max(0, total - page * PAGE_SIZE)):
                raise TicketmasterError("Ticketmaster returned an incomplete page")
            for raw in batch:
                event_id = raw.get("id")
                if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", event_id):
                    raise TicketmasterError("Ticketmaster event identifier missing or invalid")
                if event_id in raw_events:
                    raise TicketmasterError("Repeated Ticketmaster identifier across pages; retry next update")
                raw_events[event_id] = raw
            if page + 1 >= pages:
                break
        if len(raw_events) != total:
            raise TicketmasterError("Ticketmaster collection is incomplete")
        events, observed, issues = [], set(), []
        for event_id, raw in raw_events.items():
            observed.add(source["id"] + ":" + event_id)
            try:
                item = parse_event(raw, source)
                if item:
                    events.append(item)
            except Exception:
                # Do not echo arbitrary API content in an error or traceback.
                issues.append(f"Ticketmaster event {event_id}: date, time, venue or status needs review")
        digest = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()
        return {"events": events, "observed": observed, "issues": issues,
                "status": "needs_review" if issues else "ok", "content_hash": digest}
    except Exception as error:
        message = str(error) if isinstance(error, TicketmasterError) else "Ticketmaster response could not be safely processed"
        return {"events": [], "observed": set(), "issues": [message], "status": "error"}
