"""Step 7: non-destructive D1 migration, static privacy, and new brand assets."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector.team import ROOT, build_team_assets
from collector.site import build_public_site
from collector.db import connect, upsert_article, set_user_state


class PersonalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_preserves_old_annotations_and_enforces_owner(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript((ROOT / 'cloud/migrations/0001_init.sql').read_text())
        conn.execute("INSERT INTO users(github_id,login) VALUES(1,'RyanFasti')")
        conn.execute("INSERT INTO annotations(github_id,doi,rating,note,updated_at) VALUES(1,'10.1000/a',4,'my note','now')")
        conn.executescript((ROOT / 'cloud/migrations/0002_paper_overrides.sql').read_text())
        self.assertEqual(conn.execute('SELECT rating,note FROM annotations').fetchone(), (4, 'my note'))
        conn.execute("INSERT INTO paper_overrides(github_id,doi,categories_json,updated_at) VALUES(1,'10.1000/a','[\"film\"]','now')")
        conn.execute("UPDATE paper_overrides SET deleted_at='2026-11-19' WHERE doi='10.1000/a'")
        self.assertEqual(conn.execute('SELECT deleted_at FROM paper_overrides').fetchone()[0], '2026-11-19')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO paper_overrides(github_id,doi,updated_at) VALUES(2,'10.1000/b','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO paper_overrides(github_id,doi,categories_json,updated_at) VALUES(1,'10.1000/b','not-json','now')")
        conn.close()

    def test_public_and_team_assets_include_svg_but_no_private_state(self):
        conn = connect(self.root / 'private.sqlite3')
        paper = {'doi':'10.1000/a','title':'Perovskite photodetectors',
                 'journal':'Nature Communications','authors':['Scientist'],
                 'abstract':'Private copyrighted abstract'}
        upsert_article(conn, paper, {'decision':'include','score':1.0,'version':'rules_v2'})
        set_user_state(conn, '10.1000/a', rating=5, note='Private experimental note')
        docs = self.root / 'docs'
        build_public_site(conn, docs)
        result = build_team_assets(docs / 'articles.json', self.root / 'cloud-public')
        self.assertEqual(result['static_files'], 15)
        self.assertTrue((docs / 'logo.svg').exists())
        self.assertTrue((self.root / 'cloud-public/logo.svg').exists())
        self.assertTrue((self.root / 'cloud-public/team/categories.mjs').exists())
        self.assertIn('#004c54', (docs / 'logo.svg').read_text())
        public_json = (self.root / 'cloud-public/articles.json').read_text()
        self.assertNotIn('Private experimental note', public_json)
        self.assertNotIn('Private copyrighted abstract', public_json)
        conn.close()

    def test_appearance_refresh_preserves_cumulative_catalog(self):
        from collector.site import refresh_public_site_assets
        docs = self.root / 'docs'
        docs.mkdir()
        original = {'schema_version':1, 'generated_at':'2026-11-19T00:00:00Z',
                    'abstracts_included':False,'articles':[{'doi':'10.1000/a',
                    'title':'Solar cell','decision':'include','authors':[]}]}
        path = docs / 'articles.json'
        path.write_text(json.dumps(original), encoding='utf-8')
        before = path.read_bytes()
        result = refresh_public_site_assets(docs)
        self.assertEqual(result['articles'],1)
        self.assertEqual(before,path.read_bytes())
        self.assertTrue((docs / 'logo.svg').exists())

    def test_scientific_topic_labels_are_explainable_and_public_safe(self):
        from collector.topics import suggest_topics
        self.assertEqual(suggest_topics('Stability of perovskite solar cells',
                                        'Spin-coated thin films show ion migration'),
                         ['solar-cell','film','stability'])
        self.assertEqual(suggest_topics('Generic chemistry study'), [])
        from collector.publication import read_public_catalog
        docs = self.root / 'docs'
        docs.mkdir()
        payload = {'schema_version':1,'generated_at':'2026-11-19T00:00:00Z',
                   'abstracts_included':False,'articles':[{'doi':'10.1000/a',
                   'title':'Solar cells','decision':'include','authors':[],
                   'topics':['solar-cell','film']}]}
        path = docs / 'articles.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.assertEqual(read_public_catalog(path)['articles'][0]['topics'],['solar-cell','film'])
        payload['articles'][0]['topics']=['invalid']
        path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Invalid topics'):
            read_public_catalog(path)

    def test_no_new_private_file_in_git_workflow(self):
        workflow = (ROOT / '.github/workflows/weekly-public-catalog.yml').read_text()
        self.assertIn('docs/logo.svg', workflow)
        self.assertIn('cp docs/index.html docs/app.js docs/styles.css docs/logo.svg', workflow)
        self.assertNotIn('git add data/', workflow)
        self.assertNotIn('git add -- cloud/public', workflow)


if __name__ == '__main__':
    unittest.main()
