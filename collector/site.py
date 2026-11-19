"""Create a public-safe, read-only static catalog for free hosting.

Never exports SQLite, tombstones, rejected candidates, or per-user state.
"""
import json
import shutil
from pathlib import Path

from .db import now

SITE_ASSETS = Path(__file__).resolve().parent.parent / 'site'
FIELDS = ('doi', 'title', 'journal', 'published_date', 'published_date_precision',
          'published_online', 'type', 'decision', 'abstract_status', 'first_seen_at')


def build_public_site(conn, output_dir='docs', include_abstracts=False):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    papers = []
    for row in conn.execute("SELECT * FROM articles WHERE decision IN ('include','review') "
                            "ORDER BY COALESCE(first_seen_at,added_at) DESC,doi"):
        item = {key: row[key] for key in FIELDS}
        item['authors'] = json.loads(row['authors_json'])
        if include_abstracts:
            item['abstract'] = row['abstract']
        papers.append(item)
    payload = {'schema_version': 1, 'generated_at': now(),
               'abstracts_included': bool(include_abstracts), 'articles': papers}
    temp = output / 'articles.json.tmp'
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                    encoding='utf-8')
    temp.replace(output / 'articles.json')
    for asset in ('index.html', 'app.js', 'styles.css'):
        shutil.copyfile(SITE_ASSETS / asset, output / asset)
    (output / '.nojekyll').touch()
    return {'output': str(output), 'public_articles': len(papers),
            'abstracts_included': bool(include_abstracts),
            'private_annotations_exported': False,
            'notes': 'Static read-only catalog; user state is NOT synchronized'}
