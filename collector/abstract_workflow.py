"""Abstract-first classification and repair of legacy title-only records."""
import csv
import json
from pathlib import Path

from .abstracts import AbstractResolver
from .core import classify
from .db import (can_retry_abstract, get_known_abstract, record_abstract_check,
                 record_pending_abstract, record_rejected, upsert_article)


def resolve_before_classification(conn, paper, journal_id, resolver, budget=None):
    """Return (paper, outcome), never classify if abstract remains unresolved.

    budget is a one-element list of remaining remote DOI lookups, shared by the
    journal's retrieval modes. Local and Crossref abstracts cost no lookup.
    """
    paper = dict(paper)
    if not paper.get('abstract'):
        existing, source = get_known_abstract(conn, paper['doi'])
        if existing:
            paper['abstract'], paper['abstract_source'] = existing, source
    if paper.get('abstract'):
        return paper, {'status': 'available', 'source': paper.get('abstract_source') or 'crossref',
                       'abstract': paper['abstract'], 'attempts': [], 'lookup': False}
    if not can_retry_abstract(conn, paper['doi']):
        record_pending_abstract(conn, paper, journal_id)
        return paper, {'status': 'retry_deferred', 'abstract': None, 'source': None,
                       'attempts': [], 'lookup': False}
    if budget is not None and budget[0] <= 0:
        record_pending_abstract(conn, paper, journal_id)
        return paper, {'status': 'budget_deferred', 'abstract': None, 'source': None,
                       'attempts': [], 'lookup': False}
    if budget is not None:
        budget[0] -= 1
    result = resolver.resolve(paper)
    result['lookup'] = True
    record_abstract_check(conn, paper['doi'], result)
    if result.get('abstract'):
        paper['abstract'] = result['abstract']
        paper['abstract_source'] = result['source']
        paper['abstract_status'] = 'available'
    else:
        record_pending_abstract(conn, paper, journal_id)
    return paper, result


def _records(conn, journals, journal_id, sources):
    journal_name = journals[journal_id]['name'] if journal_id != 'all' else None
    if 'pending' in sources:
        sql = 'SELECT * FROM pending_abstracts'
        params = []
        if journal_id != 'all':
            sql += ' WHERE journal_id=?'
            params = [journal_id]
        for row in conn.execute(sql + ' ORDER BY first_seen_at,doi', params).fetchall():
            yield 'pending', row['journal_id'], json.loads(row['paper_json'])
    if 'articles' in sources:
        sql = 'SELECT * FROM articles WHERE abstract IS NULL'
        params = []
        if journal_name:
            sql += ' AND journal=?'
            params = [journal_name]
        for row in conn.execute(sql + ' ORDER BY first_seen_at,doi', params).fetchall():
            paper = dict(row)
            paper['authors'] = json.loads(row['authors_json'])
            paper['issns'] = json.loads(row['issns_json'])
            j_id = next((k for k, j in journals.items() if j['name'] == paper['journal']), 'manual')
            yield 'articles', j_id, paper
    if 'archived' in sources:
        sql = 'SELECT * FROM rejected_candidates'
        params = []
        if journal_id != 'all':
            sql += ' WHERE journal_id=?'
            params = [journal_id]
        for row in conn.execute(sql + ' ORDER BY first_seen_at,doi', params).fetchall():
            paper = json.loads(row['paper_json'])
            if not paper.get('abstract'):
                yield 'archived', row['journal_id'], paper


def repair_abstracts(conn, journals, journal_id='all', limit=20, retry=False,
                     dry_run=False, sources=('pending', 'articles', 'archived'),
                     resolver=None, output=None):
    """Fetch missing abstracts, then reclassify. Existing notes/ratings survive."""
    if not 1 <= limit <= 2000:
        raise ValueError('limit must be between 1 and 2000')
    if journal_id != 'all' and journal_id not in journals:
        raise ValueError('Unknown journal: ' + journal_id)
    if any(x not in ('pending', 'articles', 'archived') for x in sources):
        raise ValueError('Unknown repair source')
    resolver = resolver or AbstractResolver()
    summary = {'journal': journal_id, 'checked': 0, 'available': 0,
               'unresolved': 0, 'deferred': 0, 'metadata_pending': 0, 'promoted': 0,
               'rejected_after_abstract': 0, 'provider_errors': 0,
               'dry_run': bool(dry_run), 'results': []}
    seen = set()
    for origin, j_id, paper in _records(conn, journals, journal_id, sources):
        doi = paper['doi']
        if conn.execute('SELECT 1 FROM deleted_dois WHERE doi=?', (doi,)).fetchone():
            continue
        if doi in seen:
            continue
        seen.add(doi)
        if not can_retry_abstract(conn, doi, force=retry):
            summary['deferred'] += 1
            continue
        if summary['checked'] >= limit:
            break
        result = resolver.resolve(paper)
        summary['checked'] += 1
        status = result['status']
        row = {'doi': doi, 'journal_id': j_id, 'origin': origin,
               'title': paper.get('title', ''), 'status': status,
               'source': result.get('source') or '', 'decision': '',
               'provider_attempts': ';'.join(x['provider'] + ':' + x['status']
                                            for x in result['attempts'])}
        if status == 'available':
            summary['available'] += 1
            paper['abstract'] = result['abstract']
            paper['abstract_source'] = result['source']
            paper['abstract_status'] = 'available'
            if not (paper.get('title') or '').strip():
                # A retrieved abstract does not make a titleless record safe
                # to publish. Keep the DOI in the retry inventory.
                summary['metadata_pending'] += 1
                row['decision'] = 'pending_metadata'
                if not dry_run:
                    record_pending_abstract(conn, paper, j_id)
            else:
                decision = classify(paper)
                row['decision'] = decision['decision']
                if origin == 'archived' and decision['decision'] != 'reject':
                    summary['promoted'] += 1
                if decision['decision'] == 'reject':
                    summary['rejected_after_abstract'] += 1
                if not dry_run:
                    if origin == 'articles' or conn.execute(
                            'SELECT 1 FROM articles WHERE doi=?', (doi,)).fetchone() or decision['decision'] != 'reject':
                        manually_added = conn.execute('SELECT manually_added FROM articles WHERE doi=?', (doi,)).fetchone()
                        upsert_article(conn, paper, decision, manual=bool(manually_added and manually_added[0]))
                    else:
                        record_rejected(conn, paper, decision, j_id)
        else:
            summary['unresolved'] += 1
            if status == 'provider_error':
                summary['provider_errors'] += 1
            if not dry_run:
                record_pending_abstract(conn, paper, j_id)
        if not dry_run:
            record_abstract_check(conn, doi, result)
        summary['results'].append(row)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ['doi', 'journal_id', 'origin', 'title', 'status', 'source',
                  'decision', 'provider_attempts']
        with path.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(summary['results'])
    return summary
