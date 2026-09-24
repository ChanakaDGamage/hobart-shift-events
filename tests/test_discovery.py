from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs,urlsplit

from scripts.discovery import (DiscoveryError, check_host, collect_conferences, date_range,
    enrich_conference, parse_design, parse_leishman, public_url)
from scripts.afl_discovery import api_pages, collect_afl, parse_match
from scripts.update_events import merge_feed, mark_duplicate_listings

NOW = datetime(2026,9,24,tzinfo=timezone.utc)
CD = {'id':'cd','name':'Conference Design','url':'https://conferencedesign.com.au/',
      'adapter':'conference_design','coverage':'Public calendars'}
LA = dict(CD,id='la',name='Leishman',adapter='leishman',url='https://leishman-associates.com.au/future/')
AFL = dict(CD,id='afl-official',name='AFL',adapter='afl_official',url='https://www.afl.com.au/fixture')
SEASON = {'id':96,'competition':{'id':3},'season':{'year':2026}}


def design(year=2026, when='26 – 29 October', city='Hobart, TAS', title='Example Congress'):
    return f'<h1>{year} Conferences</h1><div class="wpb_wrapper"><figure><a href="https://example.org/congress/{year}">Congress</a></figure><p><strong>{year} {title}<br>\n</strong>{when}<br>\nWrest Point<br>\n{city}<br>\n</p></div>'


def leishman(year=2027, city='Hobart', when='Jan 29 - Feb 2'):
    return f'<h2>{year} Conferences</h2><ul class="events_wrapper"><a href="https://example.org/{year}"><li class="conference-list"><span class="confrnce-title">Example Forum</span><span class="cat-conf-date">{when}</span><span class="cat-location">{city}</span></li></a></ul>'


def match(**overrides):
    result = {'id':8979,'compSeason':{'id':96}, 'status':'SCHEDULED',
        'home':{'team':{'name':'North Melbourne'}},'away':{'team':{'name':'Adelaide Crows'}},
        'venue':{'name':'North Hobart Oval','location':'Hobart','state':'TAS'},
        'utcStartTime':'2026-10-25T02:05:00.000+0000','metadata':{}}
    result.update(overrides)
    return result


def payload(field,values,page=0,total=None,pages=1):
    return json.dumps({'meta':{'code':200,'pagination':{'page':page,'numPages':pages,'numEntries':len(values) if total is None else total}},field:values})


