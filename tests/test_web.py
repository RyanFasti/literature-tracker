"""Offline integration tests for the local SQLite-backed browser API."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from collector.core import classify
from collector.db import connect, set_user_state, upsert_article
from collector.web import create_server, query_articles


def paper(doi, title, decision=None):
    return {
        'doi': doi, 'title': title, 'abstract': 'Lead halide perovskite study' if decision not in ('reject', 'review') else None,
        'authors': ['Researcher One'], 'journal': 'Example Journal',
        'published_date': '2026-11', 'published_date_precision': 'month',
        'metadata_created': '2026-11-18', 'source': 'crossref',
    }


class WebQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'literature.sqlite3'
        self.conn = connect(self.db)
        self.included = paper('10.1234/included', 'Lead halide perovskite photovoltaics')
        self.review = paper('10.1234/review', 'Ferroelectric perovskite materials', 'review')
        self.rejected = paper('10.1234/rejected', 'Solid oxide electrolysis perovskite cathodes', 'reject')
        for p in (self.included, self.review, self.rejected):
            upsert_article(self.conn, p, classify(p))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()  # Windows requires closing SQLite before cleanup.

    def test_inbox_excludes_rejected_but_all_retains_it(self):
        inbox = query_articles(self.conn, {'view': ['inbox']})
        self.assertEqual(inbox['total'], 2)
        self.assertEqual(query_articles(self.conn, {'view': ['all']})['total'], 3)
        self.assertEqual(query_articles(self.conn, {'view': ['review']})['total'], 1)
        self.assertEqual(query_articles(self.conn, {'view': ['all'], 'decision': ['reject']})['total'], 1)

    def test_rating_notes_hidden_and_acknowledged_are_independent(self):
        set_user_state(self.conn, self.included['doi'], rating=4, note='Important',
                       acknowledged=True, hidden=True)
        self.assertEqual(query_articles(self.conn, {'view': ['inbox']})['total'], 1)
        self.assertEqual(query_articles(self.conn, {'view': ['hidden']})['total'], 1)
        self.assertEqual(query_articles(self.conn, {'view': ['rated']})['total'], 1)
        item = query_articles(self.conn, {'view': ['rated']})['items'][0]
        self.assertEqual((item['rating'], item['note'], item['acknowledged'], item['hidden']),
                         (4, 'Important', 1, 1))

    def test_search_and_pagination(self):
        data = query_articles(self.conn, {'view': ['all'], 'search': ['photovoltaics']})
        self.assertEqual(data['total'], 1)
        data = query_articles(self.conn, {'view': ['all'], 'limit': ['1'], 'offset': ['1']})
        self.assertEqual((data['total'], len(data['items'])), (3, 1))
        self.assertEqual(query_articles(self.conn, {'view': ['all'], 'search': ['%']})['total'], 0)

    def test_invalid_sort_and_filter_rejected(self):
        with self.assertRaises(ValueError):
            query_articles(self.conn, {'sort': ['a.doi; DROP TABLE articles']})
        with self.assertRaises(ValueError):
            query_articles(self.conn, {'view': ['unrecognized']})


class WebHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'literature.sqlite3'
        conn = connect(self.db)
        try:
            p = paper('10.1234/existing', 'Perovskite solar cells')
            upsert_article(conn, p, classify(p))
        finally:
            conn.close()
        self.server = create_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, method='GET', data=None, headers=None):
        body = json.dumps(data).encode('utf-8') if data is not None else None
        h = {'Content-Type': 'application/json'} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base+path, data=body, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=5) as response:
            return response, response.read()

    def test_html_and_api_serve(self):
        response, raw = self.request('/')
        self.assertIn(b'Literature Tracker', raw)
        self.assertIn(b'app.js', raw)
        response, raw = self.request('/api/articles')
        result = json.loads(raw)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['doi'], '10.1234/existing')
        response, raw = self.request('/app.js')
        self.assertIn(b'function renderDetail', raw)

    def test_patch_state_persists_across_connections(self):
        path = '/api/state'
        self.request(path, 'PATCH', {'doi':'10.1234/existing','acknowledged':True,
                                      'hidden':True,'rating':5,'note':'Mn-doped samples'})
        response, raw = self.request('/api/article?doi=10.1234%2Fexisting')
        result = json.loads(raw)
        self.assertEqual((result['acknowledged'], result['hidden'], result['rating'], result['note']),
                         (1, 1, 5, 'Mn-doped samples'))
        self.request(path, 'PATCH', {'doi':'10.1234/existing','rating':None})
        response, raw = self.request('/api/article?doi=10.1234%2Fexisting')
        self.assertIsNone(json.loads(raw)['rating'])

    def test_manual_doi_add_bypasses_classifier(self):
        unrelated = paper('10.1234/unrelated', 'Organic helical polymer photodetector', 'reject')
        with patch('collector.web.crossref_doi', return_value=unrelated):
            self.request('/api/add-doi', 'POST', {'doi':unrelated['doi']})
        response, raw = self.request('/api/article?doi=10.1234%2Funrelated')
        result = json.loads(raw)
        self.assertEqual((result['decision'], result['manually_added']), ('include', 1))

    def test_delete_creates_tombstone_and_removes_state(self):
        self.request('/api/state', 'PATCH', {'doi':'10.1234/existing','note':'private'})
        self.request('/api/delete', 'POST', {'doi':'10.1234/existing','confirm':True})
        response, raw = self.request('/api/articles?view=all')
        self.assertEqual(json.loads(raw)['total'], 0)
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM deleted_dois').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM user_articles').fetchone()[0], 0)
        finally:
            conn.close()

    def test_sqlite_backup_includes_private_notes(self):
        self.request('/api/state','PATCH',{'doi':'10.1234/existing','note':'Private backup'})
        response, raw = self.request('/api/backup')
        self.assertTrue(raw.startswith(b'SQLite format 3'))
        destination = Path(self.tmp.name)/'download.sqlite3'
        destination.write_bytes(raw)
        conn = sqlite3.connect(destination)
        try:
            self.assertEqual(conn.execute('SELECT note FROM user_articles').fetchone()[0],
                             'Private backup')
        finally:
            conn.close()

    def test_cross_origin_and_non_json_mutations_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request('/api/delete','POST',{'doi':'10.1234/existing','confirm':True},
                         {'Origin':'https://attacker.example'})
        self.assertEqual(cm.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request('/api/state','PATCH',{'doi':'10.1234/existing','rating':True})
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request('/api/delete','POST',{'doi':'10.1234/existing','confirm':False})
        self.assertEqual(cm.exception.code,400)


if __name__ == '__main__':
    unittest.main()
