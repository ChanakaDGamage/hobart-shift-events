"""Attach reviewed attendance references without presenting them as live counts.

Reference check dates are fixed in config. A routine feed refresh never renews
them. Organiser estimates, historical attendance and capacity stay separate.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from urllib.parse import urlsplit

KINDS = {"organiser_estimate", "historical_attendance", "venue_capacity"}


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Attendance reference timestamp needs a timezone")
    return result


def validate_references(references):
    if not isinstance(references, list):
        raise ValueError("Attendance references must be a list")
    seen = set()
    for entry in references:
        if not isinstance(entry, dict) or entry.get("id") in seen:
            raise ValueError("Attendance reference IDs must be unique")
        seen.add(entry.get("id"))
        if entry.get("kind") not in KINDS:
            raise ValueError("Unrecognised attendance reference kind")
        for field in ("id", "text", "note", "source_name", "source_url", "checked_at", "review_after"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError("Attendance reference field missing: " + field)
        url = urlsplit(entry["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Attendance reference link must be public HTTPS")
        if instant(entry["review_after"]) <= instant(entry["checked_at"]):
            raise ValueError("Attendance review date must follow its check date")
        match = entry.get("match", {})
        if not match or not set(match) <= {"id", "source_id", "start_date", "end_date", "venue", "category"}:
            raise ValueError("Invalid attendance matching fields")
        if not all(isinstance(value, str) and value for value in match.values()):
            raise ValueError("Invalid attendance matching value")
        if entry["kind"] == "venue_capacity":
            if "venue" not in match:
                raise ValueError("Capacity must refer to a named venue")
        elif not {"id", "start_date", "venue"} <= set(match):
            raise ValueError("Attendance figures must match the exact event, date and venue")


def attach_attendance(feed, references):
    validate_references(references)
    for event in feed["events"]:
        details = []
        # Existing live parser figures take priority over a researched forecast.
        count = event.get("expected_attendance")
        if isinstance(count, dict) and count.get("basis") == "organiser_expectation":
            value = count.get("value")
            prefix = {"more_than": "More than", "approximately": "About"}.get(count.get("qualifier"))
            if type(value) is int and value > 0 and prefix and event.get("last_seen_at"):
                checked = instant(event["last_seen_at"])
                details.append({
                    "id": "published:" + event["id"], "kind": "organiser_estimate",
                    "text": f"{prefix} {value:,} {'delegates' if event.get('category') == 'Conference' else 'people'} expected",
                    "note": "Organiser forecast for the whole event, not a daily count.",
                    "source_name": event["source_name"], "source_url": event["source_url"],
                    "checked_at": event["last_seen_at"],
                    "review_after": (checked + timedelta(hours=12)).isoformat(timespec="seconds"),
                    "verification": event.get("verification", "needs_review"),
                    "update_method": "event_source_parser",
                })
        for reference in references:
            if not all(event.get(field) == value for field, value in reference["match"].items()):
                continue
            if reference["kind"] == "organiser_estimate" and any(d["kind"] == "organiser_estimate" for d in details):
                continue
            detail = deepcopy({key: value for key, value in reference.items() if key not in ("match", "evidence")})
            detail.update(verification="checked", update_method="reviewed_reference")
            details.append(detail)
        # Rebuild this list, so moved/removed references cannot survive a merge.
        event["attendance_details"] = details
    feed["attendance_note"] = (
        "Organiser forecasts, previous attendance and venue capacities are separate. "
        "Reviewed references retain their research check date; feed refreshes do not recheck them. "
        "These figures do not estimate mainland visitors, airport passengers or Uber bookings.")
    return feed
