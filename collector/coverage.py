"""Independent OpenAlex DOI inventory and optional user-supplied publisher-list comparison.

A discrepancy between indexes is a lead for investigation, NOT proof that a
publisher article was missed. These operations never modify SQLite.
"""
import csv
import calendar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .core import USER_AGENT, classify, normalize_doi
from .db import now

BASE = 'https://api.openalex.org'


def openalex_json(path, params=None, attempts=4):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + ('?' + query if query else '')
    headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                if key:
                    raise RuntimeError(
                        f'OpenAlex HTTP {exc.code}: verify that OPENALEX_API_KEY is valid and activated; '
                        'the key itself is not shown') from exc
                raise RuntimeError(
                    f'OpenAlex HTTP {exc.code}: unauthenticated request refused. '
                    'A free API key may be required; see https://help.openalex.org/api/authentication/'
                ) from exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
            delay = min(2 ** (attempt + 1), 30)
            if isinstance(exc, urllib.error.HTTPError) and exc.headers:
                try:
                    delay = min(60, max(0, float(exc.headers.get('Retry-After', delay))))
                except (TypeError, ValueError):
                    pass
            time.sleep(delay)


def _source_id(value):
    """OpenAlex source IDs can be returned as URLs; filter uses short S IDs."""
    match = re.fullmatch(r'(?:https?://openalex\.org/)?(S\d+)', str(value or ''), re.I)
    if not match:
        raise ValueError('Unexpected OpenAlex source ID: ' + repr(value))
    return match.group(1).upper()


def resolve_sources(journal, fetch=openalex_json):
    """Resolve each ISSN and verify that returned source actually carries it."""
    ids = set()
    for issn in journal['issns']:
        response = fetch('/sources', {'filter': 'issn:' + issn, 'per-page': 100})
        for source in response.get('results', []):
            all_issns = set(source.get('issn') or [])
            if source.get('issn_l'):
                all_issns.add(source['issn_l'])
            if issn in all_issns:
                ids.add(_source_id(source['id']))
    if not ids:
        raise ValueError('No matching OpenAlex source IDs for ' + journal['name'])
    return sorted(ids)


def iter_source_works(source_id, start, end, fetch=openalex_json, max_pages=1000,
                      paging='auto'):
    """Read a complete OpenAlex primary-source publication-date window.

    OpenAlex now requires a free key for cursor paging beyond page one.
    Without a key, use ordinary page-number pagination (max 10,000 works).
    A free key enables cursor paging for larger inventories and is preferred
    for more stable traversal of a changing index. Fail closed on truncation.
    """
    if paging not in ('auto', 'cursor', 'page'):
        raise ValueError('paging must be auto, cursor, or page')
    if max_pages < 1:
        raise ValueError('max_pages must be positive')
    if paging == 'auto':
        paging = 'cursor' if os.environ.get('OPENALEX_API_KEY', '').strip() else 'page'
    base_params = {
        'filter': (f'primary_location.source.id:{source_id},'
                   f'from_publication_date:{start},to_publication_date:{end}'),
        'per-page': 100,
    }
    cursor = '*'
    cursors = set()
    seen_work_ids = set()
    count = 0
    expected = None
    limit = min(max_pages, 100) if paging == 'page' else max_pages
    for page_index in range(limit):
        params = dict(base_params)
        if paging == 'cursor':
            if cursor in cursors:
                raise RuntimeError('Repeated OpenAlex cursor')
            cursors.add(cursor)
            params['cursor'] = cursor
        else:
            params['page'] = page_index + 1
        response = fetch('/works', params)
        items = response.get('results')
        if not isinstance(items, list):
            raise ValueError('OpenAlex response missing results list')
        meta = response.get('meta') or {}
        current_expected = meta.get('count')
        if not isinstance(current_expected, int) or current_expected < 0:
            raise ValueError('OpenAlex response missing valid meta.count')
        if expected is None:
            expected = current_expected
            if paging == 'page' and expected > 10000:
                raise RuntimeError(
                    'OpenAlex basic paging cannot safely cover more than 10,000 works; '
                    'use a free OPENALEX_API_KEY for cursor paging')
        elif current_expected != expected:
            raise RuntimeError('OpenAlex result count changed during pagination; rerun audit')
        count += len(items)
        if count > expected:
            raise RuntimeError('OpenAlex returned more records than meta.count; rerun audit')
        for work in items:
            work_id = work.get('id')
            if work_id:
                if work_id in seen_work_ids:
                    raise RuntimeError('OpenAlex repeated a work across pages; rerun audit')
                seen_work_ids.add(work_id)
            yield work
        if count == expected:
            return
        if not items:
            raise RuntimeError(f'OpenAlex ended at {count} of {expected} results')
        if paging == 'cursor':
            cursor = meta.get('next_cursor')
            if not cursor:
                raise RuntimeError(f'OpenAlex cursor missing after {count} of {expected} results')
        elif len(items) < 100:
            raise RuntimeError(f'OpenAlex page was short at {count} of {expected} results')
    if paging == 'page':
        raise RuntimeError('OpenAlex basic paging limit reached; use a free OPENALEX_API_KEY')
    raise RuntimeError('OpenAlex cursor pagination safety limit reached; narrow the date window')


