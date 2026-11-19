"""Regression tests for keyless OpenAlex paging and read-only DOI diagnostics."""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.coverage import (iter_source_works, investigate_unseen,
                                _publication_window_relation, resolve_sources)
from collector.db import connect

JOURNAL = {'id': 'ees', 'name': 'Energy & Environmental Science',
           'issns': ['1754-5692', '1754-5706']}
START, END = '2026-10-19', '2026-11-18'


def work(i):
    return {'id': f'https://openalex.org/W{i}',
            'doi': f'https://doi.org/10.1039/d6ee{i:05d}b',
            'publication_date': '2026-11-01',
            'primary_location': {'source': {'id': 'https://openalex.org/S123'}}}


class KeylessPagingTests(unittest.TestCase):
    def test_keyless_uses_page_numbers_and_100_per_page(self):
        calls = []
        def fetch(path, params):
            self.assertEqual(params['per-page'], 100)
            self.assertNotIn('cursor', params)
            page = params['page']
            calls.append(page)
            items = list(range((page-1)*100, min(page*100, 231)))
            return {'results': [work(i) for i in items], 'meta': {'count': 231}}
        with patch.dict(os.environ, {'OPENALEX_API_KEY': ''}):
            found = list(iter_source_works('S123', START, END, fetch=fetch))
        self.assertEqual(len(found), 231)
        self.assertEqual(calls, [1, 2, 3])

    def test_key_uses_cursor_and_100_per_page(self):
        calls = []
        def fetch(path, params):
            self.assertEqual(params['per-page'], 100)
            self.assertNotIn('page', params)
            calls.append(params['cursor'])
            if params['cursor'] == '*':
                return {'results': [work(1)], 'meta': {'count': 2, 'next_cursor': 'abc'}}
            return {'results': [work(2)], 'meta': {'count': 2, 'next_cursor': None}}
        with patch.dict(os.environ, {'OPENALEX_API_KEY': 'test-key'}):
            found = list(iter_source_works('S123', START, END, fetch=fetch))
        self.assertEqual(len(found), 2)
        self.assertEqual(calls, ['*', 'abc'])

    def test_page_mode_rejects_over_10000(self):
        def fetch(path, params):
            return {'results': [work(1)], 'meta': {'count': 10001}}
        with self.assertRaisesRegex(RuntimeError, '10,000'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='page'))

    def test_page_mode_fails_on_truncation(self):
        def fetch(path, params):
            return {'results': [work(1)], 'meta': {'count': 101}}
        with self.assertRaisesRegex(RuntimeError, 'short'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='page'))

    def test_page_mode_fails_on_changed_total(self):
        def fetch(path, params):
            return {'results': [work(i) for i in range(100)],
                    'meta': {'count': 101}} if params['page'] == 1 else {
                    'results': [work(101)], 'meta': {'count': 102}}
        with self.assertRaisesRegex(RuntimeError, 'count changed'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='page'))

    def test_page_mode_detects_duplicate_work_from_shifting_pages(self):
        def fetch(path, params):
            if params['page'] == 1:
                return {'results': [work(i) for i in range(100)], 'meta': {'count': 101}}
            return {'results': [work(99)], 'meta': {'count': 101}}
        with self.assertRaisesRegex(RuntimeError, 'repeated a work'):
            list(iter_source_works('S123', START, END, fetch=fetch, paging='page'))

    def test_sources_use_documented_page_size(self):
        def fetch(path, params):
            self.assertEqual(params['per-page'], 100)
            return {'results': [{'id': 'https://openalex.org/S123',
                                 'issn': ['1754-5692', '1754-5706']}]}
        self.assertEqual(resolve_sources(JOURNAL, fetch=fetch), ['S123'])


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = connect(self.root / 'db.sqlite3')
        self.input = self.root / 'unseen.csv'
        self.output = self.root / 'investigation.csv'
        self.input.write_text('journal_id,doi,title,openalex_publication_date\n'
                              'ees,10.1039/d6ee04175b,Relevant halide paper,2026-10-20\n',
                              encoding='utf-8')

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_diagnostic_is_read_only_and_reports_date_and_issn(self):
        def fetch(doi):
            return {'doi': doi, 'title': 'Degradation dynamics of lead-halide perovskites',
                    'abstract': 'Halide perovskite degradation', 'journal': JOURNAL['name'],
                    'issns': ['1754-5692'], 'published_date': '2026-10-20',
                    'published_date_precision': 'day', 'publication_date_source': 'published-online',
                    'metadata_created': '2026-11-25', 'metadata_deposited': '2026-11-25'}
        result = investigate_unseen(self.conn, self.input, self.output,
                                    {'ees': JOURNAL}, START, END, fetch=fetch)
        self.assertEqual(result['investigated'], 1)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['inside_publication_window'], 1)
        with self.output.open(encoding='utf-8-sig', newline='') as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row['crossref_issn_match'], 'yes')
        self.assertEqual(row['crossref_date_vs_window'], 'inside')
        self.assertEqual(row['crossref_created'], '2026-11-25')
        self.assertEqual(row['classifier_decision'], 'include')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rejected_candidates').fetchone()[0], 0)

    def test_mismatched_doi_is_flagged_not_written(self):
        result = investigate_unseen(self.conn, self.input, self.output,
                                    {'ees': JOURNAL}, START, END,
                                    fetch=lambda doi: {'doi': '10.1039/another'})
        self.assertEqual(result['errors'], 1)
        with self.output.open(encoding='utf-8-sig', newline='') as handle:
            row = next(csv.DictReader(handle))
        self.assertIn('mismatch', row['error'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 0)

    def test_date_precision_is_conservative(self):
        self.assertEqual(_publication_window_relation('2026-10', 'month', START, END),
                         'overlaps_window_imprecise')
        self.assertEqual(_publication_window_relation('2026-11', 'month', START, END),
                         'overlaps_window_imprecise')
        self.assertEqual(_publication_window_relation('2026-09', 'month', START, END),
                         'outside')
        self.assertEqual(_publication_window_relation('2026', 'year', START, END),
                         'overlaps_window_imprecise')
        self.assertEqual(_publication_window_relation(None, None, START, END), 'unknown')


if __name__ == '__main__':
    unittest.main()
