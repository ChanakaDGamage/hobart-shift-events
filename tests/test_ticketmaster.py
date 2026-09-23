from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.ticketmaster import collect_ticketmaster, fetch_page, geohash, parse_event
from scripts.update_events import merge_feed

SOURCE = {"id": "ticketmaster", "name": "Ticketmaster Discovery", "adapter": "ticketmaster",
          "url": "https://www.ticketmaster.com.au/", "coverage": "Hobart area",
          "latitude": -42.8821, "longitude": 147.3272, "radius_km": 30, "lookahead_days": 365}
NOW = datetime(2026, 9, 23, 20, tzinfo=timezone.utc)
FAKE_KEY = "synthetic-key-not-a-credential"


def raw_event(identifier="test-1", status="onsale"):
    return {"id": identifier, "name": "Example concert", "type": "event",
            "url": "https://www.ticketmaster.com.au/example/event/test-1?tracking=sample",
            "dates": {"start": {"localDate": "2026-10-16", "localTime": "19:30:00",
                                  "dateTime": "2026-10-16T08:30:00Z"},
                      "timezone": "Australia/Hobart", "status": {"code": status}},
            "classifications": [{"primary": True, "segment": {"name": "Music"},
                                  "genre": {"name": "Rock"}}],
            "_embedded": {"venues": [{"name": "Odeon Theatre", "city": {"name": "Hobart"},
                                       "country": {"countryCode": "AU"}, "state": {"stateCode": "TAS"},
                                       "location": {"latitude": "-42.885", "longitude": "147.324"}}]}}


