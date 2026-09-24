"""Discover dated Hobart conferences from public organiser calendars."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from lxml import html

HOBART = ZoneInfo('Australia/Hobart')
MONTHS = {name.lower(): i for i, name in enumerate([
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December'], 1)}
MONTHS.update({name[:3]: value for name, value in list(MONTHS.items())})
MONTH = '(?:' + '|'.join(MONTHS) + ')'
VENUES = {'wrest point conference centre': 'Wrest Point', 'wrest point': 'Wrest Point',
          'hotel grand chancellor hobart': 'Hotel Grand Chancellor',
          'hotel grand chancellor': 'Hotel Grand Chancellor', 'crowne plaza hobart': 'Crowne Plaza',
          'racv hobart hotel': 'RACV Hobart Hotel', 'university of tasmania': 'University of Tasmania'}


class DiscoveryError(ValueError):
    pass


def text(node):
    return ' '.join(' '.join(node.itertext()).split())


def public_url(value):
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443):
        raise DiscoveryError('Expected a public HTTPS link')
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DiscoveryError('Non-public network address')
    if '.' not in url.hostname or url.hostname.endswith(('.local', '.internal', '.localhost')):
        raise DiscoveryError('Non-public host')
    return urlunsplit(('https', url.netloc.lower(), url.path, url.query, ''))


def check_host(url):
    host = urlsplit(public_url(url)).hostname
    for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise DiscoveryError('Non-public network address')


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        check_host(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(url):
    check_host(url)
    request = Request(url, headers={'User-Agent': 'HobartShiftEvents/1.0 (public event calendar checker; four checks daily)',
                                   'Accept': 'text/html,application/json'})
    with build_opener(PublicRedirect()).open(request, timeout=20) as response:
        content = response.read(5_000_001)
        if len(content) > 5_000_000:
            raise DiscoveryError('Source exceeds size limit')
        return content.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')


def document(content, keep_navigation=False):
    root = html.fromstring(content)
    if root.xpath('//input[@type="password"]') or 'Protected Page' in text(root):
        raise DiscoveryError('Calendar is not public')
    remove = '//script|//style|//footer|//aside' + ('' if keep_navigation else '|//nav')
    for node in root.xpath(remove):
        node.drop_tree()
    return root


def date_range(value, year):
    value = ' '.join(value.replace('\xa0', ' ').split())
    # A year printed in the date must agree with the calendar section.
    years = re.findall(r'\b20\d{2}\b', value)
    if years and set(years) != {str(year)}:
        raise DiscoveryError('Calendar year disagrees with event date')
    value = re.sub(r'\s+20\d{2}\b', '', value).strip()
    patterns = [
        (rf'^(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})\s+({MONTH})$', 'same'),
        (rf'^(\d{{1,2}})\s+({MONTH})\s*[-–—]\s*(\d{{1,2}})\s+({MONTH})$', 'cross'),
        (rf'^({MONTH})\s+(\d{{1,2}})\s*[-–—]\s*({MONTH})\s+(\d{{1,2}})$', 'monthfirst'),
        (rf'^(\d{{1,2}})\s+({MONTH})$', 'single'),
    ]
    for pattern, kind in patterns:
        match = re.fullmatch(pattern, value, re.I)
        if not match:
            continue
        values = match.groups()
        if kind == 'same':
            d1, d2, m1 = values; m2 = m1
        elif kind == 'cross':
            d1, m1, d2, m2 = values
        elif kind == 'monthfirst':
            m1, d1, m2, d2 = values
        else:
            d1, m1 = values; d2, m2 = d1, m1
        start = date(year, MONTHS[m1.lower()], int(d1))
        end_year = year + (1 if MONTHS[m2.lower()] < MONTHS[m1.lower()] else 0)
        end = date(end_year, MONTHS[m2.lower()], int(d2))
        if not 0 <= (end - start).days <= 14:
            raise DiscoveryError('Invalid conference date range')
        return start, end
    raise DiscoveryError('Conference dates not recognised')


def make_event(source, key, title, venue, start, end, category='Conference', url=None):
    return {'id': source['id'] + ':' + key, 'source_id': source['id'], 'source_name': source['name'],
            'source_url': url or source['url'], 'title': title, 'venue': venue, 'city': 'Hobart',
            'timezone': 'Australia/Hobart', 'category': category, 'start_date': start.isoformat(),
            'end_date': end.isoformat(), 'start_at': None, 'end_at': None,
            'status': 'listed', 'status_evidence': None, 'expected_attendance': None}


def conference(source, title, venue, when, year, link, now, calendar_url):
    start, end = date_range(when, year)
    if end < now.astimezone(HOBART).date() - timedelta(days=30) or start > now.astimezone(HOBART).date() + timedelta(days=730):
        return None
    # Date and venue are deliberately excluded from the identity so revisions
    # replace the original record. The section year separates annual editions.
    identity = str(year) + ':' + (link or re.sub(r'\W+', '', title.lower()))
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    item = make_event(source, key, title, VENUES.get(venue.lower(), venue) if venue else 'Venue not listed', start, end, url=calendar_url)
    item.update(discovered=True, venue_is_unknown=not bool(venue), organiser_url=link,
                discovery_url=calendar_url)
    return item


def parse_design(content, source, year, now, calendar_url):
    root = document(content)
    heading = root.xpath('//h1')
    if not heading or text(heading[0]) != f'{year} Conferences':
        raise DiscoveryError('Conference calendar heading changed')
    cards = root.xpath('//p[.//strong and .//br]')
    if not cards:
        raise DiscoveryError('Conference cards not found')
    events, issues = [], []
    for card in cards:
        lines = [part.strip() for part in html.tostring(card, method='text', encoding='unicode').splitlines() if part.strip()]
        if not lines or not re.fullmatch(r'Hobart,?\s*(?:TAS|Tasmania)', lines[-1], re.I):
            continue
        try:
            if len(lines) != 4:
                raise DiscoveryError('Hobart conference card structure changed')
            title, when, venue, _ = lines
            links = card.xpath('ancestor::div[contains(concat(" ",normalize-space(@class)," ")," wpb_wrapper ")][.//figure//a[@href]][1]//figure//a/@href')
            link = public_url(urljoin(calendar_url, links[0])) if len(links) == 1 and links[0].startswith('https:') else None
            item = conference(source, title, venue, when, year, link, now, calendar_url)
            if item:
                events.append(item)
        except Exception as error:
            issues.append('Hobart conference needs review: ' + type(error).__name__)
    return events, issues


def parse_leishman(content, source, now):
    root = document(content)
    sections = root.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," events_wrapper ")]')
    if not sections:
        raise DiscoveryError('Conference calendar sections not found')
    events, issues = [], []
    valid_sections = 0
    for section in sections:
        headings = section.xpath('preceding::*[self::h2 or self::h3][1]')
        match = re.fullmatch(r'(20\d{2}) Conferences', text(headings[0])) if headings else None
        if not match:
            continue
        valid_sections += 1
        year = int(match[1])
        for card in section.xpath('.//li[contains(@class,"conference-list")]'):
            locations = card.xpath('.//span[@class="cat-location"]')
            location = text(locations[0]) if locations else ''
            if not re.search(r'\bHobart\b', location, re.I) or re.search(r'Launceston|Devonport|\s&\s', location, re.I):
                continue
            try:
                title = text(card.xpath('.//span[@class="confrnce-title"]')[0])
                when = text(card.xpath('.//span[@class="cat-conf-date"]')[0])
                link = public_url(card.xpath('ancestor::a[@href][1]/@href')[0])
                venue = re.sub(r',?\s*Hobart(?:,?\s*(?:Tasmania|TAS))?$', '', location, flags=re.I).strip()
                item = conference(source, title, venue, when, year, link, now, source['url'])
                if item:
                    events.append(item)
            except Exception as error:
                issues.append('Hobart conference needs review: ' + type(error).__name__)
    if not valid_sections:
        raise DiscoveryError('No dated conference sections found')
    return events, issues


def enrich_conference(item, fetcher):
    """Optional detail lookup. A failed detail page does not erase calendar dates."""
    link = item.get('organiser_url')
    if not link:
        return item, {}
    try:
        content = fetcher(link)
        root = document(content)
        body = text(root)
        year = item['start_date'][:4]
        words = [word for word in re.findall(r'[A-Za-z]{4,}', item['title'])
                 if word.lower() not in {'conference', 'congress', 'annual', 'australian', 'australia', 'international', 'the', 'and'}
                 and re.search(r'\b' + re.escape(word) + r'\b', body, re.I)]
        if not words or year not in body or not re.search(r'\bHobart\b', body, re.I):
            return item, {}
        identity = max(words, key=len)
        item['attendance_source'] = {'id': 'attendance-auto-' + item['id'], 'name': item['title'] + ' organiser',
            'url': link, 'format': 'html', 'identity': identity, 'year': int(year), 'priority': 100,
            'match': {k: item[k] for k in ('id', 'start_date', 'end_date', 'venue')}}
        # Only explicit whole-event cancellation wording qualifies.
        status = re.search(r'\bthis (?:conference|congress|meeting|event) (?:has been|is) (cancelled|canceled|postponed)\b', body, re.I)
        if status:
            item['status'] = 'postponed' if status[1].lower() == 'postponed' else 'cancelled'
            item['status_evidence'] = status[0]
        return item, {link: content}
    except Exception:
        return item, {}


def collect_conferences(source, now, fetcher=fetch):
    events, issues, pages = [], [], {}
    try:
        content = fetcher(source['url'])
        if source['adapter'] == 'conference_design':
            root = document(content, keep_navigation=True)
            urls = sorted({urljoin(source['url'], link) for link in root.xpath('//a/@href')
                           if re.fullmatch(r'/20\d{2}-conferences/', urlsplit(urljoin(source['url'], link)).path)
                           and urlsplit(urljoin(source['url'], link)).hostname == urlsplit(source['url']).hostname
                           and (now.astimezone(HOBART).date() - timedelta(days=30)).year <= int(re.search(r'20\d{2}', urlsplit(link).path)[0]) <= now.year + 2})
            if not urls:
                raise DiscoveryError('No current public conference calendar links found')
            for url in urls:
                try:
                    year = int(re.search(r'20\d{2}', urlsplit(url).path)[0])
                    found, warnings = parse_design(fetcher(url), source, year, now, url)
                    events.extend(found); issues.extend(warnings)
                except Exception as error:
                    issues.append('Conference calendar could not be checked: ' + type(error).__name__)
        else:
            events, issues = parse_leishman(content, source, now)
        if len(events) > 40:
            raise DiscoveryError('Conference detail limit exceeded; review calendar size')
        with ThreadPoolExecutor(max_workers=4) as pool:
            checked = list(pool.map(lambda item: enrich_conference(item, fetcher), events))
        events = []
        for item, cached in checked:
            events.append(item); pages.update(cached)
        ids = [item['id'] for item in events]
        if len(ids) != len(set(ids)):
            raise DiscoveryError('Duplicate conference identity in calendar')
        return {'events': events, 'observed': set(ids), 'issues': issues,
                'status': 'needs_review' if issues else 'ok', 'attendance_pages': pages}
    except Exception as error:
        return {'events': [], 'observed': set(), 'issues': ['Conference discovery failed: ' + type(error).__name__], 'status': 'error'}
