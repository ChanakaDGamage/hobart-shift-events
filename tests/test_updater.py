from datetime import date, datetime, timedelta, timezone
import unittest

from scripts.update_events import (
    SourceFormatError, clock_at, day_range, explicit_status, merge_feed, parse_document,
)

SOURCE = {"id": "jj", "name": "Club schedule", "url": "https://www.jackjumpers.com.au/schedule",
          "adapter": "jackjumpers", "coverage": "Hobart games"}
NOW = datetime(2026, 9, 23, 8, tzinfo=timezone.utc)


def fixture(key="game-1", venue="MyState Bank Arena", when="01 Oct 26", clock="7:30 pm", status=""):
    return f'''<a data-id="{key}" href="/schedule/{key}">
      <p fs-list-field="team">Tasmania JackJumpers</p>
      <p fs-list-field="team">Melbourne United</p>
      <p fs-list-field="date">{when}</p><p fs-list-field="time">{clock}</p>
      <p fs-list-field="venue">{venue}</p><span>{status}</span></a>'''


def result(markup):
    events, observed, issues = parse_document('<html>' + markup + '</html>', SOURCE)
    return {"events": events, "observed": observed, "issues": issues,
            "status": "needs_review" if issues else "ok"}


def initial():
    return merge_feed({}, [SOURCE], [result(fixture())], NOW)


class UpdaterTests(unittest.TestCase):
    def test_hobart_clock_accounts_for_daylight_saving(self):
        self.assertEqual(clock_at(date(2026, 10, 1), '7:30 pm').utcoffset(), timedelta(hours=10))
        self.assertEqual(clock_at(date(2026, 10, 16), '7:30 pm').utcoffset(), timedelta(hours=11))

    def test_away_and_launceston_games_are_excluded(self):
        data = result(fixture() + fixture('away', 'John Cain Arena') + fixture('north', 'The Silverdome'))
        self.assertEqual([e['id'] for e in data['events']], ['jj:game-1'])

    def test_time_to_be_confirmed_is_not_midnight(self):
        self.assertIsNone(result(fixture(clock='TBC'))['events'][0]['start_at'])

    def test_failed_source_keeps_original_dates_and_last_seen(self):
        before = initial()
        failed = {"events": [], "observed": set(), "issues": ["Timeout"], "status": "error"}
        after = merge_feed(before, [SOURCE], [failed], NOW + timedelta(hours=6))
        old, new = before['events'][0], after['events'][0]
        self.assertEqual(old['start_at'], new['start_at'])
        self.assertEqual(old['last_seen_at'], new['last_seen_at'])
        self.assertEqual(new['verification'], 'source_unavailable')
        self.assertEqual(new['status'], 'listed')

    def test_disappearing_event_does_not_become_cancelled(self):
        data = result(fixture('game-2'))
        feed = merge_feed(initial(), [SOURCE], [data], NOW + timedelta(hours=6))
        old = next(e for e in feed['events'] if e['id'] == 'jj:game-1')
        self.assertEqual(old['status'], 'listed')
        self.assertEqual(old['verification'], 'not_in_latest_listing')
        self.assertEqual(feed['sources'][0]['status'], 'needs_review')

    def test_bad_time_keeps_previous_event_and_flags_review(self):
        feed = merge_feed(initial(), [SOURCE], [result(fixture(clock='broken'))], NOW + timedelta(hours=6))
        self.assertEqual(len(feed['events']), 1)
        self.assertEqual(feed['events'][0]['verification'], 'needs_review')
        self.assertTrue(feed['events'][0]['start_at'].startswith('2026-10-01'))

    def test_venue_moved_outside_hobart_flags_old_entry(self):
        feed = merge_feed(initial(), [SOURCE], [result(fixture(venue='The Silverdome'))], NOW + timedelta(hours=6))
        self.assertEqual(feed['events'][0]['verification'], 'needs_review')
        self.assertEqual(feed['sources'][0]['status'], 'needs_review')

    def test_changed_date_updates_existing_identifier(self):
        feed = merge_feed(initial(), [SOURCE], [result(fixture(when='02 Oct 26'))], NOW + timedelta(hours=6))
        self.assertEqual(len(feed['events']), 1)
        self.assertEqual(feed['events'][0]['start_date'], '2026-10-02')
        self.assertEqual(feed['changes'][-1]['kind'], 'changed')

    def test_repeated_checks_do_not_duplicate_events_or_changes(self):
        before = initial()
        after = merge_feed(before, [SOURCE], [result(fixture())], NOW + timedelta(hours=6))
        self.assertEqual(len(after['events']), 1)
        self.assertEqual(len(after['changes']), len(before['changes']))

    def test_explicit_cancelled_row_is_recognised(self):
        self.assertEqual(result(fixture(status='Cancelled'))['events'][0]['status'], 'cancelled')

    def test_policy_text_does_not_mark_conference_cancelled(self):
        self.assertEqual(explicit_status('Cancellation deadline 16 October. Cancelled registrations are refunded.')[0], 'listed')
        self.assertEqual(explicit_status('If this event is cancelled, refunds will be issued.')[0], 'listed')
        self.assertEqual(explicit_status('This conference has been cancelled.')[0], 'cancelled')

    def test_missing_cancellation_label_requires_review_before_reinstating(self):
        before = merge_feed({}, [SOURCE], [result(fixture(status='Cancelled'))], NOW)
        after = merge_feed(before, [SOURCE], [result(fixture())], NOW + timedelta(hours=6))
        self.assertEqual(after['events'][0]['status'], 'cancelled')
        self.assertEqual(after['events'][0]['verification'], 'needs_review')

    def test_conference_year_rollover_is_rejected(self):
        with self.assertRaises(SourceFormatError):
            day_range('13-15 October 2027', 2026)

    def test_cross_month_range_requires_review(self):
        with self.assertRaises(SourceFormatError):
            day_range('30 October-2 November 2026', 2026)

    def test_broken_page_is_rejected_instead_of_returning_empty_schedule(self):
        with self.assertRaises(SourceFormatError):
            parse_document('<html>Temporarily unavailable</html>', SOURCE)

    def test_duplicate_source_ids_are_rejected(self):
        with self.assertRaises(SourceFormatError):
            result(fixture() + fixture())


if __name__ == '__main__':
    unittest.main()
