"""Strictly validate and apply an explicitly downloaded public collector configuration.

This is a local action: Cloudflare settings cannot silently change a GitHub
repository or scheduled collection. No credentials or private state included.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def valid_issn(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{3}[\dX]', value):
        return False
    digits = value.replace('-', '')
    check = (11 - sum(int(digits[i]) * (8 - i) for i in range(7)) % 11) % 11
    return digits[7] == ('X' if check == 10 else str(check))


def validate_export(payload):
    if not isinstance(payload, dict) or set(payload) != {'schema_version', 'journals', 'topic_keywords', 'collection'} or payload['schema_version'] != 1:
        raise ValueError('Unexpected configuration format')
    collection = payload['collection']
    if not isinstance(collection, dict) or set(collection) != {'lookback_days', 'backfill_years'} or not (
            type(collection['lookback_days']) is int and 7 <= collection['lookback_days'] <= 120 and
            type(collection['backfill_years']) is int and 1 <= collection['backfill_years'] <= 5):
        raise ValueError('Invalid collection window')
    from .topics import TOPIC_IDS
    keywords = payload['topic_keywords']
    if not isinstance(keywords, dict) or set(keywords) != set(TOPIC_IDS):
        raise ValueError('Invalid topic keyword set')
    for topic, terms in keywords.items():
        if (not isinstance(terms, list) or len(terms) > 35 or
                any(not isinstance(t, str) or not t.strip() or len(t) > 65 or re.search(r'[<>\r\n]', t) for t in terms) or
                len({t.casefold() for t in terms}) != len(terms)):
            raise ValueError('Invalid keywords for ' + topic)
    journals = payload['journals']
    if not isinstance(journals, list) or not 1 <= len(journals) <= 40:
        raise ValueError('Invalid journal count')
    ids, issns = set(), set()
    for journal in journals:
        if not isinstance(journal, dict) or set(journal) != {'id', 'name', 'issns', 'crossref_issns'}:
            raise ValueError('Invalid journal fields')
        jid, name, numbers, retrieval = (journal[k] for k in ('id', 'name', 'issns', 'crossref_issns'))
        if (not isinstance(jid, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', jid) or jid in ids or
                not isinstance(name, str) or not name.strip() or len(name) > 150 or re.search(r'[<>]', name) or
                not isinstance(numbers, list) or not 1 <= len(numbers) <= 8 or
                not isinstance(retrieval, list) or retrieval != numbers):
            raise ValueError('Invalid journal identity or ISSN list')
        ids.add(jid)
        for number in numbers:
            if not valid_issn(number) or number in issns:
                raise ValueError('Invalid or duplicate ISSN: ' + str(number))
            issns.add(number)
    return payload


def apply_export(path, root=ROOT):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError('Configuration file exceeds 64 KiB')
    data = validate_export(json.loads(path.read_text(encoding='utf-8')))
    output = Path(root) / 'config'
    output.mkdir(parents=True, exist_ok=True)
    targets = {
        'journals.json': data['journals'],
        'topic-keywords.json': data['topic_keywords'],
        'collection.json': data['collection'],
    }
    for filename, value in targets.items():
        destination = output / filename
        temporary = destination.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(destination)
    return {'updated_files': [str(output / x) for x in targets], 'journals': len(data['journals']),
            'lookback_days': data['collection']['lookback_days'], 'requires_git_commit': True}
