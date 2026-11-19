import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.coverage import (audit_openalex, compare_publisher_list,
                                iter_source_works, resolve_sources)
from collector.db import connect, record_rejected, set_user_state, upsert_article
from collector.site import build_public_site

JOURNAL = {'id': 'example_journal', 'name': 'Example Journal', 'issns': ['1234-5678', '2345-6789']}
START, END = '2026-11-01', '2026-11-30'


def work(doi, source='S123', date='2026-11-12'):
    return {'doi': 'https://doi.org/' + doi if doi else None,
            'id': 'https://openalex.org/W' + str(sum(ord(c) for c in (doi or 'none'))), 'title': 'A sample paper',
            'publication_date': date, 'type': 'article',
            'primary_location': {'source': {'id': 'https://openalex.org/' + source}}}


def paper(doi, title='Halide perovskite LEDs', abstract='Abstract with α-PbI₃'):
    return {'doi': doi, 'title': title, 'abstract': abstract,
            'journal': 'Example Journal', 'authors': ['A Researcher'],
            'published_date': '2026-11-12', 'published_date_precision': 'day',
            'issns': JOURNAL['issns'], 'source': 'crossref'}


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / 'papers.sqlite3')
        self.cls = {'decision': 'include', 'score': .9, 'version': 'rules_v2', 'reason': 'test'}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_source_resolution_uses_issns_and_deduplicates(self):
        calls = []
        def fetch(path, params):
            calls.append(params['filter'])
            return {'results': [{'id': 'https://openalex.org/S123',
                                 'issn': ['1234-5678', '2345-6789']},
                                {'id': 'https://openalex.org/S999', 'issn': ['9999-9999']}]}
        self.assertEqual(resolve_sources(JOURNAL, fetch=fetch), ['S123'])
        self.assertEqual(calls, ['issn:1234-5678', 'issn:2345-6789'])

    def test_source_resolution_fails_when_none(self):
        with self.assertRaisesRegex(ValueError, 'No matching'):
            resolve_sources(JOURNAL, fetch=lambda *args: {'results': []})

    def test_cursor_pages_all_results(self):
        calls = []
        def fetch(path, params):
            calls.append(params['cursor'])
            if params['cursor'] == '*':
                return {'results': [work('10.1000/a')],
                        'meta': {'count': 2, 'next_cursor': 'next'}}
            return {'results': [work('10.1000/b')],
                    'meta': {'count': 2, 'next_cursor': 'end'}}
        self.assertEqual(len(list(iter_source_works('S123', START, END, fetch=fetch, paging='cursor'))), 2)
        self.assertEqual(calls, ['*', 'next'])

    def test_cursor_truncation_fails_closed(self):
        def fetch(path, params):
            return {'results': [work('10.1000/a')], 'meta': {'count': 2}}
        with self.assertRaisesRegex(RuntimeError, 'cursor missing'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='cursor'))

    def test_repeated_cursor_fails_closed(self):
        def fetch(path, params):
            return {'results': [work('10.1000/a')],
                    'meta': {'count': 3, 'next_cursor': '*'}}
        with self.assertRaisesRegex(RuntimeError, 'Repeated'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='cursor'))

    def test_inventory_statuses_and_no_sqlite_changes(self):
        upsert_article(self.conn, paper('10.1000/retained'), self.cls)
        record_rejected(self.conn, paper('10.1000/rejected'),
                        {'decision': 'reject', 'score': 0, 'reason': 'test', 'version': 'rules_v2'},
                        JOURNAL['id'])
        self.conn.execute("INSERT INTO deleted_dois VALUES('10.1000/deleted','2026-11-12')")
        self.conn.commit()
        def works(source_id, start, end):
            for doi in ('10.1000/retained', '10.1000/rejected', '10.1000/deleted', '10.1000/unseen'):
                yield work(doi)
            yield work(None)
        report = audit_openalex(self.conn, [JOURNAL], START, END, Path(self.tmp.name) / 'audit',
                                source_fetch=lambda j: ['S123'], works_fetch=works)
        summary = report['journals'][0]
        self.assertEqual(summary['status'], 'completed_index_comparison')
        self.assertEqual(summary['openalex_dois'], 4)
        self.assertEqual(summary['no_doi'], 1)
        for name in ('retained', 'archived_reject', 'explicitly_deleted', 'not_in_local_inventory'):
            self.assertEqual(summary[name], 1)
        with (Path(self.tmp.name) / 'audit/openalex_unseen.csv').open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r['doi'] for r in rows], ['10.1000/unseen'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)

    def test_failure_discards_partial_journal_results(self):
        def broken(source_id, start, end):
            yield work('10.1000/unseen')
            raise RuntimeError('Network disconnected')
        report = audit_openalex(self.conn, [JOURNAL], START, END, Path(self.tmp.name) / 'audit',
                                source_fetch=lambda j: ['S123'], works_fetch=broken)
        self.assertEqual(report['journals'][0]['status'], 'error')
        self.assertIsNone(report['journals'][0]['openalex_dois'])
        with (Path(self.tmp.name) / 'audit/openalex_unseen.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(len(list(csv.DictReader(f))), 0)

    def test_out_of_window_result_fails_closed(self):
        report = audit_openalex(self.conn, [JOURNAL], START, END, Path(self.tmp.name) / 'audit',
                                source_fetch=lambda j: ['S123'],
                                works_fetch=lambda *args: [work('10.1000/a', date='2026-10-31')])
        self.assertEqual(report['journals'][0]['status'], 'error')

    def test_wrong_primary_source_fails_closed(self):
        report = audit_openalex(self.conn, [JOURNAL], START, END, Path(self.tmp.name) / 'audit',
                                source_fetch=lambda j: ['S123'],
                                works_fetch=lambda *args: [work('10.1000/a', source='S999')])
        self.assertEqual(report['journals'][0]['status'], 'error')

    def test_publisher_csv_comparison_and_duplicates(self):
        upsert_article(self.conn, paper('10.1000/retained'), self.cls)
        source = Path(self.tmp.name) / 'publisher.csv'
        source.write_text('DOI,Title,Published_Date\n10.1000/retained,One,2026-11-01\n'
                          '10.1000/unseen,Two,2026-11-02\n10.1000/unseen,Two,2026-11-02\n',
                          encoding='utf-8')
        result = compare_publisher_list(self.conn, JOURNAL, source,
                                        Path(self.tmp.name) / 'comparison.csv')
        self.assertEqual(result['input_dois'], 2)
        self.assertEqual(result['not_in_local_inventory'], 1)

    def test_publisher_csv_rejects_invalid_doi(self):
        source = Path(self.tmp.name) / 'bad.csv'
        source.write_text('doi\nnot a doi\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'line 2'):
            compare_publisher_list(self.conn, JOURNAL, source, Path(self.tmp.name) / 'x.csv')

    def test_static_site_has_no_private_data_and_abstracts_opt_in(self):
        upsert_article(self.conn, paper('10.1000/retained'), self.cls)
        set_user_state(self.conn, '10.1000/retained', acknowledged=True, hidden=True,
                       rating=5, note='SECRET PRIVATE EXPERIMENT')
        record_rejected(self.conn, paper('10.1000/rejected'),
                        {'decision': 'reject', 'score': 0, 'reason': 'test', 'version': 'rules_v2'},
                        JOURNAL['id'])
        output = Path(self.tmp.name) / 'docs'
        result = build_public_site(self.conn, output)
        raw = (output / 'articles.json').read_text(encoding='utf-8')
        data = json.loads(raw)
        self.assertEqual(result['public_articles'], 1)
        self.assertNotIn('SECRET PRIVATE', raw)
        self.assertNotIn('user_articles', raw)
        self.assertNotIn('rating', raw)
        self.assertNotIn('hidden', raw)
        self.assertNotIn('abstract', data['articles'][0])
        self.assertNotIn('10.1000/rejected', raw)
        self.assertTrue((output / 'index.html').is_file())
        build_public_site(self.conn, output, include_abstracts=True)
        data = json.loads((output / 'articles.json').read_text(encoding='utf-8'))
        self.assertIn('α-PbI₃', data['articles'][0]['abstract'])


if __name__ == '__main__':
    unittest.main()
