"""Regression tests for EES metadata-redeposit omissions found in the coverage audit."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from collector.__main__ import collect
from collector.core import crossref_journal, normalize_crossref
from collector.db import connect, set_user_state, upsert_article
from collector.core import classify

START, END = '2026-10-19', '2026-11-18'
EES = {'id': 'ees', 'issns': ['1754-5692'], 'name': 'Energy & Environmental Science'}


def crossref_message(doi, title, created, deposited):
    return {
        'DOI': doi, 'title': [title],
        'container-title': ['Energy & Environmental Science'],
        'ISSN': ['1754-5692'],
        'published-online': {'date-parts': [[2026]]},
        'created': {'date-parts': [[int(x) for x in created.split('-')]]},
        'deposited': {'date-parts': [[int(x) for x in deposited.split('-')]]},
    }


MESSAGES = [
    crossref_message('10.1039/d6ee04175b',
                     'Degradation dynamics of lead-halide perovskites under combined heat and light',
                     '2026-09-17', '2026-10-20'),
    crossref_message('10.1039/d6ee02958b',
                     'Holistic design strategy of ferrocene-based interlayers for ultralow-noise perovskite photodetectors and high-efficiency solar cells',
                     '2026-09-22', '2026-10-20'),
    crossref_message('10.1039/d6ee05477c',
                     'Continuous optical–electrical field diagnostics separate environmental modulation from irreversible perovskite solar-cell failure',
                     '2026-10-06', '2026-11-03'),
]


class UpdatedDateRetrievalTests(unittest.TestCase):
    def test_updated_mode_uses_documented_crossref_filter(self):
        urls = []

        def fetch(url):
            urls.append(url)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['filter'],
                             ['from-update-date:2026-10-19,until-update-date:2026-11-18'])
            return {'message': {'items': [MESSAGES[0]]}}

        with patch('collector.core.get_json', side_effect=fetch):
            result = list(crossref_journal('1754-5692', START, END, mode='updated'))
        self.assertEqual(len(urls), 1)
        self.assertEqual(result[0]['doi'], '10.1039/d6ee04175b')
        self.assertEqual(result[0]['published_date'], '2026')
        self.assertEqual(result[0]['published_date_precision'], 'year')
        self.assertEqual(result[0]['metadata_deposited'], '2026-10-20')

    def test_unknown_mode_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'Unknown Crossref date mode'):
            list(crossref_journal('1754-5692', START, END, mode='not_a_mode'))

    def test_three_unseen_ees_records_are_retrieved_by_update_pass(self):
        modes = []

        def fetch(issn, start, end, mode='pub'):
            modes.append(mode)
            if mode == 'updated':
                return iter(normalize_crossref(m) for m in MESSAGES)
            return iter([])

        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'papers.sqlite3')
            try:
                with patch('collector.__main__.crossref_journal', side_effect=fetch):
                    with redirect_stdout(io.StringIO()):
                        result = collect(conn, [EES], START, END)[0]
                self.assertEqual(modes, ['pub', 'online', 'created', 'updated'])
                self.assertEqual(result['scanned'], 3)
                self.assertEqual(result['updated_only'], 3)
                self.assertEqual(result['created_only'], 0)
                self.assertEqual(result['include'], 2)
                self.assertEqual(result['review'], 1)
                self.assertIsNone(result['error'])
                rows = {r['doi']: r for r in conn.execute('SELECT * FROM articles')}
                self.assertEqual(set(rows), {m['DOI'] for m in MESSAGES})
                self.assertTrue(all(r['published_date'] == '2026' for r in rows.values()))
                self.assertTrue(all(r['published_date_precision'] == 'year' for r in rows.values()))
                self.assertTrue(all(r['manually_added'] == 0 for r in rows.values()))
                details = json.loads(conn.execute('SELECT details_json FROM runs').fetchone()[0])
                self.assertEqual(details['updated_only'], 3)
                self.assertEqual(details['source_counts']['1754-5692:updated'], 3)
            finally:
                conn.close()

    def test_dedup_preserves_user_state_and_deletion_tombstones(self):
        p = normalize_crossref(MESSAGES[0])
        removed = normalize_crossref(MESSAGES[1])
        modes = []

        def fetch(issn, start, end, mode='pub'):
            modes.append(mode)
            if mode in ('pub', 'updated'):
                return iter([p, removed])
            return iter([])

        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'papers.sqlite3')
            try:
                upsert_article(conn, p, classify(p))
                set_user_state(conn, p['doi'], acknowledged=True, hidden=True,
                               rating=5, note='Keep this annotation')
                conn.execute('INSERT INTO deleted_dois(doi,deleted_at) VALUES(?,?)',
                             (removed['doi'], '2026-11-18'))
                conn.commit()
                with patch('collector.__main__.crossref_journal', side_effect=fetch):
                    with redirect_stdout(io.StringIO()):
                        result = collect(conn, [EES], START, END)[0]
                self.assertEqual(modes, ['pub', 'online', 'created', 'updated'])
                self.assertEqual(result['scanned'], 2)
                self.assertEqual(result['updated_only'], 0)
                self.assertEqual(conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 0)
                state = conn.execute('SELECT * FROM user_articles WHERE doi=?', (p['doi'],)).fetchone()
                self.assertEqual((state['acknowledged'], state['hidden'], state['rating'], state['note']),
                                 (1, 1, 5, 'Keep this annotation'))
            finally:
                conn.close()

    def test_update_failure_is_not_reported_as_complete(self):
        def fetch(issn, start, end, mode='pub'):
            if mode == 'updated':
                raise RuntimeError('simulated API outage')
            return iter([normalize_crossref(MESSAGES[0])] if mode == 'pub' else [])

        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'papers.sqlite3')
            try:
                with patch('collector.__main__.crossref_journal', side_effect=fetch):
                    with redirect_stdout(io.StringIO()):
                        result = collect(conn, [EES], START, END)[0]
                self.assertEqual(result['scanned'], 1)
                self.assertIn('Partial source failure', result['error'])
                self.assertTrue(any('updated' in w for w in result['warnings']))
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
