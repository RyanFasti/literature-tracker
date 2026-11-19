import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collector.core import crossref_journal
from collector.__main__ import collect
from collector.db import connect


def example_message():
    return {'DOI': '10.1234/halide-test', 'title': ['MAPbI3 thin films'],
            'published-online': {'date-parts': [[2026, 11, 5]]}}


def example_paper():
    return {'doi': '10.1234/halide-test', 'title': 'MAPbI3 thin films',
            'abstract': None, 'abstract_status': 'missing', 'journal': 'Example',
            'published_date': '2026-11-05', 'authors': [], 'issns': [],
            'source': 'crossref'}


class RetrievalTests(unittest.TestCase):
    def test_404_journal_endpoint_falls_back_to_works_issn_filter(self):
        requested = []

        def fake_get_json(url):
            requested.append(url)
            if '/journals/' in url:
                raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
            self.assertIn('issn%3A1873-3212', url)
            return {'message': {'items': [example_message()]}}

        with patch('collector.core.get_json', side_effect=fake_get_json):
            papers = list(crossref_journal('1873-3212', '2026-11-01', '2026-11-18'))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]['doi'], '10.1234/halide-test')
        self.assertEqual(len(requested), 2)

    def test_empty_journal_endpoint_falls_back_to_works_filter(self):
        requested = []

        def fake_get_json(url):
            requested.append(url)
            if '/journals/' in url:
                return {'message': {'items': []}}
            return {'message': {'items': [example_message()]}}

        with patch('collector.core.get_json', side_effect=fake_get_json):
            papers = list(crossref_journal('1754-5706', '2026-11-01', '2026-11-18'))
        self.assertEqual(len(papers), 1)
        self.assertEqual(len(requested), 2)
        self.assertIn('issn%3A1754-5706', requested[1])

    def test_created_fallback_for_zero_publication_date_results(self):
        calls = []

        def fake_crossref(issn, start, end, mode='pub'):
            calls.append(mode)
            return iter([example_paper()] if mode == 'created' else [])

        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'db.sqlite')
            try:
                with patch('collector.__main__.crossref_journal', side_effect=fake_crossref):
                    with redirect_stdout(io.StringIO()):
                        result = collect(conn, [{'id': 'test', 'issns': ['1234-5678']}],
                                         '2026-11-01', '2026-11-18')[0]
                self.assertEqual(calls, ['pub', 'online', 'created', 'updated'])
                self.assertEqual(result['scanned'], 1)
                self.assertEqual(result['include'], 1)
                self.assertIsNone(result['error'])
                self.assertEqual(conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
            finally:
                conn.close()

    def test_zero_records_are_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'db.sqlite')
            try:
                with patch('collector.__main__.crossref_journal', return_value=iter([])):
                    with redirect_stdout(io.StringIO()):
                        result = collect(conn, [{'id': 'test', 'issns': ['1234-5678']}],
                                         '2026-11-01', '2026-11-18')[0]
                self.assertEqual(result['scanned'], 0)
                self.assertIn('Zero records', result['error'])
                run = conn.execute('SELECT * FROM runs').fetchone()
                self.assertIn('source_counts', json.loads(run['details_json']))
            finally:
                conn.close()

    def test_existing_run_table_is_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            raw = sqlite3.connect(path)
            raw.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, journal_id TEXT)')
            raw.execute("INSERT INTO runs(id,journal_id) VALUES(1,'old')")
            raw.commit()
            raw.close()
            conn = connect(path)
            try:
                self.assertEqual(conn.execute('SELECT journal_id FROM runs WHERE id=1').fetchone()[0], 'old')
                self.assertIn('details_json', [x[1] for x in conn.execute('PRAGMA table_info(runs)')])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
