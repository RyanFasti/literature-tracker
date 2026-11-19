"""Command-line interface: python -m collector --help"""
import argparse
import json
import sys
from datetime import date, timedelta

from .core import classify, crossref_doi, crossref_journal, load_journals, normalize_doi
from .db import (connect, export_json, now, quality_report, reclassify_database,
                 record_rejected, sample_rejected, set_user_state, upsert_article)
from .openalex import enrich_missing_abstracts

DEFAULT_DB = 'data/literature.sqlite3'


def collect(conn, journals, start, end):
    results = []
    for j in journals:
        count = {'journal': j['id'], 'scanned': 0, 'include': 0, 'review': 0,
                 'reject': 0, 'error': None, 'warnings': [], 'source_counts': {},
                 'created_only': 0, 'updated_only': 0,
                 'imprecise_publication_dates': 0}
        started = now()
        seen = set()
        retrieval_issns = j.get('crossref_issns', j['issns'])

        def collect_mode(mode):
            for issn in retrieval_issns:
                key = f'{issn}:{mode}'
                count['source_counts'][key] = 0
                try:
                    for paper in crossref_journal(issn, start, end, mode=mode):
                        count['source_counts'][key] += 1
                        if paper['doi'] in seen:
                            continue
                        seen.add(paper['doi'])
                        count['scanned'] += 1
                        if mode == 'created':
                            count['created_only'] += 1
                        elif mode == 'updated':
                            count['updated_only'] += 1
                        if paper.get('published_date_precision') not in ('day',):
                            count['imprecise_publication_dates'] += 1
                        classification = classify(paper)
                        decision = classification['decision']
                        count[decision] += 1
                        if decision != 'reject' or conn.execute(
                            'SELECT 1 FROM articles WHERE doi=?', (paper['doi'],)).fetchone():
                            upsert_article(conn, paper, classification)
                        else:
                            record_rejected(conn, paper, classification, j['id'])
                except Exception as exc:
                    count['warnings'].append(f'{key}: {type(exc).__name__}: {exc}')

        collect_mode('pub')
        collect_mode('online')
        # Initial deposits may have year-only or future publication dates.
        collect_mode('created')
        # Publisher redeposits can occur after initial creation. This pass is
        # essential for EES records with year-only publication dates whose
        # creation date predates the collection window. Deduplicate by DOI.
        collect_mode('updated')
        if count['scanned'] == 0:
            count['error'] = 'Zero records retrieved: journal coverage needs investigation'
        elif count['warnings']:
            count['error'] = 'Partial source failure; inspect warnings and rerun'
        conn.execute('''INSERT INTO runs(journal_id,started_at,ended_at,scanned,
          included,review,rejected,error,details_json) VALUES(?,?,?,?,?,?,?,?,?)''',
          (j['id'], started, now(), count['scanned'], count['include'],
           count['review'], count['reject'], count['error'],
           json.dumps({'source_counts': count['source_counts'],
                       'warnings': count['warnings'],
                       'created_only': count['created_only'],
                       'updated_only': count['updated_only'],
                       'imprecise_publication_dates': count['imprecise_publication_dates'],
                       'window_start': start, 'window_end': end})))
        conn.commit()
        results.append(count)
        print(json.dumps(count))
    return results


