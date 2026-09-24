"""Read public AFL fixture data and discover newly published seasons."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode

try:
    from .discovery import DiscoveryError, HOBART, fetch, make_event, public_url
except ImportError:
    from discovery import DiscoveryError, HOBART, fetch, make_event, public_url

API = 'https://aflapi.afl.com.au/afl/v2/'
COMPETITIONS = {1: 'AFL', 3: 'AFLW', 7: 'VFL', 11: 'VFLW'}
VENUES = {'Ninja Stadium', 'Bellerive Oval', 'Blundstone Arena', 'North Hobart Oval', 'KGV Oval', 'Kingborough Twin Ovals', 'Twin Ovals'}


def api_pages(path, field, fetcher, **params):
    records = []
    expected = None
    for page in range(20):
        url = API + path + '?' + urlencode(dict(params, pageSize=200, page=page))
        data = json.loads(fetcher(url))
        metadata = data['meta']
        pagination = metadata['pagination']
        values = data[field]
        if metadata['code'] != 200 or not isinstance(values, list) or pagination['page'] != page:
            raise DiscoveryError('Fixture API response changed')
        total, pages = pagination['numEntries'], pagination['numPages']
        if type(total) is not int or type(pages) is not int or not 0 <= pages <= 20 or total < 0:
            raise DiscoveryError('Invalid fixture pagination')
        if expected is not None and expected != total:
            raise DiscoveryError('Fixture pages changed during collection')
        expected = total
        records.extend(values)
        if page + 1 >= pages:
            if len(records) != total:
                raise DiscoveryError('Incomplete fixture pages')
            return records
    raise DiscoveryError('Fixture page limit exceeded')


def parse_match(raw, season, source, now):
    if raw['compSeason']['id'] != season['id']:
        raise DiscoveryError('Fixture season filter was not honoured')
    venue = raw.get('venue') or {}
    if venue.get('state') != 'TAS':
        return None
    if venue.get('location', '').lower() not in {'hobart', 'north hobart', 'bellerive', 'kingston', 'glenorchy'} and venue.get('name') not in VENUES:
        return None
    state = raw['status']
    if state in {'PLACEHOLDER', 'TBC', 'UNCONFIRMED_TEAMS'}:
        return None
    states = {'SCHEDULED': 'listed', 'CONCLUDED': 'listed', 'LIVE': 'listed',
              'IN_PROGRESS': 'listed', 'CANCELLED': 'cancelled', 'CANCELED': 'cancelled',
              'POSTPONED': 'postponed', 'RESCHEDULED': 'rescheduled'}
    if state not in states:
        raise DiscoveryError('Unrecognised fixture status')
    starts = datetime.fromisoformat(raw['utcStartTime'].replace('Z', '+00:00'))
    if starts.tzinfo is None:
        raise DiscoveryError('Fixture timestamp has no timezone')
    starts = starts.astimezone(HOBART)
    today = now.astimezone(HOBART).date()
    if not today - timedelta(days=30) <= starts.date() <= today + timedelta(days=730):
        return None
    home, away = raw['home']['team']['name'], raw['away']['team']['name']
    if not home or not away or not venue.get('name'):
        raise DiscoveryError('Fixture teams or venue missing')
    category = COMPETITIONS[season['competition']['id']]
    if type(raw['id']) is not int:
        raise DiscoveryError('Invalid football match ID')
    prefix = {'AFL': '', 'AFLW': 'aflw/', 'VFL': 'vfl/', 'VFLW': 'vflw/'}[category]
    item = make_event(source, str(raw['id']), home + ' v ' + away, venue['name'], starts.date(), starts.date(),
                      category, 'https://www.afl.com.au/' + prefix + 'matches/' + str(raw['id']))
    item.update(start_at=starts.isoformat(), status=states[state],
                status_evidence='AFL fixture status=' + state, afl_match_id=raw['id'], discovered=True)
    # Preserve the public ticket identity to join listings without fuzzy names.
    ticket = raw.get('metadata', {}).get('ticket_link')
    if ticket:
        try:
            item['related_urls'] = [public_url(ticket)]
        except ValueError:
            pass
    return item


def collect_afl(source, now, fetcher=fetch):
    events, observed, issues = [], set(), []
    try:
        seasons = api_pages('compseasons', 'compSeasons', fetcher)
        if not seasons:
            raise DiscoveryError('No football seasons found')
        cutoff_year = (now.astimezone(HOBART).date() - timedelta(days=30)).year
        seasons = [s for s in seasons if s['competition']['id'] in COMPETITIONS
                   and cutoff_year <= s['season']['year'] <= now.year + 2]
        if not seasons:
            raise DiscoveryError('Current football seasons are not published')
        for season in seasons:
            try:
                matches = api_pages('matches', 'matches', fetcher, compSeasonId=season['id'])
                for raw in matches:
                    key = source['id'] + ':' + str(raw['id'])
                    observed.add(key)
                    try:
                        item = parse_match(raw, season, source, now)
                        if item:
                            events.append(item)
                    except Exception as error:
                        issues.append('Football fixture needs review: ' + type(error).__name__)
            except Exception as error:
                issues.append('Football season could not be checked: ' + type(error).__name__)
        if len({e['id'] for e in events}) != len(events):
            raise DiscoveryError('Duplicate football match IDs')
        return {'events': events, 'observed': observed, 'issues': issues, 'status': 'needs_review' if issues else 'ok'}
    except Exception as error:
        return {'events': [], 'observed': set(), 'issues': ['Football discovery failed: ' + type(error).__name__], 'status': 'error'}
