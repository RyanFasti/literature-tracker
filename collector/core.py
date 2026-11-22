"""Crossref retrieval, scientific-text normalization, and explainable rules."""
import html
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_CONTACT = os.environ.get('CROSSREF_MAILTO', '').strip()
USER_AGENT = 'LiteratureTracker/1.3' + (f' (mailto:{_CONTACT})' if _CONTACT else '')
BASE = 'https://api.crossref.org'
CLASSIFIER_VERSION = 'rules_v2'


def get_json(url, attempts=4):
    """HTTP JSON request with bounded retries for transient failures."""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
            retry_after = exc.headers.get('Retry-After') if isinstance(exc, urllib.error.HTTPError) and exc.headers else None
            try:
                wait = min(max(float(retry_after), 0), 60) if retry_after else min(2 ** attempt * 2, 20)
            except (TypeError, ValueError):
                wait = min(2 ** attempt * 2, 20)
            time.sleep(wait)


def normalize_doi(value):
    value = (value or '').strip()
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value, flags=re.I)
    value = urllib.parse.unquote(value).strip().lower()
    if not re.fullmatch(r'10\.\d{4,9}/\S+', value, flags=re.I):
        raise ValueError('Expected a valid-looking DOI, e.g. 10.1021/acsenergylett.XXXXXXX')
    return value


def strip_markup(value):
    value = re.sub(r'<[^>]+>', ' ', value or '')
    return re.sub(r'\s+', ' ', html.unescape(value)).strip()


def parse_crossref_date(message, field):
    """Return (ISO-like date, precision); never invent month/day components."""
    parts = (message.get(field) or {}).get('date-parts', [])
    if not parts or not parts[0]:
        return None, None
    vals = parts[0]
    try:
        year = int(vals[0])
        if not (1 <= year <= 9999):
            return None, None
        if len(vals) == 1:
            return f'{year:04d}', 'year'
        month = int(vals[1])
        if not (1 <= month <= 12):
            return None, None
        if len(vals) == 2:
            return f'{year:04d}-{month:02d}', 'month'
        day = int(vals[2])
        # Validate real dates, including leap years.
        from datetime import date
        date(year, month, day)
        return f'{year:04d}-{month:02d}-{day:02d}', 'day'
    except (ValueError, TypeError, OverflowError):
        return None, None


def date_from_crossref(message):
    """Backwards-compatible accessor, now preserving source precision."""
    for field in ('published-online', 'published', 'published-print', 'issued'):
        value, _ = parse_crossref_date(message, field)
        if value:
            return value
    return None


def normalize_crossref(message):
    doi = normalize_doi(message['DOI'])
    title = strip_markup((message.get('title') or [''])[0])
    from .abstracts import clean_abstract
    abstract = clean_abstract(message.get('abstract', ''))
    published = None
    precision = None
    date_source = None
    for field in ('published-online', 'published', 'published-print', 'issued'):
        published, precision = parse_crossref_date(message, field)
        if published:
            date_source = field
            break
    online, online_precision = parse_crossref_date(message, 'published-online')
    printed, print_precision = parse_crossref_date(message, 'published-print')
    created, _ = parse_crossref_date(message, 'created')
    deposited, _ = parse_crossref_date(message, 'deposited')
    return {
        'doi': doi, 'title': title, 'abstract': abstract or None,
        'abstract_status': 'available' if abstract else 'missing',
        'abstract_source': 'crossref' if abstract else None,
        'journal': strip_markup((message.get('container-title') or [''])[0]),
        'issns': message.get('ISSN', []),
        'published_date': published, 'published_date_precision': precision,
        'publication_date_source': date_source,
        'published_online': online, 'published_online_precision': online_precision,
        'published_print': printed, 'published_print_precision': print_precision,
        'metadata_created': created, 'metadata_deposited': deposited,
        'authors': [strip_markup(' '.join(filter(None, [a.get('given'), a.get('family')])))
                    for a in message.get('author', [])],
        'type': (message.get('subtype') if message.get('subtype') in ('review-article', 'research-article')
                 else message.get('type')),
        'url': message.get('URL') or 'https://doi.org/' + doi,
        'source': 'crossref',
    }


def crossref_doi(doi):
    url = BASE + '/works/' + urllib.parse.quote(doi, safe='')
    return normalize_crossref(get_json(url)['message'])