def refresh_existing_metadata(conn, limit=200, all_articles=False, fetch=crossref_doi):
    """Repair legacy dates by DOI without rescanning entire journal back catalogs."""
    if not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    sql = 'SELECT doi,manually_added FROM articles'
    if not all_articles:
        sql += ' WHERE published_date_precision IS NULL'
    sql += ' ORDER BY added_at,doi LIMIT ?'
    summary = {'checked': 0, 'refreshed': 0, 'errors': [], 'remaining_unknown_precision': None}
    for row in conn.execute(sql, (limit,)).fetchall():
        try:
            paper = fetch(row['doi'])
            if normalize_doi(paper['doi']) != row['doi']:
                raise ValueError('Crossref DOI mismatch; metadata ignored')
            upsert_article(conn, paper, classify(paper), manual=bool(row['manually_added']))
            summary['checked'] += 1
            summary['refreshed'] += 1
        except Exception as exc:
            summary['errors'].append({'doi': row['doi'], 'error': f'{type(exc).__name__}: {exc}'})
            # Avoid repeated requests after rate limits or network failures.
            break
    summary['remaining_unknown_precision'] = conn.execute(
        'SELECT count(*) FROM articles WHERE published_date_precision IS NULL').fetchone()[0]
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description='Local-first scientific literature collector')
    p.add_argument('--db', default=DEFAULT_DB, help='SQLite database path')
    sub = p.add_subparsers(dest='command', required=True)
    update = sub.add_parser('update', help='Collect recent papers for all or one journal')
    update.add_argument('--journal', default='all')
    update.add_argument('--days', type=int, default=30)
    update.add_argument('--start', help='YYYY-MM-DD')
    update.add_argument('--end', help='YYYY-MM-DD')
    back = sub.add_parser('backfill', help='Backfill one journal in monthly batches')
    back.add_argument('--journal', required=True)
    back.add_argument('--years', type=int, default=3)
    add = sub.add_parser('add-doi', help='Manually add a DOI, regardless of journal/filter')
    add.add_argument('doi')
    rm = sub.add_parser('delete', help='Delete a paper and block automatic reimport')
    rm.add_argument('doi')
    state = sub.add_parser('state', help='Set local per-user paper state')
    state.add_argument('doi')
    state.add_argument('--acknowledged', choices=['yes', 'no'])
    state.add_argument('--hidden', choices=['yes', 'no'])
    state.add_argument('--rating', type=int)
    state.add_argument('--clear-rating', action='store_true')
    state.add_argument('--note')
    exp = sub.add_parser('export', help='Export retained articles as UTF-8 JSON')
    exp.add_argument('--output', default='data/published/articles.json')
    exp.add_argument('--include-private', action='store_true',
                     help='Export notes; NEVER publish this file')
    audit = sub.add_parser('audit', help='Show recent collection run statistics')
    audit.add_argument('--journal')
    sub.add_parser('quality', help='Summarize abstract/date quality and archived rejects')
    reclass = sub.add_parser('reclassify', help='Re-score existing and archived papers without deleting any')
    reclass.add_argument('--dry-run', action='store_true')
    refresh = sub.add_parser('refresh-metadata', help='Repair legacy publication dates via Crossref DOI lookups')
    refresh.add_argument('--limit', type=int, default=200)
    refresh.add_argument('--all', action='store_true', help='Refresh all records, not just unknown date precision')
    enrich = sub.add_parser('enrich', help='Fetch missing abstracts from OpenAlex by DOI')
    enrich.add_argument('--limit', type=int, default=60)
    enrich.add_argument('--retry', action='store_true',
                        help='Retry DOI lookups already checked but still missing abstracts')
    browser = sub.add_parser('serve', help='Run the local browser dashboard (SQLite-backed)')
    browser.add_argument('--port', type=int, default=8765)
    browser.add_argument('--no-browser', action='store_true', help='Do not open a browser automatically')
    sample = sub.add_parser('audit-rejected', help='Sample archived rejects for manual recall auditing')
    sample.add_argument('--per-journal', type=int, default=20)
    sample.add_argument('--seed', default='v1.3')
    sample.add_argument('--output', default='data/audits/rejected_sample.csv')
    coverage = sub.add_parser('audit-coverage', help='Compare journal DOI inventories against OpenAlex (read-only)')
    coverage.add_argument('--journal', default='all')
    coverage.add_argument('--days', type=int, default=30)
    coverage.add_argument('--start', help='YYYY-MM-DD')
    coverage.add_argument('--end', help='YYYY-MM-DD')
    coverage.add_argument('--output', default='data/audits/coverage')
    investigate = sub.add_parser('investigate-unseen', help='Diagnose OpenAlex-unseen DOIs using Crossref (read-only)')
    investigate.add_argument('--csv', default='data/audits/coverage/openalex_unseen.csv')
    investigate.add_argument('--output', default='data/audits/coverage/unseen_investigation.csv')
    investigate.add_argument('--start', required=True, help='Audit window YYYY-MM-DD')
    investigate.add_argument('--end', required=True, help='Audit window YYYY-MM-DD')
    publisher = sub.add_parser('compare-publisher', help='Compare a user-supplied publisher DOI CSV (read-only)')
    publisher.add_argument('--journal', required=True)
    publisher.add_argument('--csv', required=True)
    publisher.add_argument('--output', default='data/audits/publisher_comparison.csv')
    public = sub.add_parser('build-site', help='Build a public-safe static journal catalog (no private state)')
    public.add_argument('--output', default='docs')
    public.add_argument('--include-abstracts', action='store_true',
                        help='Only if you have verified redistribution rights for ALL included abstracts')
    check_public = sub.add_parser('check-public', help='Fail closed on unsafe public catalog JSON')
    check_public.add_argument('--path', default='docs/articles.json')
    check_public.add_argument('--min-articles', type=int, default=0)
    merge_public = sub.add_parser('merge-site', help='Merge new papers into persistent public catalog')
    merge_public.add_argument('--previous', required=True)
    merge_public.add_argument('--incoming', required=True)
    merge_public.add_argument('--output', required=True)
    merge_public.add_argument('--exclusions', default='config/public-exclusions.json')
    args = p.parse_args(argv)
    if args.command == 'check-public':
        from .publication import read_public_catalog
        result = read_public_catalog(args.path)
        if len(result['articles']) < args.min_articles:
            p.error('Public catalog has fewer papers than --min-articles')
        print(json.dumps({'status': 'safe_public_metadata', 'articles': len(result['articles'])}))
        return
    if args.command == 'merge-site':
        from .publication import merge_public_catalog
        print(json.dumps(merge_public_catalog(args.previous, args.incoming,
                                              args.output, args.exclusions)))
        return
    if args.command == 'serve':
        if not 1 <= args.port <= 65535:
            p.error('--port must be between 1 and 65535')
        from .web import serve
        serve(args.db, port=args.port, open_browser=not args.no_browser)
        return
    journals = load_journals()
    if args.command in ('update', 'backfill', 'audit-coverage') and args.journal != 'all' and args.journal not in journals:
        p.error(f'Unknown journal {args.journal!r}; choose from {", ".join(journals)}')
    if args.command == 'compare-publisher' and args.journal not in journals:
        p.error('Unknown journal: ' + args.journal)
    conn = connect(args.db)
    try:
        if args.command == 'update':
            if args.days < 1:
                p.error('--days must be positive')
            end = date.fromisoformat(args.end) if args.end else date.today()
            start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
            if start > end:
                p.error('start must not exceed end')
            selected = list(journals.values()) if args.journal == 'all' else [journals[args.journal]]
            results = collect(conn, selected, start.isoformat(), end.isoformat())
            if any(x['error'] for x in results):
                sys.exit(1)
        elif args.command == 'backfill':
            if args.years < 1:
                p.error('--years must be positive')
            j = journals[args.journal]
            end = date.today()
            start = end - timedelta(days=round(args.years * 365.25))
            failures = 0
            while start <= end:
                chunk_end = min(start + timedelta(days=29), end)
                result = collect(conn, [j], start.isoformat(), chunk_end.isoformat())
                failures += int(bool(result[0]['error']))
                start = chunk_end + timedelta(days=1)
            if failures:
                sys.exit(1)
        elif args.command == 'add-doi':
            doi = normalize_doi(args.doi)
            paper = crossref_doi(doi)
            upsert_article(conn, paper, classify(paper), manual=True)
            print('Added:', doi)
        elif args.command == 'delete':
            doi = normalize_doi(args.doi)
            conn.execute('INSERT OR REPLACE INTO deleted_dois(doi,deleted_at) VALUES(?,?)',
                         (doi, now()))
            conn.execute('DELETE FROM articles WHERE doi=?', (doi,))
            conn.execute('DELETE FROM rejected_candidates WHERE doi=?', (doi,))
            conn.commit()
            print('Deleted and blocked from automatic reimport:', doi)
        elif args.command == 'state':
            doi = normalize_doi(args.doi)
            rating = None if args.clear_rating else args.rating if args.rating is not None else 'unchanged'
            set_user_state(conn, doi,
                           acknowledged=None if args.acknowledged is None else args.acknowledged == 'yes',
                           hidden=None if args.hidden is None else args.hidden == 'yes',
                           rating=rating, note=args.note)
            print('Updated:', doi)
        elif args.command == 'export':
            print('Exported', export_json(conn, args.output, args.include_private),
                  'articles to', args.output)
        elif args.command == 'audit':
            sql = 'SELECT * FROM runs'
            params = []
            if args.journal:
                sql += ' WHERE journal_id=?'
                params.append(args.journal)
            sql += ' ORDER BY id DESC LIMIT 30'
            for row in conn.execute(sql, params):
                print(json.dumps(dict(row)))
        elif args.command == 'quality':
            print(json.dumps(quality_report(conn), indent=2))
        elif args.command == 'reclassify':
            print(json.dumps(reclassify_database(conn, classify, dry_run=args.dry_run), indent=2))
        elif args.command == 'refresh-metadata':
            if not 1 <= args.limit <= 500:
                p.error('--limit must be between 1 and 500')
            result = refresh_existing_metadata(conn, args.limit, args.all)
            print(json.dumps(result, indent=2))
            if result['errors']:
                sys.exit(1)
        elif args.command == 'enrich':
            if not 1 <= args.limit <= 500:
                p.error('--limit must be between 1 and 500')
            result = enrich_missing_abstracts(conn, limit=args.limit, retry=args.retry)
            print(json.dumps(result, indent=2))
            if result['errors']:
                sys.exit(1)
        elif args.command == 'audit-coverage':
            if args.days < 1:
                p.error('--days must be positive')
            end = date.fromisoformat(args.end) if args.end else date.today()
            start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
            if start > end:
                p.error('start must not exceed end')
            from .coverage import audit_openalex
            selected = list(journals.values()) if args.journal == 'all' else [journals[args.journal]]
            report = audit_openalex(conn, selected, start.isoformat(), end.isoformat(), args.output)
            for result in report['journals']:
                print(json.dumps(result, ensure_ascii=False))
            print('Saved audit to', args.output)
            if any(r['status'] == 'error' for r in report['journals']):
                sys.exit(1)
        elif args.command == 'investigate-unseen':
            from .coverage import investigate_unseen
            result = investigate_unseen(conn, args.csv, args.output, journals, args.start, args.end)
            print(json.dumps(result, ensure_ascii=False))
            if result['errors']:
                sys.exit(1)
        elif args.command == 'compare-publisher':
            from .coverage import compare_publisher_list
            result = compare_publisher_list(conn, journals[args.journal], args.csv, args.output)
            print(json.dumps(result, ensure_ascii=False))
            print('Saved comparison to', args.output)
        elif args.command == 'build-site':
            from .site import build_public_site
            result = build_public_site(conn, args.output, include_abstracts=args.include_abstracts)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == 'audit-rejected':
            if not 1 <= args.per_journal <= 1000:
                p.error('--per-journal must be between 1 and 1000')
            n = sample_rejected(conn, args.output, args.per_journal, args.seed)
            print(f'Sampled {n} archived rejects to {args.output}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