class ConferenceDiscoveryTests(unittest.TestCase):
    def test_new_published_year_is_followed_without_config_change(self):
        def fetcher(url):
            if url==CD['url']:
                return '<nav><a href="/2026-conferences/">2026</a><a href="/2027-conferences/">2027</a></nav>'
            if url.endswith('/2026-conferences/'):
                return design()
            if url.endswith('/2027-conferences/'):
                return design(2027,'4 – 6 February')
            return '<h1>Not available</h1>'
        result=collect_conferences(CD,NOW,fetcher)
        self.assertEqual(result['status'],'ok')
        self.assertEqual([e['start_date'] for e in result['events']],['2026-10-26','2027-02-04'])

    def test_dates_change_without_creating_another_event(self):
        first=parse_design(design(),CD,2026,NOW,CD['url'])[0][0]
        revised=parse_design(design(when='27 – 30 October'),CD,2026,NOW,CD['url'])[0][0]
        self.assertEqual(first['id'],revised['id'])
        self.assertNotEqual(first['start_date'],revised['start_date'])

    def test_new_organiser_year_section_has_explicit_unknown_venue(self):
        events,issues=parse_leishman(leishman(),LA,NOW)
        self.assertFalse(issues)
        self.assertEqual(events[0]['start_date'],'2027-01-29')
        self.assertEqual(events[0]['end_date'],'2027-02-02')
        self.assertTrue(events[0]['venue_is_unknown'])
        self.assertEqual(events[0]['venue'],'Venue not listed')

    def test_outside_hobart_old_and_multi_city_events_are_excluded(self):
        for city in ['Launceston, TAS','Melbourne, VIC','Hobart & Launceston']:
            self.assertEqual(parse_design(design(city=city),CD,2026,NOW,CD['url'])[0],[])
            self.assertEqual(parse_leishman(leishman(city=city),LA,NOW)[0],[])
        self.assertEqual(parse_leishman(leishman(year=2025),LA,NOW)[0],[])

    def test_cross_month_dates_and_year_mismatch(self):
        self.assertEqual(str(date_range('30 June – 2 July',2027)[1]),'2027-07-02')
        self.assertEqual(str(date_range('8 – 11 November 2027',2027)[0]),'2027-11-08')
        with self.assertRaises(DiscoveryError):date_range('8 – 11 November 2028',2027)
        with self.assertRaises(DiscoveryError):date_range('1 – 30 November',2027)

    def test_protected_calendar_is_not_read_as_empty_success(self):
        result=collect_conferences(CD,NOW,lambda _: '<input type="password">Protected Page')
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['events'],[])

    def test_new_conference_gets_an_attendance_source_automatically(self):
        item=parse_design(design(),CD,2026,NOW,CD['url'])[0][0]
        enriched,pages=enrich_conference(item,lambda _: '<h1>Example Congress 2026 in Hobart</h1><p>We expect 700 delegates.</p>')
        source=enriched['attendance_source']
        self.assertEqual(source['match']['id'],item['id'])
        self.assertEqual(source['year'],2026)
        self.assertIn(source['url'],pages)
        from scripts.auto_attendance import parse_figures
        self.assertEqual(parse_figures(pages[source['url']],source)[0]['value'],700)

    def test_failed_optional_detail_does_not_erase_a_calendar_listing(self):
        item=parse_design(design(),CD,2026,NOW,CD['url'])[0][0]
        enriched,pages=enrich_conference(item,Mock(side_effect=TimeoutError))
        self.assertEqual(enriched['start_date'],'2026-10-26')
        self.assertNotIn('attendance_source',enriched)
        self.assertEqual(pages,{})

    def test_removed_or_failed_source_keeps_previous_event_without_cancelling(self):
        event=parse_design(design(),CD,2026,NOW,CD['url'])[0][0]
        first=merge_feed({},[CD],[{'status':'ok','events':[event],'observed':{event['id']},'issues':[]}],NOW)
        second=merge_feed(first,[CD],[{'status':'error','events':[],'observed':set(),'issues':['Timeout']}],NOW+timedelta(hours=6))
        self.assertEqual(second['events'][0]['status'],'listed')
        self.assertEqual(second['events'][0]['last_seen_at'],first['events'][0]['last_seen_at'])
        self.assertEqual(second['events'][0]['verification'],'source_unavailable')

    def test_network_guard_blocks_private_hosts_and_redirect_targets(self):
        for url in ['http://example.org','https://user:password@example.org','https://localhost/test','https://example.org:8080']:
            with self.assertRaises(ValueError):public_url(url)
        with patch('scripts.discovery.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(DiscoveryError):check_host('https://example.org/')
        with patch('scripts.discovery.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]):
            check_host('https://example.org/')


class FootballDiscoveryTests(unittest.TestCase):
    def test_hobart_daylight_saving_and_correct_match_link(self):
        event=parse_match(match(),SEASON,AFL,NOW)
        self.assertEqual(event['start_at'],'2026-10-25T13:05:00+11:00')
        self.assertEqual(event['source_url'],'https://www.afl.com.au/aflw/matches/8979')
        self.assertIsNone(event['end_at'])

    def test_away_and_launceston_matches_and_placeholders_are_excluded(self):
        for venue in [{'name':'UTAS Stadium','location':'Launceston','state':'TAS'},
                      {'name':'Marvel Stadium','location':'Melbourne','state':'VIC'}]:
            self.assertIsNone(parse_match(match(venue=venue),SEASON,AFL,NOW))
        for status in ['TBC','PLACEHOLDER','UNCONFIRMED_TEAMS']:
            self.assertIsNone(parse_match(match(status=status),SEASON,AFL,NOW))

    def test_explicit_status_and_bad_timestamps(self):
        self.assertEqual(parse_match(match(status='CANCELLED'),SEASON,AFL,NOW)['status'],'cancelled')
        self.assertEqual(parse_match(match(status='POSTPONED'),SEASON,AFL,NOW)['status'],'postponed')
        with self.assertRaises(DiscoveryError):parse_match(match(utcStartTime='2026-10-25T02:05:00'),SEASON,AFL,NOW)
        with self.assertRaises(DiscoveryError):parse_match(match(status='UNKNOWN'),SEASON,AFL,NOW)

    def test_new_season_is_discovered_from_api(self):
        upcoming={'id':105,'competition':{'id':3},'season':{'year':2027}}
        game=match(id=9501,compSeason={'id':105},utcStartTime='2027-09-01T02:00:00Z')
        seen=[]
        def fetcher(url):
            seen.append(url)
            return payload('compSeasons',[upcoming]) if '/compseasons?' in url else payload('matches',[game])
        result=collect_afl(AFL,NOW,fetcher)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['events'][0]['start_date'],'2027-09-01')
        self.assertTrue(any('compSeasonId=105' in u for u in seen))

    def test_pagination_is_complete_and_partial_results_require_review(self):
        def fetcher(url):
            page=int(parse_qs(urlsplit(url).query)['page'][0])
            return payload('matches',[{'id':page}],page,2,2)
        self.assertEqual(len(api_pages('matches','matches',fetcher)),2)
        with self.assertRaises(DiscoveryError):
            api_pages('matches','matches',lambda _:payload('matches',[],total=1))
        result=collect_afl(AFL,NOW,Mock(side_effect=TimeoutError))
        self.assertEqual(result['status'],'error')

    def test_revision_uses_same_match_id(self):
        before=parse_match(match(),SEASON,AFL,NOW)
        after=parse_match(match(utcStartTime='2026-10-26T02:05:00Z'),SEASON,AFL,NOW)
        self.assertEqual(before['id'],after['id'])
        self.assertNotEqual(before['start_date'],after['start_date'])

    def test_ticket_listing_and_article_are_joined_to_official_match(self):
        official=parse_match(match(metadata={'ticket_link':'https://www.ticketmaster.com.au/event/ABC123'}),SEASON,AFL,NOW)
        article=dict(official,id='article:round11',source_id='article',title='North Melbourne v Adelaide',related_urls=[])
        ticket=dict(official,id='ticketmaster:1',source_id='ticketmaster',title='North Melbourne v Adelaide - 2026 NAB AFLW Season 11',
                    source_url='https://www.ticketmaster.com.au/long-title/event/ABC123',related_urls=[])
        stamp=NOW.isoformat(timespec='seconds')
        events=[dict(e,verification='checked',last_seen_at=stamp) for e in [official,article,ticket]]
        states=[{'id':e['source_id'],'status':'ok','issues':[]} for e in events]
        mark_duplicate_listings(events,states,stamp)
        self.assertNotIn('duplicate_of',events[0])
        self.assertEqual([e['duplicate_of'] for e in events[1:]],[official['id'],official['id']])
        events[2]['status']='cancelled'
        mark_duplicate_listings(events,states,stamp)
        self.assertFalse(any('duplicate_of' in e for e in events))
        self.assertTrue(any(s['status']=='needs_review' for s in states))

    def test_same_organiser_annual_conferences_do_not_merge_across_years(self):
        a=parse_design(design(),CD,2026,NOW,CD['url'])[0][0]
        b=dict(a,id='other:2027',source_id='other',start_date='2027-10-26',end_date='2027-10-29')
        stamp=NOW.isoformat(timespec='seconds')
        events=[dict(e,verification='checked',last_seen_at=stamp) for e in [a,b]]
        mark_duplicate_listings(events,[],stamp)
        self.assertFalse(any('duplicate_of' in e for e in events))

if __name__=='__main__':unittest.main()
