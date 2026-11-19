"""Persistent SQLite storage with non-destructive migrations and provenance."""
import csv
import hashlib
import html
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ARTICLE_ADDITIONS = {
    'published_date_precision': 'TEXT',
    'publication_date_source': 'TEXT',
    'published_online': 'TEXT',
    'published_online_precision': 'TEXT',
    'published_print': 'TEXT',
    'published_print_precision': 'TEXT',
    'metadata_created': 'TEXT',
    'metadata_deposited': 'TEXT',
    'first_seen_at': 'TEXT',
    'abstract_source': 'TEXT',
    'openalex_checked_at': 'TEXT',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS articles (
        doi TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT,
        abstract_status TEXT NOT NULL, journal TEXT, published_date TEXT,
        authors_json TEXT NOT NULL DEFAULT '[]', issns_json TEXT NOT NULL DEFAULT '[]',
        type TEXT, url TEXT, source TEXT, decision TEXT NOT NULL,
        score REAL NOT NULL, classifier_version TEXT NOT NULL,
        added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        manually_added INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS user_articles (
        doi TEXT NOT NULL REFERENCES articles(doi) ON DELETE CASCADE,
        user_id TEXT NOT NULL DEFAULT 'local', acknowledged INTEGER NOT NULL DEFAULT 0,
        hidden INTEGER NOT NULL DEFAULT 0, rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(doi, user_id)
      );
      CREATE TABLE IF NOT EXISTS deleted_dois (doi TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, journal_id TEXT, started_at TEXT,
        ended_at TEXT, scanned INTEGER, included INTEGER, review INTEGER,
        rejected INTEGER, error TEXT
      );
      CREATE TABLE IF NOT EXISTS rejected_candidates (
        doi TEXT PRIMARY KEY, journal_id TEXT, paper_json TEXT NOT NULL,
        score REAL NOT NULL, reason TEXT NOT NULL, classifier_version TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS articles_date ON articles(published_date);
      CREATE INDEX IF NOT EXISTS articles_journal ON articles(journal);
      CREATE INDEX IF NOT EXISTS rejected_candidates_journal ON rejected_candidates(journal_id);
    ''')
    run_columns = {row[1] for row in conn.execute('PRAGMA table_info(runs)')}
    if 'details_json' not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
    columns = {row[1] for row in conn.execute('PRAGMA table_info(articles)')}
    for name, sql_type in ARTICLE_ADDITIONS.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE articles ADD COLUMN {name} {sql_type}')
    conn.execute('UPDATE articles SET first_seen_at=added_at WHERE first_seen_at IS NULL')
    conn.execute("UPDATE articles SET abstract_source='crossref' WHERE abstract IS NOT NULL AND abstract_source IS NULL")
    # Decode legacy journal names such as 'Energy &amp; Environmental Science'.
    for row in conn.execute('SELECT doi,journal FROM articles WHERE journal LIKE ?', ('%&%;%',)):
        decoded = html.unescape(row['journal'])
        if decoded != row['journal']:
            conn.execute('UPDATE articles SET journal=? WHERE doi=?', (decoded, row['doi']))
    conn.commit()
    return conn


def _article_values(paper, classification, manual, timestamp):
    return (
        paper['doi'], paper['title'], paper.get('abstract'),
        'available' if paper.get('abstract') else 'missing',
        html.unescape(paper.get('journal') or ''), paper.get('published_date'),
        json.dumps(paper.get('authors', []), ensure_ascii=False),
        json.dumps(paper.get('issns', []), ensure_ascii=False),
        paper.get('type'), paper.get('url'), paper.get('source'),
        'include' if manual else classification['decision'],
        classification['score'], classification['version'], timestamp, timestamp,
        int(manual), paper.get('published_date_precision'),
        paper.get('publication_date_source'), paper.get('published_online'),
        paper.get('published_online_precision'), paper.get('published_print'),
        paper.get('published_print_precision'), paper.get('metadata_created'),
        paper.get('metadata_deposited'), timestamp,
        paper.get('abstract_source') or ('crossref' if paper.get('abstract') else None),
    )


def upsert_article(conn, paper, classification, manual=False):
    """Keep existing user state, manual inclusion, first-seen time and good abstracts."""
    timestamp = now()
    if manual:
        conn.execute('DELETE FROM deleted_dois WHERE doi=?', (paper['doi'],))
    elif conn.execute('SELECT 1 FROM deleted_dois WHERE doi=?', (paper['doi'],)).fetchone():
        return False
    conn.execute('''
      INSERT INTO articles(
        doi,title,abstract,abstract_status,journal,published_date,
        authors_json,issns_json,type,url,source,decision,score,classifier_version,
        added_at,updated_at,manually_added,published_date_precision,
        publication_date_source,published_online,published_online_precision,
        published_print,published_print_precision,metadata_created,metadata_deposited,
        first_seen_at,abstract_source)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(doi) DO UPDATE SET
        title=CASE WHEN excluded.title!='' THEN excluded.title ELSE articles.title END,
        abstract=COALESCE(excluded.abstract,articles.abstract),
        abstract_status=CASE WHEN COALESCE(excluded.abstract,articles.abstract) IS NULL THEN 'missing' ELSE 'available' END,
        abstract_source=CASE WHEN excluded.abstract IS NOT NULL THEN excluded.abstract_source ELSE articles.abstract_source END,
        journal=COALESCE(NULLIF(excluded.journal,''),articles.journal),
        published_date=COALESCE(excluded.published_date,articles.published_date),
        published_date_precision=CASE WHEN excluded.published_date IS NOT NULL
          THEN excluded.published_date_precision ELSE articles.published_date_precision END,
        publication_date_source=CASE WHEN excluded.published_date IS NOT NULL
          THEN excluded.publication_date_source ELSE articles.publication_date_source END,
        published_online=COALESCE(excluded.published_online,articles.published_online),
        published_online_precision=COALESCE(excluded.published_online_precision,articles.published_online_precision),
        published_print=COALESCE(excluded.published_print,articles.published_print),
        published_print_precision=COALESCE(excluded.published_print_precision,articles.published_print_precision),
        metadata_created=COALESCE(excluded.metadata_created,articles.metadata_created),
        metadata_deposited=COALESCE(excluded.metadata_deposited,articles.metadata_deposited),
        authors_json=CASE WHEN excluded.authors_json!='[]' THEN excluded.authors_json ELSE articles.authors_json END,
        issns_json=CASE WHEN excluded.issns_json!='[]' THEN excluded.issns_json ELSE articles.issns_json END,
        type=COALESCE(excluded.type,articles.type),
        url=COALESCE(excluded.url,articles.url),
        source=excluded.source,
        decision=CASE WHEN articles.manually_added=1 THEN 'include' ELSE excluded.decision END,
        score=excluded.score,
        classifier_version=excluded.classifier_version,
        updated_at=excluded.updated_at,
        manually_added=MAX(articles.manually_added,excluded.manually_added)
    ''', _article_values(paper, classification, manual, timestamp))
    conn.execute('DELETE FROM rejected_candidates WHERE doi=?', (paper['doi'],))
    conn.commit()
    return True


def record_rejected(conn, paper, classification, journal_id):
    """Store rejected metadata for later recall audits; never resurrect tombstones."""
    doi = paper['doi']
    if conn.execute('SELECT 1 FROM deleted_dois WHERE doi=?', (doi,)).fetchone():
        return False
    timestamp = now()
    conn.execute('''
      INSERT INTO rejected_candidates(doi,journal_id,paper_json,score,reason,
        classifier_version,first_seen_at,last_seen_at)
      VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(doi) DO UPDATE SET
        journal_id=excluded.journal_id,paper_json=excluded.paper_json,
        score=excluded.score,reason=excluded.reason,
        classifier_version=excluded.classifier_version,last_seen_at=excluded.last_seen_at
    ''', (doi, journal_id, json.dumps(paper, ensure_ascii=False),
          classification['score'], classification['reason'],
          classification['version'], timestamp, timestamp))
    conn.commit()
    return True


def enrich_abstract(conn, doi, abstract, checked_at=None):
    """Record an OpenAlex lookup without overwriting an existing Crossref abstract."""
    timestamp = checked_at or now()
    if abstract:
        conn.execute('''UPDATE articles SET abstract=COALESCE(abstract,?),
          abstract_status='available',
          abstract_source=CASE WHEN abstract IS NULL THEN 'openalex' ELSE abstract_source END,
          openalex_checked_at=?,updated_at=? WHERE doi=?''',
          (abstract, timestamp, timestamp, doi))
    else:
        conn.execute('UPDATE articles SET openalex_checked_at=? WHERE doi=?', (timestamp, doi))
    conn.commit()


def reclassify_database(conn, classify, dry_run=False):
    """Re-score retained articles and archived rejects; never delete or override manual inclusion."""
    summary = {'articles_checked': 0, 'decision_changes': {},
               'classifier_versions_updated': 0,
               'rejected_checked': 0, 'promoted': 0, 'still_rejected': 0,
               'dry_run': bool(dry_run)}
    for row in conn.execute('SELECT * FROM articles').fetchall():
        paper = dict(row)
        classification = classify(paper)
        if row['manually_added']:
            classification['decision'] = 'include'
        summary['articles_checked'] += 1
        if row['decision'] != classification['decision']:
            key = row['decision'] + ' -> ' + classification['decision']
            summary['decision_changes'][key] = summary['decision_changes'].get(key, 0) + 1
        if row['classifier_version'] != classification['version']:
            summary['classifier_versions_updated'] += 1
        if not dry_run:
            conn.execute('''UPDATE articles SET decision=?,score=?,classifier_version=?,updated_at=?
                WHERE doi=?''', (classification['decision'], classification['score'],
                                classification['version'], now(), row['doi']))
    for row in conn.execute('SELECT * FROM rejected_candidates').fetchall():
        paper = json.loads(row['paper_json'])
        classification = classify(paper)
        summary['rejected_checked'] += 1
        if classification['decision'] == 'reject':
            summary['still_rejected'] += 1
            if not dry_run:
                conn.execute('''UPDATE rejected_candidates SET score=?,reason=?,classifier_version=?
                    WHERE doi=?''', (classification['score'], classification['reason'],
                                    classification['version'], row['doi']))
        else:
            if not conn.execute('SELECT 1 FROM deleted_dois WHERE doi=?', (row['doi'],)).fetchone():
                summary['promoted'] += 1
                if not dry_run:
                    upsert_article(conn, paper, classification)
    if not dry_run:
        conn.commit()
    return summary


def set_user_state(conn, doi, user='local', acknowledged=None, hidden=None, rating='unchanged', note=None):
    if not conn.execute('SELECT 1 FROM articles WHERE doi=?', (doi,)).fetchone():
        raise ValueError('Paper not found')
    row = conn.execute('SELECT * FROM user_articles WHERE doi=? AND user_id=?', (doi, user)).fetchone()
    old = dict(row) if row else {'acknowledged': 0, 'hidden': 0, 'rating': None, 'note': ''}
    if rating != 'unchanged' and rating is not None and not (1 <= rating <= 5):
        raise ValueError('Rating must be between 1 and 5')
    conn.execute('''INSERT INTO user_articles(doi,user_id,acknowledged,hidden,rating,note,updated_at)
      VALUES(?,?,?,?,?,?,?) ON CONFLICT(doi,user_id) DO UPDATE SET
      acknowledged=excluded.acknowledged,hidden=excluded.hidden,rating=excluded.rating,
      note=excluded.note,updated_at=excluded.updated_at''', (
      doi, user, int(acknowledged) if acknowledged is not None else old['acknowledged'],
      int(hidden) if hidden is not None else old['hidden'],
      old['rating'] if rating == 'unchanged' else rating,
      old['note'] if note is None else note, now()))
    conn.commit()


def export_json(conn, path, include_private=False):
    records = []
    for row in conn.execute('SELECT * FROM articles ORDER BY published_date DESC, doi'):
        item = dict(row)
        item['authors'] = json.loads(item.pop('authors_json'))
        item['issns'] = json.loads(item.pop('issns_json'))
        if include_private:
            states = conn.execute('SELECT * FROM user_articles WHERE doi=?', (item['doi'],)).fetchall()
            item['user_states'] = [dict(x) for x in states]
        records.append(item)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'schema_version': 2, 'generated_at': now(), 'articles': records},
                                 indent=2, ensure_ascii=False), encoding='utf-8')
    return len(records)


def sample_rejected(conn, path, per_journal=20, seed='v1.3'):
    """Deterministic equal-per-journal sample; no implied precision/recall estimate."""
    groups = {}
    for row in conn.execute('SELECT * FROM rejected_candidates'):
        groups.setdefault(row['journal_id'], []).append(row)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ['doi', 'journal_id', 'title', 'abstract', 'published_date',
              'published_date_precision', 'abstract_status', 'reason',
              'classifier_version', 'human_relevance_label', 'human_notes']
    n = 0
    with target.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for journal_id in sorted(groups):
            rows = sorted(groups[journal_id], key=lambda r:
                hashlib.sha256((seed + ':' + r['doi']).encode('utf-8')).hexdigest())
            for row in rows[:per_journal]:
                paper = json.loads(row['paper_json'])
                writer.writerow({
                    'doi': row['doi'], 'journal_id': journal_id,
                    'title': paper.get('title'), 'abstract': paper.get('abstract'),
                    'published_date': paper.get('published_date'),
                    'published_date_precision': paper.get('published_date_precision'),
                    'abstract_status': paper.get('abstract_status'),
                    'reason': row['reason'], 'classifier_version': row['classifier_version'],
                    'human_relevance_label': '', 'human_notes': ''})
                n += 1
    return n


def quality_report(conn):
    articles = [dict(x) for x in conn.execute('SELECT * FROM articles')]
    return {
        'articles': len(articles),
        'decisions': dict(Counter(x['decision'] for x in articles)),
        'missing_abstracts': sum(not x['abstract'] for x in articles),
        'date_precision': dict(Counter(x['published_date_precision'] or 'unknown'
                                       for x in articles)),
        'rejected_candidates_archived': conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0],
        'manual_additions': sum(bool(x['manually_added']) for x in articles),
    }