def _crossref_pages(issn, start, end, page_size, max_pages, mode, endpoint):
    """Paginate a Crossref journal/date query; fail rather than truncate."""
    date_filters = {
        'pub': ('from-pub-date', 'until-pub-date'),
        'online': ('from-online-pub-date', 'until-online-pub-date'),
        'created': ('from-created-date', 'until-created-date'),
        # Includes both initial deposits and later publisher redeposits.
        # Crossref documents update-date as equivalent to deposit-date.
        'updated': ('from-update-date', 'until-update-date'),
    }
    if mode not in date_filters:
        raise ValueError(f'Unknown Crossref date mode: {mode}')
    first, last = date_filters[mode]
    cursor = '*'
    seen_cursors = set()
    for _ in range(max_pages):
        if cursor in seen_cursors:
            raise RuntimeError('Repeated Crossref cursor; refusing partial ingestion')
        seen_cursors.add(cursor)
        filters = f'{first}:{start},{last}:{end}'
        if endpoint == 'works':
            filters = f'issn:{issn},' + filters
            path = '/works'
        elif endpoint == 'journal':
            path = f'/journals/{urllib.parse.quote(issn)}/works'
        else:
            raise ValueError(f'Unknown endpoint: {endpoint}')
        params = urllib.parse.urlencode({'filter': filters, 'rows': page_size, 'cursor': cursor})
        response = get_json(f'{BASE}{path}?{params}')['message']
        items = response.get('items', [])
        for item in items:
            yield normalize_crossref(item)
        if len(items) < page_size:
            return
        next_cursor = response.get('next-cursor')
        if not next_cursor:
            raise RuntimeError('Crossref omitted next cursor before pagination ended')
        cursor = next_cursor
    raise RuntimeError('Pagination safety limit reached; split the date window')


def crossref_journal(issn, start, end, page_size=100, max_pages=1000, mode='pub'):
    """Crossref journal endpoint with ISSN-filter fallback for empty/404 responses."""
    journal_count = 0
    try:
        for paper in _crossref_pages(issn, start, end, page_size, max_pages, mode, 'journal'):
            journal_count += 1
            yield paper
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        yield from _crossref_pages(issn, start, end, page_size, max_pages, mode, 'works')
        return
    if journal_count == 0:
        yield from _crossref_pages(issn, start, end, page_size, max_pages, mode, 'works')


# Chemical specificity is preferred over the unqualified word 'perovskite'.
# These scores are ordinal rule strengths, NOT probabilities or model confidence.
HALIDE_LANGUAGE = re.compile(
    r'\b(?:metal[ -]?halide|lead[ -]?halide|tin[ -]?halide|hybrid[ -]?halide|'
    r'organic[ -]?inorganic[ -]?halide|halide)\s+perovskit(?:es?|oids?)\b|'
    r'\b(?:lead|tin|cesium|caesium|methylammonium|formamidinium)\s+'
    r'(?:iodide|bromide|chloride)\s+perovskites?\b', re.I)
HALIDE_COMPOSITION = re.compile(
    r'(?<![a-z0-9])(?:'
    r'(?:ma|fa|cs|ch3nh3|nh2chnh2|ba2ma|pea2|fpea2)(?:pb|sn|ge)(?:i|br|cl|x)\d{1,2}'
    r'|cs\d?(?:ag|na|k|sn|pb|bi|sb|hf|y)(?:ag|bi|sb|sn|pb|hf|y)?(?:i|br|cl|x)\d{1,2}'
    r'|(?:fapbi3|mapbi3|cspbi3|cspbbr3|cspbcl3|cssni3|csgei3)'
    r')(?![a-z0-9])', re.I)
HALIDE_CHEMISTRY = re.compile(
    r'\b(?:lead|tin|bismuth|antimony|germanium|silver|cesium|caesium|'
    r'iodide|bromide|chloride|halide|mixed[ -]?halide|sn[–-]pb|pb[–-]sn)\b', re.I)
PEROVSKITE = re.compile(r'\bperovskit(?:es?|oids?)\b', re.I)
DEVICE = re.compile(
    r'\b(?:solar cells?|photovoltaics?|tandem|light[ -]?emitting diodes?|leds?|'
    r'quantum dots?|nanocrystals?|photodetectors?|lasing|lasers?|'
    r'photoluminescence|electroluminescence|excitons?|scintillat(?:ors?|ion)|'
    r'x[ -]?ray detection|ultrasound detection|piezoelectric|'
    r'optoelectronic|memristors?|photocatalysis)\b', re.I)
HALIDE_DEVICE = re.compile(
    r'\bperovskite(?:[ -]?(?:organic|silicon|topcon|cigs|pbs))?'
    r'(?:[ -]|/)*(?:solar cells?|photovoltaics?|tandems?|leds?|'
    r'light[ -]?emitting diodes?|quantum dots?|nanocrystals?|'
    r'photodetectors?|lasers?|lasing|scintillators?|memristors?)\b', re.I)
OXIDE_CONTEXT = re.compile(
    r'\b(?:oxide perovskites?|perovskite oxides?|solid[ -]?oxide|sofc|soec|'
    r'protonic ceramic|oxygen vacancies|oxygen[ -]?evolution|'
    r'cathode catalysts?|perovskite catalysts?|fuel electrodes?|metal[ -]?oxide|'
    r'ferrite|electrolysis|lithium[ -]?sulfur|sodium metal anodes?|'
    r'solar distillation|catalytic|catalysts?|fuel cells?)\b|'
    r'\b(?:batio3|srtio3|lafeo3|srfemo|pzt|srmno3|lanio3)\b', re.I)