def page(events, number=0, total=None):
    total = len(events) if total is None else total
    return {"_embedded": {"events": events},
            "page": {"size": 200, "number": number, "totalElements": total,
                     "totalPages": (total + 199) // 200}}


def collected(events):
    with patch.dict(os.environ, {"TICKETMASTER_API_KEY": FAKE_KEY}), \
            patch("scripts.ticketmaster.fetch_page", return_value=page(events)):
        return collect_ticketmaster(SOURCE, NOW)


class TicketmasterTests(unittest.TestCase):
    def test_hobart_time_status_and_no_invented_attendance(self):
        for provider, expected in [("onsale", "listed"), ("offsale", "listed"),
                                   ("canceled", "cancelled"), ("postponed", "postponed"),
                                   ("rescheduled", "rescheduled")]:
            item = parse_event(raw_event(status=provider), SOURCE)
            self.assertEqual(item["status"], expected)
            self.assertEqual(item["start_at"], "2026-10-16T19:30:00+11:00")
            self.assertIsNone(item["end_at"])
            self.assertIsNone(item["expected_attendance"])
            self.assertNotIn("?", item["source_url"])

    def test_foreign_mainland_and_launceston_venues_are_excluded(self):
        for country, lat, lon in [("US", 41.5, -87.2), ("AU", -41.44, 147.14), ("AU", -37.8, 144.96)]:
            raw = raw_event()
            venue = raw["_embedded"]["venues"][0]
            venue["country"]["countryCode"] = country
            venue["location"] = {"latitude": lat, "longitude": lon}
            self.assertIsNone(parse_event(raw, SOURCE))

    def test_glenorchy_is_included_and_unknown_location_is_flagged(self):
        raw = raw_event()
        venue = raw["_embedded"]["venues"][0]
        venue.update(name="MyState Bank Arena", city={"name": "Glenorchy"},
                     location={"latitude": -42.826, "longitude": 147.284})
        self.assertIsNotNone(parse_event(raw, SOURCE))
        venue.pop("location")
        self.assertIsNotNone(parse_event(raw, SOURCE))
        venue["city"]["name"] = "Unknown place"
        result = collected([raw])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["events"], [])

    def test_placeholder_time_is_not_displayed_and_tbd_date_is_flagged(self):
        raw = raw_event()
        raw["dates"]["start"]["timeTBA"] = True
        self.assertIsNone(parse_event(raw, SOURCE)["start_at"])
        raw["dates"]["start"]["dateTBD"] = True
        result = collected([raw])
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("ticketmaster:test-1", result["observed"])
        self.assertEqual(result["events"], [])

    def test_exact_finish_can_be_used_but_approximate_finish_is_not(self):
        raw = raw_event()
        raw["dates"]["end"] = {"localDate": "2026-10-16", "dateTime": "2026-10-16T10:30:00Z"}
        self.assertEqual(parse_event(raw, SOURCE)["end_at"], "2026-10-16T21:30:00+11:00")
        raw["dates"]["end"]["approximate"] = True
        self.assertIsNone(parse_event(raw, SOURCE)["end_at"])

    def test_clock_conflicts_unknown_status_and_invalid_dates_need_review(self):
        for field, value in [("dateTime", "2026-10-16T19:30:00Z"), ("localDate", "2026-02-31")]:
            raw = raw_event()
            raw["dates"]["start"][field] = value
            self.assertEqual(collected([raw])["status"], "needs_review")
        self.assertEqual(collected([raw_event(status="unknown")])["status"], "needs_review")

    def test_missing_key_does_not_request_network_or_erase_previous_events(self):
        before = merge_feed({}, [SOURCE], [collected([raw_event()])], NOW)
        with patch.dict(os.environ, {"TICKETMASTER_API_KEY": ""}), patch("scripts.ticketmaster.fetch_page") as request:
            result = collect_ticketmaster(SOURCE, NOW)
        request.assert_not_called()
        after = merge_feed(before, [SOURCE], [result], NOW + timedelta(hours=6))
        self.assertEqual(after["events"][0]["verification"], "source_unavailable")
        self.assertEqual(after["events"][0]["last_seen_at"], before["events"][0]["last_seen_at"])

    def test_all_pages_are_collected_with_a_hobart_geo_search(self):
        events = [raw_event(f"test-{n}") for n in range(201)]
        with patch.dict(os.environ, {"TICKETMASTER_API_KEY": FAKE_KEY}), \
                patch("scripts.ticketmaster.fetch_page", side_effect=[page(events[:200], total=201), page(events[200:], 1, 201)]) as request, \
                patch("scripts.ticketmaster.sleep"):
            result = collect_ticketmaster(SOURCE, NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["events"]), 201)
        query = request.call_args_list[0].args[0]
        self.assertEqual(query["countryCode"], "AU")
        self.assertEqual(query["radius"], 30)
        self.assertEqual(query["unit"], "km")
        self.assertEqual(query["geoPoint"], "r22u098")
        self.assertEqual(geohash(42.6, -5.6, 5), "ezs42")

    def test_partial_collection_rate_limit_and_overflow_preserve_previous_data(self):
        first = page([raw_event(f"test-{n}") for n in range(200)], total=201)
        with patch.dict(os.environ, {"TICKETMASTER_API_KEY": FAKE_KEY}), \
                patch("scripts.ticketmaster.fetch_page", side_effect=[first, RuntimeError("not logged " + FAKE_KEY)]), \
                patch("scripts.ticketmaster.sleep"):
            result = collect_ticketmaster(SOURCE, NOW)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["events"], [])
        self.assertNotIn(FAKE_KEY, str(result))
        with patch.dict(os.environ, {"TICKETMASTER_API_KEY": FAKE_KEY}), \
                patch("scripts.ticketmaster.fetch_page", return_value=page([], total=1001)):
            self.assertEqual(collect_ticketmaster(SOURCE, NOW)["status"], "error")

    def test_empty_results_are_valid_but_a_removed_event_is_not_cancelled(self):
        before = merge_feed({}, [SOURCE], [collected([raw_event()])], NOW)
        empty = collected([])
        self.assertEqual(empty["status"], "ok")
        after = merge_feed(before, [SOURCE], [empty], NOW + timedelta(hours=6))
        self.assertEqual(after["events"][0]["status"], "listed")
        self.assertEqual(after["events"][0]["verification"], "not_in_latest_listing")

    def test_transport_errors_never_expose_the_key(self):
        url = "https://app.ticketmaster.com/?apikey=" + FAKE_KEY
        for error in [HTTPError(url, 429, "quota " + FAKE_KEY, {}, None), URLError(url)]:
            with patch("scripts.ticketmaster.urlopen", side_effect=error):
                with self.assertRaises(ValueError) as caught:
                    fetch_page({}, FAKE_KEY)
            self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_reflected_key_and_internal_api_links_are_not_published(self):
        raw = raw_event()
        raw["name"] += " " + FAKE_KEY
        payload = page([raw])
        payload["_links"] = {"self": {"href": "https://example.test?apikey=" + FAKE_KEY}}
        with patch.dict(os.environ, {"TICKETMASTER_API_KEY": FAKE_KEY}), \
                patch("scripts.ticketmaster.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            result = collect_ticketmaster(SOURCE, NOW)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(FAKE_KEY, str(result))
        self.assertNotIn("apikey", str(result))

    def test_exact_duplicate_is_hidden_and_status_conflict_remains_visible(self):
        official = dict(SOURCE, id="club", name="Club")
        tm_result = collected([raw_event()])
        club_item = deepcopy(tm_result["events"][0])
        club_item.update(id="club:1", source_id="club", source_name="Club")
        club_result = {"events": [club_item], "observed": {"club:1"}, "status": "ok", "issues": []}
        feed = merge_feed({}, [SOURCE, official], [tm_result, club_result], NOW)
        tm = next(e for e in feed["events"] if e["source_id"] == "ticketmaster")
        self.assertEqual(tm["duplicate_of"], "club:1")
        cancelled = collected([raw_event(status="canceled")])
        feed = merge_feed({}, [SOURCE, official], [cancelled, club_result], NOW)
        self.assertTrue(all("duplicate_of" not in e for e in feed["events"]))
        self.assertTrue(all(e["verification"] == "needs_review" for e in feed["events"]))

    def test_categories_and_addons(self):
        for segment, genre, family, expected in [("Music", "Rock", False, "Music"),
                ("Arts & Theatre", "Comedy", False, "Comedy"),
                ("Arts & Theatre", "Theatre", False, "Arts & theatre"),
                ("Miscellaneous", "Family", True, "Family"), ("Sports", "Cricket", False, "Sport")]:
            raw = raw_event()
            raw["classifications"] = [{"segment": {"name": segment}, "genre": {"name": genre}, "family": family}]
            self.assertEqual(parse_event(raw, SOURCE)["category"], expected)
        raw["classifications"] = [{"type": {"name": "Parking"}}]
        self.assertIsNone(parse_event(raw, SOURCE))


if __name__ == "__main__":
    unittest.main()
