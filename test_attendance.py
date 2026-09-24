from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.attendance import attach_attendance, validate_references

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = json.loads((ROOT / 'config/attendance_references.json').read_text())


def item(**overrides):
    event = {'id': 'ohaa-2026:conference', 'source_id': 'ohaa-2026',
             'source_name': 'Test organiser', 'source_url': 'https://example.org/event',
             'start_date': '2026-10-15', 'end_date': '2026-10-17', 'venue': 'Wrest Point',
             'category': 'Conference', 'last_seen_at': '2026-09-23T22:00:00+00:00',
             'verification': 'checked', 'expected_attendance': None}
    event.update(overrides)
    return event


def annotated(*events):
    return attach_attendance({'events': list(events)}, REFERENCES)['events']


class AttendanceTests(unittest.TestCase):
    def test_organiser_estimate_is_tied_to_the_correct_conference(self):
        event = annotated(item())[0]
        self.assertEqual(event['attendance_details'][0]['kind'], 'organiser_estimate')
        self.assertIn('600', event['attendance_details'][0]['text'])
        self.assertIsNone(event['expected_attendance'])

    def test_historical_attendance_and_capacity_do_not_become_estimates(self):
        hith, nbl = annotated(
            item(id='hith-2026:conference', start_date='2026-11-17', end_date='2026-11-19', venue='Hotel Grand Chancellor'),
            item(id='game:1', venue='MyState Bank Arena', category='NBL'))
        self.assertEqual(hith['attendance_details'][0]['kind'], 'historical_attendance')
        self.assertEqual(nbl['attendance_details'][0]['kind'], 'venue_capacity')
        self.assertIsNone(hith['expected_attendance'])
        self.assertIsNone(nbl['expected_attendance'])
        concert = annotated(item(id='concert:1', venue='MyState Bank Arena', category='Music'))[0]
        self.assertEqual(concert['attendance_details'], [])

    def test_reference_check_date_does_not_advance_with_feed_refreshes(self):
        before = annotated(item())[0]['attendance_details'][0]
        after = annotated(item(last_seen_at='2026-10-01T22:00:00+00:00'))[0]['attendance_details'][0]
        self.assertEqual(before['checked_at'], after['checked_at'])
        self.assertEqual(before['review_after'], after['review_after'])

    def test_rescheduled_moved_and_unknown_events_do_not_inherit_references(self):
        for changed in [item(start_date='2026-10-16'), item(venue='Other venue'), item(id='ohaa-2027:conference')]:
            changed['attendance_details'] = [{'kind': 'organiser_estimate', 'text': 'Old data'}]
            self.assertEqual(annotated(changed)[0]['attendance_details'], [])
        self.assertEqual(annotated(item(id='unknown'))[0]['attendance_details'], [])

    def test_live_parser_estimate_takes_priority_without_becoming_fresh_on_failure(self):
        event = item(expected_attendance={'basis': 'organiser_expectation', 'qualifier': 'approximately', 'value': 650},
                     verification='source_unavailable')
        detail = annotated(event)[0]['attendance_details']
        self.assertEqual(len(detail), 1)
        self.assertIn('650', detail[0]['text'])
        self.assertEqual(detail[0]['verification'], 'source_unavailable')
        self.assertEqual(detail[0]['checked_at'], event['last_seen_at'])

    def test_bad_links_dates_and_broad_estimate_matches_are_rejected(self):
        for change in [{'source_url': 'http://example.com'}, {'review_after': '2020-01-01T00:00:00Z'},
                       {'match': {'venue': 'Wrest Point'}}]:
            reference = deepcopy(REFERENCES[0])
            reference.update(change)
            with self.assertRaises(ValueError):
                validate_references([reference])


if __name__ == '__main__':
    unittest.main()
