"""Fail-closed public catalog validation and persistent, stateless weekly merging.

The public JSON is the *only* cross-run persistence for GitHub Actions. It
contains no SQLite database, annotations, abstracts or rejected-paper archive.
"""
import json
from pathlib import Path

from .core import normalize_doi
from .db import now

ARTICLE_FIELDS = frozenset({
    'doi', 'title', 'journal', 'published_date', 'published_date_precision',
    'published_online', 'type', 'decision', 'abstract_status', 'first_seen_at',
    'manually_added', 'authors', 'topics',
})
TOP_FIELDS = frozenset({'schema_version', 'generated_at', 'abstracts_included', 'articles'})
REQUIRED_FIELDS = frozenset({'doi', 'title', 'decision', 'authors'})
PRECISION = {None, 'year', 'month', 'day'}
MAX_RECORDS = 100_000


def read_public_catalog(path):
    """Validate *every* record and disallow unrecognized/private fields."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != TOP_FIELDS:
        raise ValueError('Invalid public catalog envelope (unexpected or missing keys)')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported public catalog schema')
    if data['abstracts_included'] is not False:
        raise ValueError('Refusing to publish abstracts: redistribution rights not established')
    if not isinstance(data['generated_at'], str) or not data['generated_at']:
        raise ValueError('Missing catalog generation timestamp')
    if not isinstance(data['articles'], list) or len(data['articles']) > MAX_RECORDS:
        raise ValueError('Invalid or oversized public article list')
    seen = set()
    for i, item in enumerate(data['articles']):
        if not isinstance(item, dict) or not REQUIRED_FIELDS <= set(item) or not set(item) <= ARTICLE_FIELDS:
            raise ValueError(f'Unsafe/invalid public fields in article {i}')
        doi = item['doi']
        if not isinstance(doi, str) or normalize_doi(doi) != doi:
            raise ValueError(f'Invalid/noncanonical DOI at article {i}')
        if doi in seen:
            raise ValueError(f'Duplicate DOI in public catalog: {doi}')
        seen.add(doi)
        if item['decision'] not in ('include', 'review'):
            raise ValueError(f'Non-public classification for {doi}')
        if not isinstance(item['title'], str) or not item['title'].strip():
            raise ValueError(f'Missing title for {doi}')
        if not isinstance(item['authors'], list) or not all(isinstance(a, str) for a in item['authors']):
            raise ValueError(f'Invalid authors for {doi}')
        if 'topics' in item:
            from .topics import TOPIC_IDS
            value = item['topics']
            if (not isinstance(value, list) or len(value) > len(TOPIC_IDS) or
                    len(set(map(str, value))) != len(value) or
                    any(not isinstance(t, str) or t not in TOPIC_IDS for t in value)):
                raise ValueError(f'Invalid topics for {doi}')
        if item.get('published_date_precision') not in PRECISION:
            raise ValueError(f'Invalid publication date precision for {doi}')
        if item.get('manually_added', 0) not in (0, 1, False, True):
            raise ValueError(f'Invalid manual flag for {doi}')
        for name in ('journal', 'published_date', 'published_online', 'type',
                     'abstract_status', 'first_seen_at'):
            if name in item and item[name] is not None and not isinstance(item[name], str):
                raise ValueError(f'Invalid {name} for {doi}')
    return data


def read_public_exclusions(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'dois'} or not isinstance(data['dois'], list):
        raise ValueError('Exclusions must be JSON with exactly one key: "dois" (list)')
    dois = set()
    for value in data['dois']:
        if not isinstance(value, str):
            raise ValueError('Exclusion DOIs must be strings')
        doi = normalize_doi(value)
        if doi in dois:
            raise ValueError('Duplicate DOI in public exclusions: ' + doi)
        dois.add(doi)
    return dois


def merge_public_catalog(previous, incoming, output, exclusions):
    """Union by DOI, retaining old published papers unless explicitly excluded.

    Newly retrieved metadata can improve a title/date and promote Review to
    Include. It cannot silently remove an existing paper or demote Include.
    """
    old = read_public_catalog(previous)
    new = read_public_catalog(incoming)
    excluded = read_public_exclusions(exclusions)
    papers = {p['doi']: dict(p) for p in old['articles'] if p['doi'] not in excluded}
    old_count = len(papers)
    new_dois = set()
    for paper in new['articles']:
        doi = paper['doi']
        if doi in excluded:
            continue
        new_dois.add(doi)
        if doi not in papers:
            papers[doi] = dict(paper)
            continue
        prior = papers[doi]
        updated = dict(prior)
        for field in ARTICLE_FIELDS - {'doi', 'decision', 'first_seen_at', 'manually_added',
                                       'published_date', 'published_date_precision'}:
            if field in paper and paper[field] not in (None, '', []):
                updated[field] = paper[field]
        # Publication precision must never regress from day to month/year.
        rank = {None: 0, 'year': 1, 'month': 2, 'day': 3}
        if (paper.get('published_date') and
                rank[paper.get('published_date_precision')] >= rank[prior.get('published_date_precision')]):
            updated['published_date'] = paper['published_date']
            updated['published_date_precision'] = paper.get('published_date_precision')
        if prior.get('abstract_status') == 'available':
            updated['abstract_status'] = 'available'
        updated['first_seen_at'] = prior.get('first_seen_at') or paper.get('first_seen_at')
        updated['decision'] = 'include' if 'include' in (prior['decision'], paper['decision']) else 'review'
        papers[doi] = updated
    if len(papers) < old_count:
        raise RuntimeError('Catalog would lose previously published papers')
    # Legacy Step 3 catalogs may contain a manual-curation flag; strip it
    # from public output. Manually added papers themselves remain present.
    for item in papers.values():
        item.pop('manually_added', None)
    merged = {'schema_version': 1, 'generated_at': now(), 'abstracts_included': False,
              'articles': sorted(papers.values(), key=lambda p: (p.get('first_seen_at') or '', p['doi']),
                                 reverse=True)}
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.tmp')
    temp.write_text(json.dumps(merged, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    # Validate the final file before replacing the existing published catalog.
    try:
        read_public_catalog(temp)
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()
    return {'previous': len(old['articles']), 'incoming': len(new['articles']),
            'public_articles': len(papers), 'new_dois': len(new_dois - set(p['doi'] for p in old['articles'])),
            'explicit_exclusions': len(excluded), 'private_annotations_exported': False}