def _known_status(conn, doi):
    if conn.execute('SELECT 1 FROM articles WHERE doi=?', (doi,)).fetchone():
        return 'retained'
    if conn.execute('SELECT 1 FROM rejected_candidates WHERE doi=?', (doi,)).fetchone():
        return 'archived_reject'
    if conn.execute('SELECT 1 FROM deleted_dois WHERE doi=?', (doi,)).fetchone():
        return 'explicitly_deleted'
    return 'not_in_local_inventory'


def _last_run(conn, journal_id, start, end):
    row = conn.execute('SELECT error,details_json FROM runs WHERE journal_id=? ORDER BY id DESC LIMIT 1',
                       (journal_id,)).fetchone()
    if not row:
        return 'no_local_collection_run'
    try:
        details = json.loads(row['details_json'] or '{}')
    except (TypeError, ValueError):
        details = {}
    if row['error']:
        return 'latest_collection_run_failed'
    if details.get('window_start') == start and details.get('window_end') == end:
        return 'matching_successful_collection_window'
    return 'collection_window_unverified_or_different'


def audit_openalex(conn, journals, start, end, output_dir, source_fetch=resolve_sources,
                   works_fetch=iter_source_works):
    """Compare OpenAlex journal DOI inventory with retained + rejected + deleted DOIs.

    Report includes only OpenAlex primary-source works whose OpenAlex publication
    date is in range. This is NOT a publisher completeness or recall estimate.
    """
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError('start after end')
    summaries = []
    unseen_rows = []
    for journal in journals:
        result = {'journal_id': journal['id'], 'journal': journal['name'],
                  'status': 'error', 'error': None, 'source_ids': [],
                  'openalex_dois': 0, 'no_doi': 0, 'retained': 0,
                  'archived_reject': 0, 'explicitly_deleted': 0,
                  'not_in_local_inventory': 0,
                  'local_window_status': _last_run(conn, journal['id'], start, end)}
        try:
            sources = source_fetch(journal)
            result['source_ids'] = sources
            seen = set()
            rows = []
            for source_id in sources:
                for work in works_fetch(source_id, start, end):
                    # Verify the returned primary source: no journal-wide semantic search.
                    primary = ((work.get('primary_location') or {}).get('source') or {}).get('id')
                    if not primary or _source_id(primary) != source_id:
                        raise ValueError('OpenAlex returned work outside requested primary source')
                    published = work.get('publication_date')
                    if not published or not start <= published <= end:
                        raise ValueError('OpenAlex returned work outside requested publication interval')
                    raw_doi = work.get('doi')
                    if not raw_doi:
                        result['no_doi'] += 1
                        continue
                    try:
                        doi = normalize_doi(raw_doi)
                    except ValueError:
                        result['no_doi'] += 1
                        continue
                    if doi in seen:
                        continue
                    seen.add(doi)
                    status = _known_status(conn, doi)
                    result[status] += 1
                    if status == 'not_in_local_inventory':
                        rows.append({'journal_id': journal['id'], 'doi': doi,
                                     'title': work.get('title') or '',
                                     'openalex_publication_date': published,
                                     'openalex_type': work.get('type') or '',
                                     'openalex_id': work.get('id') or '',
                                     'investigation_status': '', 'reviewer_notes': ''})
            result['openalex_dois'] = len(seen)
            result['status'] = 'completed_index_comparison'
            unseen_rows.extend(rows)
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            # Discard counts and candidates from incomplete pagination.
            for key in ('openalex_dois', 'no_doi', 'retained', 'archived_reject',
                        'explicitly_deleted', 'not_in_local_inventory'):
                result[key] = None
        summaries.append(result)
    report = {'generated_at': now(), 'source': 'OpenAlex (independent bibliographic index, not publisher)',
              'window_start': start, 'window_end': end,
              'interpretation': ('Not-in-local-inventory means absent from retained articles, archived rejects, '
                                 'and explicit deletions; it is NOT proof of missing publisher coverage. '
                                 'OpenAlex publication dates and Crossref publication/creation dates can differ.'),
              'journals': summaries}
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'coverage_summary.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    with (target / 'openalex_unseen.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=['journal_id', 'doi', 'title',
            'openalex_publication_date', 'openalex_type', 'openalex_id',
            'investigation_status', 'reviewer_notes'])
        writer.writeheader()
        writer.writerows(unseen_rows)
    return report


