import copy
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("flight_archive_updater", Path(__file__).resolve().parents[1] / "scripts" / "update_flights.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def row(number="JQ701", scheduled="2026-09-24T07:30:00+10:00", status=""):
    return {"flight_number": number, "airline": "Test", "from": "Melbourne", "to": "Melbourne",
            "scheduled_time": scheduled, "estimated_time": scheduled, "status": status}


def feed(now, arrivals=None, departures=None):
    return {"last_refresh": now.isoformat(), "arrivals": arrivals or [], "departures": departures or []}


class FlightArchiveTests(unittest.TestCase):
    def setUp(self):
        self.morning = datetime.fromisoformat("2026-09-24T08:00:00+10:00")
        self.afternoon = self.morning + timedelta(hours=6)

    def test_first_flight_survives_removal_and_retains_observation(self):
        first = module.merge_archive({}, feed(self.morning, [row(status="Landed")]), self.morning)
        later = module.merge_archive(first, feed(self.afternoon, [row("VA1320", "2026-09-24T14:00:00+10:00")]), self.afternoon)
        self.assertEqual([f["flight_number"] for f in later["arrivals"]], ["JQ701", "VA1320"])
        self.assertEqual(later["arrivals"][0]["last_seen_at"], first["arrivals"][0]["last_seen_at"])
        self.assertEqual(later["tracking_started_at"], first["tracking_started_at"])

    def test_scheduled_time_correction_and_repeat_do_not_duplicate(self):
        first = module.merge_archive({}, feed(self.morning, [row()]), self.morning)
        changed = row(scheduled="2026-09-24T08:30:00+10:00", status="Delayed")
        later = module.merge_archive(first, feed(self.afternoon, [changed, changed]), self.afternoon)
        self.assertEqual(len(later["arrivals"]), 1)
        self.assertEqual(later["arrivals"][0]["status"], "Delayed")

    def test_missing_flight_is_not_assumed_landed_or_departed(self):
        first = module.merge_archive({}, feed(self.morning, [row()], [row()]), self.morning)
        later = module.merge_archive(first, feed(self.afternoon), self.afternoon)
        self.assertEqual(later["arrivals"][0]["status"], "")
        self.assertEqual(later["departures"][0]["status"], "")

    def test_invalid_or_older_feed_cannot_overwrite_history(self):
        first = module.merge_archive({}, feed(self.morning, [row()]), self.morning)
        original = copy.deepcopy(first)
        for bad in ({"last_refresh": self.afternoon.isoformat(), "arrivals": []},
                    feed(self.afternoon, [row(scheduled="2026-09-24T07:30:00")]),
                    feed(self.morning), feed(self.afternoon + timedelta(hours=1))):
            with self.assertRaises(ValueError):
                module.merge_archive(first, bad, self.afternoon)
        self.assertEqual(first, original)
        with self.assertRaises(ValueError):
            module.merge_archive(first, feed(self.morning - timedelta(minutes=1)), self.morning)

    def test_hobart_midnight_prunes_old_days_but_keeps_today_and_future(self):
        now = datetime.fromisoformat("2026-10-05T00:10:00+11:00")
        rows = [row("OLD", "2026-10-03T23:50:00+10:00"),
                row("YESTERDAY", "2026-10-04T23:50:00+11:00"),
                row("TODAY", "2026-10-05T07:00:00+11:00"),
                row("NEXT", "2026-10-06T07:00:00+11:00")]
        result = module.merge_archive({}, feed(now, rows), now.astimezone(timezone.utc))
        self.assertEqual([f["flight_number"] for f in result["arrivals"]], ["YESTERDAY", "TODAY", "NEXT"])

    def test_new_status_and_estimate_replace_earlier_snapshot(self):
        first = module.merge_archive({}, feed(self.morning, [row()]), self.morning)
        updated = row(status="Landed")
        updated["estimated_time"] = "2026-09-24T07:48:00+10:00"
        result = module.merge_archive(first, feed(self.afternoon, [updated]), self.afternoon)
        self.assertEqual(result["arrivals"][0]["estimated_time"], updated["estimated_time"])
        self.assertEqual(result["arrivals"][0]["status"], "Landed")


if __name__ == "__main__":
    unittest.main()
