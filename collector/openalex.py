"""Optional, bounded OpenAlex DOI enrichment. No third-party dependencies."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import normalize_doi, strip_markup, USER_AGENT
from .db import enrich_abstract

BASE = 'https://api.openalex.org'


def reconstruct_abstract(index):
    """Reconstruct words from OpenAlex abstract_inverted_index; return plain text."""
    if not isinstance(index, dict) or not index:
        return None
    positions = {}
    for token, offsets in index.items():
        if not isinstance(token, str) or not isinstance(offsets, list):
            return None
        for position in offsets:
            if not isinstance(position, int) or position < 0 or position > 5000:
                return None
            positions[position] = token
    if not positions or len(positions) > 5000:
        return None
    words = [positions.get(i, '') for i in range(max(positions) + 1)]
    text = strip_markup(' '.join(words))
    return text or None


def openalex_doi(doi, attempts=3):
    """Fetch a singleton by DOI (free under OpenAlex's Nov 2026 pricing)."""
    doi = normalize_doi(doi)
    # OpenAlex documents singleton paths of the form /works/https://doi.org/<DOI>.
    url = BASE + '/works/https://doi.org/' + urllib.parse.quote(doi, safe='/')
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            try:
                delay = min(max(float(retry_after), 0), 60) if retry_after else min(2 ** attempt * 2, 20)
            except (TypeError, ValueError):
                delay = min(2 ** attempt * 2, 20)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt * 2, 20))


def enrich_missing_abstracts(conn, limit=60, retry=False, fetch=openalex_doi):
    """Enrich missing abstracts only; no overwrite of existing abstracts."""
    if limit < 1 or limit > 500:
        raise ValueError('limit must be between 1 and 500')
    query = "SELECT doi FROM articles WHERE abstract IS NULL"
    if not retry:
        query += ' AND openalex_checked_at IS NULL'
    query += ' ORDER BY first_seen_at, doi LIMIT ?'
    summary = {'checked': 0, 'filled': 0, 'not_available': 0,
               'errors': [], 'remaining_missing': None}
    for row in conn.execute(query, (limit,)).fetchall():
        doi = row['doi']
        try:
            work = fetch(doi)
            if work:
                remote_doi = work.get('doi')
                if not remote_doi or normalize_doi(remote_doi) != doi:
                    raise ValueError('OpenAlex DOI mismatch; metadata ignored')
                abstract = reconstruct_abstract(work.get('abstract_inverted_index'))
            else:
                abstract = None
            enrich_abstract(conn, doi, abstract)
            summary['checked'] += 1
            summary['filled' if abstract else 'not_available'] += 1
        except Exception as exc:
            summary['errors'].append({'doi': doi, 'error': f'{type(exc).__name__}: {exc}'})
            # Stop on API errors rather than repeatedly hitting a quota or bad key.
            break
    summary['remaining_missing'] = conn.execute('SELECT count(*) FROM articles WHERE abstract IS NULL').fetchone()[0]
    return summary