def compare_publisher_list(conn, journal, csv_path, output_path):
    """Compare a *user-supplied* publisher DOI list; no claim of source verification."""
    input_path = Path(csv_path)
    rows = []
    seen = set()
    with input_path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or 'doi' not in [x.lower().strip() for x in reader.fieldnames]:
            raise ValueError('CSV requires a doi column (case-insensitive)')
        doi_field = next(x for x in reader.fieldnames if x.lower().strip() == 'doi')
        title_field = next((x for x in reader.fieldnames if x.lower().strip() == 'title'), None)
        date_field = next((x for x in reader.fieldnames if x.lower().strip() in ('date', 'published_date')), None)
        for line, item in enumerate(reader, 2):
            try:
                doi = normalize_doi(item.get(doi_field) or '')
            except ValueError as exc:
                raise ValueError(f'Invalid DOI on CSV line {line}: {exc}') from exc
            if doi in seen:
                continue
            seen.add(doi)
            rows.append({'journal_id': journal['id'], 'doi': doi,
                         'publisher_list_title': item.get(title_field, '') if title_field else '',
                         'publisher_list_date': item.get(date_field, '') if date_field else '',
                         'local_status': _known_status(conn, doi),
                         'investigation_status': '', 'reviewer_notes': ''})
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [
            'journal_id', 'doi', 'publisher_list_title', 'publisher_list_date',
            'local_status', 'investigation_status', 'reviewer_notes'])
        writer.writeheader()
        writer.writerows(rows)
    return {'journal': journal['id'], 'input_dois': len(rows),
            'not_in_local_inventory': sum(r['local_status'] == 'not_in_local_inventory' for r in rows),
            'note': 'Comparison to a user-supplied list; provenance and date-window accuracy are not verified'}


