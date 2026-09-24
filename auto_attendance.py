"""Recheck configured public organiser pages; never infer crowds from capacity.

Only explicit, current-event statements are accepted. Registration counts and
actual attendance remain separate. Unknown wording is left unclassified.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta, timezone
from io import BytesIO
import re
from urllib.parse import urldefrag, urlsplit
from urllib.request import Request, urlopen

from lxml import html
from pypdf import PdfReader

try:
    from .attendance import instant
except ImportError:
    from attendance import instant

KIND_RANK = {'confirmed_attendance': 0, 'confirmed_registrations': 1, 'organiser_estimate': 2, 'estimated_attendance': 3}
CURRENT_KINDS = set(KIND_RANK)
QUALIFIER = r"(?P<qualifier>more than|over|at least|approximately|around|about|nearly|up to)?\s*"
NUMBER = r"(?P<low>\d{1,3}(?:,\d{3})+|\d{1,7})(?:\s*[–−-]\s*(?P<high>\d{1,3}(?:,\d{3})+|\d{1,7}))?(?P<plus>\+)?(?![\w,+])"
COUNT = QUALIFIER + NUMBER
PEOPLE = r"(?:attendees|delegates|people|participants|spectators)"
SEPARATOR = r"\s*(?::|is|of|was|has reached)?\s*"
PATTERNS = [
    ("organiser_estimate", r"\b(?:we (?:now )?expect(?: to welcome)?|we anticipate)\s+" + COUNT + r"\s+(?:registered\s+)?" + PEOPLE + r"\b"),
    ("organiser_estimate", r"\b(?:expected attendance|attendance forecast|forecast attendance|projected attendance)" + SEPARATOR + COUNT + r"(?:\s+" + PEOPLE + r")?(?=\s|[.;:]|$)"),
    ("organiser_estimate", r"\b" + COUNT + r"\s+" + PEOPLE + r"\s+(?:are |were )?expected\b"),
    ("confirmed_registrations", r"\b(?:confirmed registrations|confirmed delegates|registered delegates|total registrations|registrations received)" + SEPARATOR + COUNT + r"(?=\s|[.;:]|$)"),
    ("confirmed_registrations", r"\b" + COUNT + r"\s+(?:registered|confirmed)\s+" + PEOPLE + r"(?=\s|[.;:]|$)"),
    ("confirmed_registrations", r"\b" + COUNT + r"\s+" + PEOPLE + r"\s+(?:have |has |are |now )*(?:registered|confirmed their attendance)\b"),
    ("confirmed_attendance", r"\b(?:confirmed attendance|final attendance|actual attendance|recorded attendance)" + SEPARATOR + COUNT + r"(?=\s|[.;:]|$)"),
    ("confirmed_attendance", r"\b" + COUNT + r"\s+" + PEOPLE + r"\s+attended\b"),
]
# These contexts cannot establish a current whole-event crowd.
EXCLUDED = re.compile(r"\b(?:last year|previous|past conferences?|capacity|members(?:hip)?|ticket allocation|complimentary|workshop|breakfast|gala|dinner|webinar|online|virtual|tour|per day|each day|daily|not|unconfirmed|target|aim|hope|potential|if|would|should|could|may|might)\b", re.I)
FORECAST = re.compile(r"\b(?:expect(?:ed)?|forecast|projected|anticipat(?:e|ed))\b", re.I)


class AttendanceSourceError(ValueError):
    pass


def matches(event, binding):
    return all(event.get(key) == value for key, value in binding.items())


def validate_sources(sources):
    seen = set()
    for source in sources:
        if source['id'] in seen:
            raise ValueError('Duplicate attendance source')
        seen.add(source['id'])
        url = urlsplit(source['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Attendance sources require public HTTPS links')
        if source['format'] not in {'html', 'pdf'}:
            raise ValueError('Unsupported attendance source format')
        binding = source['match']
        if set(binding) != {'id', 'start_date', 'end_date', 'venue'} or not all(isinstance(v, str) and v for v in binding.values()):
            raise ValueError('Attendance sources must identify the exact event, dates and venue')
        if str(source['year']) != binding['start_date'][:4] or not source['identity'].strip():
            raise ValueError('Attendance source year or identity missing')


def fetch(source):
    req = Request(urldefrag(source['url'])[0], headers={
        'User-Agent': 'HobartShiftEvents/1.0 (public event timetable checker; four checks daily)',
        'Accept': 'application/pdf' if source['format'] == 'pdf' else 'text/html,application/xhtml+xml',
    })
    with urlopen(req, timeout=25) as response:
        content = response.read(15_000_001)
        if len(content) > 15_000_000:
            raise AttendanceSourceError('Attendance source exceeds size limit')
        return content if source['format'] == 'pdf' else content.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')


def document_blocks(content, source):
    if source['format'] == 'pdf':
        reader = PdfReader(BytesIO(content))
        if len(reader.pages) > 80:
            raise AttendanceSourceError('Unexpected prospectus length')
        pages = source.get('pages', [])
        if not pages or any(page < 1 or page > len(reader.pages) for page in pages):
            raise AttendanceSourceError('Attendance prospectus pages missing')
        return [' '.join((reader.pages[page - 1].extract_text() or '').split()) for page in pages]
    doc = html.fromstring(content)
    for node in doc.xpath('//script|//style|//nav|//footer|//aside'):
        node.drop_tree()
    nodes = doc.xpath('//p|//li|//h1|//h2|//h3|//h4|//td|//div[not(.//div or .//p)]')
    return list(dict.fromkeys(' '.join(node.text_content().split()) for node in nodes if node.text_content().strip()))


def figure(match, kind):
    low = int(match['low'].replace(',', ''))
    high = int(match['high'].replace(',', '')) if match['high'] else None
    qualifier = (match['qualifier'] or '').lower()
    if match['plus']:
        qualifier = 'at least'
    qualifier = {'over': 'more than', 'approximately': 'about', 'around': 'about'}.get(qualifier, qualifier)
    if low <= 0 or low > 1_000_000 or (high is not None and not low <= high <= 1_000_000):
        raise AttendanceSourceError('Invalid attendee count')
    text = f'{low:,}' + (f'–{high:,}' if high is not None else '')
    if qualifier:
        text = qualifier.capitalize() + ' ' + text
    return {'kind': kind, 'value': low, 'upper_value': high, 'qualifier': qualifier,
            'text': text + (' registered' if kind == 'confirmed_registrations' else ' attendees')}


def parse_figures(content, source):
    blocks = document_blocks(content, source)
    all_text = ' '.join(blocks)
    if not re.search(r'\b' + str(source['year']) + r'\b', all_text) or source['identity'].lower() not in all_text.lower():
        raise AttendanceSourceError('Current event identity not found on attendance page')
    figures = {}
    for block in blocks:
        years = set(re.findall(r'\b20\d{2}\b', block))
        # Reject historical comparisons even when the page itself is for 2026.
        if years - {str(source['year'])}:
            continue
        for sentence in re.split(r'(?<=[.!;])\s+', block):
            if EXCLUDED.search(sentence) or '?' in sentence:
                continue
            for kind, pattern in PATTERNS:
                if kind.startswith('confirmed') and FORECAST.search(sentence):
                    continue
                for match in re.finditer(pattern, sentence, re.I):
                    item = figure(match, kind)
                    key = (kind, item['value'], item['upper_value'], item['qualifier'])
                    figures[key] = item
    # This known prospectus expresses the whole-congress forecast in marketing
    # prose rather than a labelled attendance field. Scope is the reviewed page.
    if source.get('forecast_phrase') == 'oral_health_professionals':
        for match in re.finditer(r'\bConnect with\s+' + COUNT + r'\s+oral health professionals\b', all_text, re.I):
            item = figure(match, 'organiser_estimate')
            figures[('organiser_estimate', item['value'], item['upper_value'], item['qualifier'])] = item
    for kind in CURRENT_KINDS:
        if sum(item['kind'] == kind for item in figures.values()) > 1:
            raise AttendanceSourceError('Conflicting attendee figures need review')
    return list(figures.values())


def collect(source, fetcher=fetch):
    try:
        return {'figures': parse_figures(fetcher(source), source), 'status': 'ok', 'issues': []}
    except Exception as error:
        # Do not include response content, URLs or credentials in errors.
        return {'figures': [], 'status': 'error', 'issues': ['Attendance check failed: ' + type(error).__name__]}


def refresh_attendance(feed, previous, sources, now, fetcher=fetch, results=None):
    """Run after attach_attendance; preserve old figures only as earlier reports."""
    validate_sources(sources)
    active = [s for s in sources if any(matches(e, s['match']) for e in feed['events'])]
    if results is None:
        def check(source):
            event = next(e for e in feed['events'] if matches(e, source['match']))
            if event.get('verification', 'checked') != 'checked':
                return {'figures': [], 'status': 'error', 'issues': ['Event dates or venue need verification before updating attendance']}
            return collect(source, fetcher)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(check, active))
    if len(results) != len(active):
        raise ValueError('Attendance result count differs from active sources')
    stamp = now.astimezone(timezone.utc).isoformat(timespec='seconds')
    old_events = {e['id']: e for e in previous.get('events', [])}
    old_states = {s['id']: s for s in previous.get('attendance_sources', [])}
    states = []
    for event in feed['events']:
        event_sources = [(s, result) for s, result in zip(active, results, strict=True) if matches(event, s['match'])]
        if not event_sources:
            continue
        baseline = event.get('attendance_details', [])
        details = [d for d in baseline if d['kind'] not in CURRENT_KINDS]
        old = old_events.get(event['id'], {})
        for source, result in event_sources:
            previous_details = old.get('attendance_details', []) if matches(old, source['match']) else []
            saved = [deepcopy(d) for d in previous_details if d.get('attendance_source_id') == source['id']]
            if not saved and not old.get('attendance_checked'):
                saved = [deepcopy(d) for d in baseline if d['kind'] in CURRENT_KINDS and (
                    d['id'] == source.get('seed_reference') or
                    (d.get('update_method') == 'event_source_parser' and d['source_url'] == source['url']))]
            latest = []
            for item in result['figures']:
                latest.append(dict(item, id=source['id'] + ':' + item['kind'],
                    attendance_source_id=source['id'], priority=source.get('priority', 0), note='Published whole-event figure.',
                    source_name=source['name'], source_url=source['url'], checked_at=stamp,
                    review_after=(now + timedelta(hours=12)).isoformat(timespec='seconds'),
                    verification='checked', update_method='automatic_source_check'))
            current_kinds = {d['kind'] for d in latest}
            # A successful forecast revision replaces its older value. A removed
            # statement or failed request never renews a previously checked date.
            best_new_rank = min((KIND_RANK[k] for k in current_kinds), default=99)
            missing = [d for d in saved if d['kind'] not in current_kinds
                       and KIND_RANK.get(d['kind'], 99) < best_new_rank]
            for detail in missing:
                detail.update(attendance_source_id=source['id'],
                    verification='source_unavailable' if result['status'] == 'error' else 'not_in_latest_listing')
            details.extend(latest + missing)
            issues = list(result['issues'])
            if missing and result['status'] == 'ok':
                issues.append('A previously published attendee figure is no longer found')
            status = result['status'] if not issues or result['status'] == 'error' else 'needs_review'
            states.append({'id': source['id'], 'name': source['name'], 'url': source['url'],
                'status': status, 'issues': issues, 'event_count': len(latest),
                'last_attempt_at': stamp,
                'last_success_at': stamp if status == 'ok' else old_states.get(source['id'], {}).get('last_success_at')})
        event['attendance_details'] = details
        event['attendance_checked'] = True
    feed['attendance_sources'] = states
    feed.pop('attendance_note', None)
    return feed
