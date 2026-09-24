from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts.attendance import attach_attendance
from scripts.auto_attendance import AttendanceSourceError, collect, parse_figures, refresh_attendance, validate_sources

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, 3, tzinfo=timezone.utc)
SOURCE = {'id': 'attendance-test', 'name': 'Test organiser', 'url': 'https://example.org/2026',
          'format': 'html', 'identity': 'Hobart Congress', 'year': 2026,
          'match': {'id': 'congress:2026', 'start_date': '2026-10-15', 'end_date': '2026-10-17', 'venue': 'Wrest Point'}}

def page(text):
    return '<html><h1>Hobart Congress 2026</h1><p>' + text + '</p></html>'

def parse(text):
    return parse_figures(page(text), SOURCE)

def feed():
    return {'events': [dict(SOURCE['match'], attendance_details=[], expected_attendance=None)]}

def run(previous, text, now=NOW):
    current = feed()
    refresh_attendance(current, previous, [SOURCE], now, fetcher=lambda _: page(text))
    return current

class AutomaticAttendanceTests(unittest.TestCase):
    def test_forecast_qualifiers_ranges_and_plus_are_preserved(self):
        for text, low, high, qualifier in [
            ('We expect over 500 delegates.', 500, None, 'more than'),
            ('Expected attendance: 600+', 600, None, 'at least'),
            ('We expect 1,200–1,500 people.', 1200, 1500, ''),
            ('About 800 attendees are expected.', 800, None, 'about')]:
            with self.subTest(text=text):
                result = parse(text)[0]
                self.assertEqual((result['value'], result['upper_value'], result['qualifier']), (low, high, qualifier))
                self.assertEqual(result['kind'], 'organiser_estimate')

    def test_confirmation_is_not_confused_with_forecasts_or_registrations(self):
        for text, kind in [('Confirmed attendance: 650.', 'confirmed_attendance'),
                           ('650 delegates attended.', 'confirmed_attendance'),
                           ('650 delegates have registered.', 'confirmed_registrations'),
                           ('Confirmed registrations: 650.', 'confirmed_registrations')]:
            self.assertEqual(parse(text)[0]['kind'], kind)
        self.assertEqual(parse('We expect 650 registered delegates.')[0]['kind'], 'organiser_estimate')
        self.assertFalse(any(d['kind'].startswith('confirmed') for d in parse('650 delegates are expected.')))

    def test_unrelated_figures_are_not_current_attendance(self):
        for text in ['Nearly 400 attendees in both 2024 and 2025.',
                     'Last year 650 delegates attended.', 'We expect 120 delegates at the breakfast.',
                     'Our membership is 2,500 people.', 'Capacity: 4,300 attendees.',
                     'We hope 650 delegates have registered.', 'Confirmed attendance: 650?',
                     'We expect 650 delegates per day.', 'Not 650 attendees expected.',
                     '2027 conference: We expect 900 delegates.', 'We expect 200 sponsors.',
                     'We expect 100 abstracts.', 'If 500 delegates attended, the room would be full.']:
            with self.subTest(text=text):
                self.assertEqual(parse(text), [])

    def test_other_sentences_do_not_hide_a_whole_event_forecast(self):
        self.assertEqual(parse('We expect over 500 delegates. A workshop follows.')[0]['value'], 500)

    def test_bad_identity_and_conflicting_figures_require_review(self):
        with self.assertRaises(AttendanceSourceError):
            parse_figures('<h1>Other meeting 2027</h1><p>We expect 900 delegates.</p>', SOURCE)
        with self.assertRaises(AttendanceSourceError):
            parse('We expect 500 delegates. We expect 700 delegates.')
        self.assertEqual(collect(SOURCE, lambda _: '<h1>Access denied</h1>')['status'], 'error')

    def test_revised_forecast_and_confirmation_replace_older_values(self):
        first = run({}, 'We expect 500 delegates.')
        revised = run(first, 'We expect 650 delegates.', NOW + timedelta(hours=6))
        details = revised['events'][0]['attendance_details']
        self.assertEqual([d['value'] for d in details], [650])
        self.assertEqual(details[0]['checked_at'], (NOW + timedelta(hours=6)).isoformat(timespec='seconds'))
        confirmed = run(revised, 'Confirmed attendance: 620.', NOW + timedelta(hours=12))
        details = confirmed['events'][0]['attendance_details']
        self.assertEqual([d['value'] for d in details if d['verification'] == 'checked'], [620])
        self.assertEqual(details[0]['kind'], 'confirmed_attendance')
        self.assertEqual(len(details), 1)
        self.assertEqual(confirmed['attendance_sources'][0]['status'], 'ok')

    def test_failure_and_removed_statement_keep_original_check_date(self):
        first = run({}, 'Confirmed registrations: 620.')
        before = first['events'][0]['attendance_details'][0]
        for result, verification in [({'status': 'error', 'issues': ['Timeout'], 'figures': []}, 'source_unavailable'),
                                     ({'status': 'ok', 'issues': [], 'figures': []}, 'not_in_latest_listing')]:
            current = feed()
            refresh_attendance(current, first, [SOURCE], NOW + timedelta(hours=6), results=[result])
            after = current['events'][0]['attendance_details'][0]
            self.assertEqual(after['checked_at'], before['checked_at'])
            self.assertEqual(after['review_after'], before['review_after'])
            self.assertEqual(after['verification'], verification)
            self.assertNotEqual(current['attendance_sources'][0]['status'], 'ok')

    def test_moved_event_does_not_reuse_old_counts_or_fetch_old_page(self):
        previous = run({}, 'Confirmed attendance: 620.')
        current = feed()
        current['events'][0]['start_date'] = '2026-10-16'
        fetcher = Mock()
        refresh_attendance(current, previous, [SOURCE], NOW, fetcher=fetcher)
        fetcher.assert_not_called()
        self.assertEqual(current['events'][0]['attendance_details'], [])

    def test_unverified_event_does_not_refresh_attendance(self):
        previous = run({}, 'Confirmed attendance: 620.')
        current = feed()
        current['events'][0]['verification'] = 'needs_review'
        fetcher = Mock()
        refresh_attendance(current, previous, [SOURCE], NOW + timedelta(hours=6), fetcher=fetcher)
        fetcher.assert_not_called()
        detail = current['events'][0]['attendance_details'][0]
        self.assertEqual(detail['verification'], 'source_unavailable')
        self.assertEqual(detail['checked_at'], NOW.isoformat(timespec='seconds'))

    def test_absent_attendance_is_successful_and_not_zero(self):
        result = run({}, 'Registration is open.')
        self.assertEqual(result['events'][0]['attendance_details'], [])
        self.assertTrue(result['events'][0]['attendance_checked'])
        self.assertEqual(result['attendance_sources'][0]['status'], 'ok')

    def test_sources_require_specific_event_dates_venue_and_https(self):
        validate_sources(json.loads((ROOT / 'config/attendance_sources.json').read_text()))
        for changes in [{'match': {'venue': 'Wrest Point'}}, {'url': 'http://example.com'}, {'year': 2027}]:
            source = deepcopy(SOURCE)
            source.update(changes)
            with self.assertRaises(ValueError):
                validate_sources([source])

    def test_reviewed_forecast_is_only_used_as_a_bootstrap_once(self):
        source = dict(SOURCE, seed_reference='reviewed')
        initial = feed()
        initial['events'][0]['attendance_details'] = [{
            'id': 'reviewed', 'kind': 'organiser_estimate', 'text': '600 attendees',
            'checked_at': '2026-09-23T00:00:00Z', 'review_after': '2026-10-23T00:00:00Z'}]
        failed = {'status': 'error', 'issues': ['Timeout'], 'figures': []}
        refresh_attendance(initial, {}, [source], NOW, results=[failed])
        self.assertEqual(initial['events'][0]['attendance_details'][0]['verification'], 'source_unavailable')
        previous = feed()
        previous['events'][0]['attendance_checked'] = True
        current = feed()
        current['events'][0]['attendance_details'] = deepcopy(initial['events'][0]['attendance_details'])
        refresh_attendance(current, previous, [source], NOW, results=[failed])
        self.assertEqual(current['events'][0]['attendance_details'], [])

    def test_capacity_remains_separate(self):
        references = json.loads((ROOT / 'config/attendance_references.json').read_text())
        current = feed()
        current['events'].append({'id': 'nbl:test', 'venue': 'MyState Bank Arena', 'category': 'NBL'})
        attach_attendance(current, references)
        refresh_attendance(current, {}, [SOURCE], NOW, fetcher=lambda _: page('We expect 650 delegates.'))
        self.assertEqual(current['events'][1]['attendance_details'][0]['kind'], 'venue_capacity')
        self.assertNotIn('attendance_checked', current['events'][1])

if __name__ == '__main__':
    unittest.main()
