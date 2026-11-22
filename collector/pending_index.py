"""Strict, public-safe pending DOI inventory for stateless GitHub Actions runs.

Only bibliographic metadata, never abstracts or private user annotations.
"""
import json
from pathlib import Path

from .core import normalize_doi
from .db import get_known_abstract, now, record_pending_abstract

FIELDS = ('doi', 'title', 'journal', 'authors', 'issns', 'published_date',
          'published_date_precision', 'published_online', 'published_online_precision',
          'published_print', 'published_print_precision', 'publication_date_source',
          'metadata_created', 'metadata_deposited', 'type', 'url', 'source')
MAX_RECORDS = 100000


def _validate(data):
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'generated_at', 'papers'}
            or data['schema_version'] != 1 or not isinstance(data['papers'], list)
            or len(data['papers']) > MAX_RECORDS):
        raise ValueError('Invalid pending inventory envelope')
    seen = set()
    for row in data['papers']:
        if not isinstance(row, dict) or set(row) != {'journal_id', 'paper'}:
            raise ValueError('Invalid pending inventory entry')
        if not isinstance(row['journal_id'], str) or not row['journal_id']:
            raise ValueError('Missing journal ID')
        p = row['paper']
        if not isinstance(p, dict) or not {'doi', 'title'} <= set(p) or not set(p) <= set(FIELDS):
            raise ValueError('Unsafe pending inventory fields')
        if normalize_doi(p['doi']) != p['doi'] or p['doi'] in seen:
            raise ValueError('Invalid or duplicate pending DOI')
        seen.add(p['doi'])
        if not isinstance(p['title'], str) or not p['title']:
            raise ValueError('Missing pending paper title')
        if not isinstance(p.get('authors', []), list) or not isinstance(p.get('issns', []), list):
            raise ValueError('Invalid pending authors/ISSNs')
        for key, val in p.items():
            if key in ('authors', 'issns'):
                if not all(isinstance(x, str) for x in val):
                    raise ValueError('Invalid pending authors/ISSNs')
            elif val is not None and not isinstance(val, str):
                raise ValueError('Invalid pending field type: ' + key)
    return data


def export_pending(conn, path):
    papers = []
    for row in conn.execute('SELECT journal_id,paper_json FROM pending_abstracts ORDER BY doi'):
        paper = json.loads(row['paper_json'])
        # Do not export even if legacy input contains an abstract or private key.
        record = {k: paper[k] for k in FIELDS if k in paper}
        papers.append({'journal_id': row['journal_id'], 'paper': record})
    payload = _validate({'schema_version': 1, 'generated_at': now(), 'papers': papers})
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temp.replace(target)
    return {'pending': len(papers), 'path': str(target), 'abstracts_exported': False}


def import_pending(conn, path):
    data = _validate(json.loads(Path(path).read_text(encoding='utf-8')))
    imported = 0
    for row in data['papers']:
        paper = row['paper']
        if get_known_abstract(conn, paper['doi'])[0]:
            continue
        if record_pending_abstract(conn, paper, row['journal_id']):
            imported += 1
    return {'imported': imported, 'total': len(data['papers'])}
