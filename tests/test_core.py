import tempfile
import unittest
from pathlib import Path
from collector.core import normalize_doi, normalize_crossref, classify
from collector.db import connect, upsert_article, set_user_state, export_json

class CoreTests(unittest.TestCase):
    def paper(self,doi='10.1234/test'):
        return {'doi':doi,'title':'MAPbI3 thin film devices','abstract':None,'abstract_status':'missing','journal':'Example','authors':[],'issns':[],'published_date':'2026-11-01','source':'crossref'}

    def test_doi(self):
        self.assertEqual(normalize_doi('https://doi.org/10.1234/ABC'), '10.1234/abc')
        with self.assertRaises(ValueError): normalize_doi('not-a-doi')

    def test_chemical_recognition(self):
        for title in ('MAPbI₃ photovoltaic devices','FAPbI3 films','halide perovskites in solar cells','CsPbBr3 LEDs'):
            self.assertEqual(classify({'title':title})['decision'],'include',title)
        self.assertEqual(classify({'title':'LiFePO4 battery cathodes'})['decision'],'reject')
        self.assertEqual(classify({'title':'BaTiO3 oxide perovskites'})['decision'],'reject')

    def test_normalization(self):
        p=normalize_crossref({'DOI':'10.1234/ABC','title':['<i>MAPbI3</i> films'], 'abstract':'<jats:p>Test &amp; results</jats:p>', 'published':{'date-parts':[[2026,10]]}})
        self.assertEqual(p['abstract'],'Test & results')
        self.assertEqual(p['published_date'],'2026-10')
        self.assertEqual(p['published_date_precision'],'month')

    def test_export_utf8_preserves_scientific_unicode(self):
        # U+2010 hyphen is not representable in the Windows cp1252 code page.
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / 'db.sqlite')
            try:
                paper = self.paper('10.1234/unicode')
                paper['title'] = 'MAPbI₃‐α phase – halide perovskites'
                paper['abstract'] = 'Photoluminescence at λ = 520 nm.'
                upsert_article(conn, paper, classify(paper))
                output = Path(d) / 'articles.json'
                self.assertEqual(export_json(conn, output), 1)
                raw = output.read_bytes()
                exported = __import__('json').loads(raw.decode('utf-8'))
                self.assertEqual(exported['articles'][0]['title'], paper['title'])
                self.assertEqual(exported['articles'][0]['abstract'], paper['abstract'])
                self.assertIn('MAPbI₃'.encode('utf-8'), raw)
            finally:
                conn.close()

    def test_persistence_and_tombstone(self):
        with tempfile.TemporaryDirectory() as d:
            conn = connect(Path(d) / 'db.sqlite')
            try:
                paper = self.paper()
                upsert_article(conn, paper, classify(paper))
                set_user_state(conn, paper['doi'], acknowledged=True, hidden=True,
                               rating=5, note='Important')
                updated = dict(paper, abstract='New abstract')
                upsert_article(conn, updated, classify(updated))
                row = conn.execute('SELECT * FROM user_articles').fetchone()
                self.assertEqual(
                    (row['acknowledged'], row['hidden'], row['rating'], row['note']),
                    (1, 1, 5, 'Important')
                )
                self.assertEqual(
                    conn.execute('SELECT abstract FROM articles').fetchone()[0],
                    'New abstract'
                )
                conn.execute('INSERT INTO deleted_dois VALUES (?, ?)',
                             (paper['doi'], 'now'))
                conn.execute('DELETE FROM articles WHERE doi=?', (paper['doi'],))
                conn.commit()
                self.assertFalse(upsert_article(conn, paper, classify(paper)))
                self.assertEqual(
                    conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0
                )
                self.assertTrue(upsert_article(conn, paper, classify(paper), manual=True))
                self.assertEqual(
                    conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1
                )
            finally:
                # Windows locks SQLite files until their connections are closed.
                # Close before TemporaryDirectory attempts to remove db.sqlite.
                conn.close()

if __name__=='__main__': unittest.main()