def _clean_for_classification(value):
    text = unicodedata.normalize('NFKC', html.unescape(value or '')).lower()
    text = re.sub(r'[\u2010-\u2015\u2212]', '-', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def classify(paper):
    title = _clean_for_classification(paper.get('title'))
    abstract = _clean_for_classification(paper.get('abstract'))
    content = title + ' ' + abstract
    # Normalize spacing inside selected chemical formulas without joining prose.
    chemical_spaced = re.sub(
        r'\bcs\s+(\d)\s*(?=(?:ag|na|k|sn|pb|bi|sb|hf|y))',
        r'cs\1', content)
    chemical_spaced = re.sub(
        r'\b((?:cs|fa|ma|ch3nh3|nh2chnh2|ba2ma|pea2|fpea2|cs\d))\s+'
        r'(?=(?:pb|sn|ge|ag|bi|sb|hf|y))', r'\1', chemical_spaced)
    chemical_spaced = re.sub(
        r'\b(pb|sn|ge|ag|bi|sb|hf|y)\s+(?=(?:i|br|cl|x)\s*\d)',
        r'\1', chemical_spaced)
    chemical_spaced = re.sub(r'\b(i|br|cl|x)\s+(?=\d\b)', r'\1', chemical_spaced)
    perovskite = bool(PEROVSKITE.search(content))
    layered_pbi4 = bool(re.search(r'\bpb\s*(?:i|br|cl)\s*[346]\b', content))
    layered_ba = bool(re.search(r'\bba\s*2\s*ma\s*pb\s*2\s*i\s*7\b', content))
    formula = bool(HALIDE_COMPOSITION.search(chemical_spaced) or
                   (perovskite and (layered_pbi4 or layered_ba)))
    explicit_halide = bool(HALIDE_LANGUAGE.search(content))
    chemistry = bool(HALIDE_CHEMISTRY.search(content))
    device = bool(DEVICE.search(content))
    direct_device = bool(HALIDE_DEVICE.search(content))
    title_device = bool(PEROVSKITE.search(title) and DEVICE.search(title))
    # A NiOx or metal-oxide contact in an LED/solar-cell abstract is not evidence
    # that the absorber itself is an oxide perovskite.
    oxide = bool(OXIDE_CONTEXT.search(title)) or (
        bool(OXIDE_CONTEXT.search(abstract)) and not (direct_device or title_device))

    if formula:
        decision, score, reason, matched = 'include', 0.99, 'recognized metal-halide composition', ['chemical_formula']
    elif explicit_halide:
        decision, score, reason, matched = 'include', 0.97, 'explicit halide-perovskite terminology', ['halide_perovskite']
    elif oxide and not chemistry:
        decision, score, reason, matched = 'reject', 0.0, 'oxide/electroceramic perovskite without halide evidence', ['oxide_context']
    elif perovskite and chemistry and device and not oxide:
        decision, score, reason, matched = 'include', 0.91, 'perovskite plus halide chemistry and device context', ['perovskite', 'halide_chemistry', 'device']
    elif (direct_device or title_device) and not oxide:
        decision, score, reason, matched = 'include', 0.85, 'perovskite device terminology', ['perovskite_device']
    elif perovskite and not oxide and re.search(r'\bpb\s*i\s*2\b', content) and \
            re.search(r'\b(?:photovoltaic|solar cell|power conversion efficiency|pce)\b', content):
        decision, score, reason, matched = 'include', 0.86, 'PbI2 precursor in perovskite photovoltaic context', ['pbi2_perovskite_photovoltaic']
    elif perovskite and oxide:
        decision, score, reason, matched = 'reject', 0.0, 'non-halide catalyst/oxide/battery context', ['oxide_context', 'perovskite']
    elif perovskite and not PEROVSKITE.search(title):
        decision, score, reason, matched = 'review', 0.30, 'perovskite appears only in abstract without halide evidence', ['abstract_only_perovskite']
    elif perovskite:
        decision, score, reason, matched = 'review', 0.40, 'perovskite structure; halide chemistry not established', ['perovskite']
    else:
        decision, score, reason, matched = 'reject', 0.0, 'no halide-perovskite evidence', []
    return {'decision': decision, 'score': score, 'reason': reason,
            'matched_rules': matched, 'version': CLASSIFIER_VERSION}


def load_journals(path=None):
    if path is None:
        path = Path(__file__).resolve().parent.parent / 'config' / 'journals.json'
    return {j['id']: j for j in json.loads(Path(path).read_text(encoding='utf-8'))}