def _publication_window_relation(value, precision, start, end):
    """Conservatively compare a precision-preserving date against a day window."""
    if not value or precision not in ('year', 'month', 'day'):
        return 'unknown'
    try:
        if precision == 'year':
            first, last = value + '-01-01', value + '-12-31'
        elif precision == 'month':
            year, month = map(int, value.split('-'))
            first = f'{year:04d}-{month:02d}-01'
            last = f'{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}'
        else:
            date.fromisoformat(value)
            first = last = value
    except (ValueError, TypeError):
        return 'unknown'
    if last < start or first > end:
        return 'outside'
    if first >= start and last <= end:
        return 'inside'
    return 'overlaps_window_imprecise'


def investigate_unseen(conn, csv_path, output_path, journals, start, end, fetch=None):
    """Read-only Crossref DOI diagnostics for OpenAlex discrepancies.

    Does not add, reclassify, or delete papers. A publication-date difference
    is not itself proof of a collection bug.
    """
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError('start after end')
    if fetch is None:
        from .core import crossref_doi
        fetch = crossref_doi
    input_rows = []
    seen = set()
    with Path(csv_path).open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {'doi', 'journal_id'} <= set(reader.fieldnames):
            raise ValueError('Input CSV requires doi and journal_id columns')
        for line, row in enumerate(reader, 2):
            doi = normalize_doi(row.get('doi') or '')
            journal_id = (row.get('journal_id') or '').strip()
            if journal_id not in journals:
                raise ValueError(f'Unknown journal_id on CSV line {line}: {journal_id!r}')
            if doi in seen:
                continue
            seen.add(doi)
            input_rows.append((doi, journal_id, row))
            if len(input_rows) > 1000:
                raise ValueError('At most 1000 DOIs per investigation; split the input CSV')
    output_rows = []
    for doi, journal_id, original in input_rows:
        row = {
            'journal_id': journal_id, 'doi': doi,
            'openalex_publication_date': original.get('openalex_publication_date') or '',
            'openalex_title': original.get('title') or '',
            'local_status': _known_status(conn, doi),
            'crossref_journal': '', 'crossref_issn_match': '',
            'crossref_published': '', 'crossref_date_precision': '',
            'crossref_date_source': '', 'crossref_created': '',
            'crossref_deposited': '', 'crossref_date_vs_window': '',
            'classifier_decision': '', 'error': '',
        }
        try:
            paper = fetch(doi)
            if normalize_doi(paper.get('doi') or '') != doi:
                raise ValueError('Crossref DOI mismatch; metadata ignored')
            issns = set(paper.get('issns') or [])
            journal = journals[journal_id]
            expected_issns = set(journal['issns']) | set(journal.get('crossref_issns', []))
            row.update({
                'crossref_journal': paper.get('journal') or '',
                'crossref_issn_match': 'yes' if issns & expected_issns else 'no' if issns else 'unknown',
                'crossref_published': paper.get('published_date') or '',
                'crossref_date_precision': paper.get('published_date_precision') or '',
                'crossref_date_source': paper.get('publication_date_source') or '',
                'crossref_created': paper.get('metadata_created') or '',
                'crossref_deposited': paper.get('metadata_deposited') or '',
                'crossref_date_vs_window': _publication_window_relation(
                    paper.get('published_date'), paper.get('published_date_precision'), start, end),
                'classifier_decision': classify(paper)['decision'],
            })
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        output_rows.append(row)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        'journal_id', 'doi', 'openalex_publication_date', 'openalex_title',
        'local_status', 'crossref_journal', 'crossref_issn_match',
        'crossref_published', 'crossref_date_precision', 'crossref_date_source',
        'crossref_created', 'crossref_deposited', 'crossref_date_vs_window',
        'classifier_decision', 'error',
    ]
    with target.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)
    return {
        'investigated': len(output_rows),
        'errors': sum(bool(row['error']) for row in output_rows),
        'inside_publication_window': sum(row['crossref_date_vs_window'] == 'inside'
                                         for row in output_rows),
        'output': str(target),
        'note': 'Read-only diagnostic; Crossref and OpenAlex publication dates may differ',
    }
