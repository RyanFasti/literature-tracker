"""Step 5: static asset privacy, D1 schema and optional deployment safety."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector.team import ROOT, build_team_assets
from collector.publication import read_public_catalog


class TeamDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        self.output = self.root / 'cloud' / 'public'
        for name in ('index.html', 'app.js', 'styles.css', 'logo.svg', 'logo.png', '.nojekyll'):
            (self.docs / name).write_text('public asset', encoding='utf-8')
        self.catalog = {'schema_version': 1, 'generated_at': '2026-11-18T00:00:00Z',
                        'abstracts_included': False, 'articles': [{
                            'doi': '10.1000/test', 'title': 'Halide perovskite',
                            'journal': 'ACS Energy Letters', 'decision': 'include', 'authors': []}]}
        (self.docs / 'articles.json').write_text(json.dumps(self.catalog), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_assets_are_only_public_metadata_and_static_ui(self):
        result = build_team_assets(self.docs / 'articles.json', self.output)
        self.assertEqual(result['static_files'], 15)
        self.assertEqual(result['public_articles'], 1)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ['.nojekyll', 'app.js', 'articles.json', 'index.html', 'logo.png', 'logo.svg', 'styles.css', 'team'])
        self.assertEqual(sorted(p.name for p in (self.output / 'team').iterdir()),
                         ['app.js', 'backup.mjs', 'categories.mjs', 'index.html', 'kinds.mjs', 'research.mjs', 'styles.css', 'workspace-config.mjs'])
        self.assertEqual(len(read_public_catalog(self.output / 'articles.json')['articles']), 1)
        self.assertNotIn('private', (self.output / 'articles.json').read_text(encoding='utf-8'))

    def test_rejects_unexpected_files_without_deleting_them(self):
        self.output.mkdir(parents=True)
        unexpected = self.output / 'private.sqlite3'
        unexpected.write_bytes(b'DO NOT DELETE')
        with self.assertRaisesRegex(ValueError, 'Unexpected file'):
            build_team_assets(self.docs / 'articles.json', self.output)
        self.assertEqual(unexpected.read_bytes(), b'DO NOT DELETE')

    def test_never_overwrites_catalog_source_directory(self):
        with self.assertRaisesRegex(ValueError, 'Asset output must not contain'):
            build_team_assets(self.docs / 'articles.json', self.docs)
        self.assertTrue((self.docs / 'articles.json').exists())

    def test_rejects_private_annotations_in_catalog(self):
        self.catalog['articles'][0]['note'] = 'Private scientific note'
        (self.docs / 'articles.json').write_text(json.dumps(self.catalog), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unsafe/invalid'):
            build_team_assets(self.docs / 'articles.json', self.output)
        self.assertFalse((self.output / 'articles.json').exists())

    def test_sql_migration_constraints_and_per_user_keys(self):
        migration = (ROOT / 'cloud/migrations/0001_init.sql').read_text(encoding='utf-8')
        conn = sqlite3.connect(':memory:')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(migration)
        conn.execute("INSERT INTO users(github_id,login) VALUES(1,'RyanFasti'),(2,'Colleague')")
        conn.execute("INSERT INTO annotations(github_id,doi,rating,note,updated_at) VALUES(1,'10.1000/a',5,'secret','now')")
        conn.execute("INSERT INTO annotations(github_id,doi,rating,note,updated_at) VALUES(2,'10.1000/a',3,'different','now')")
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM annotations').fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO annotations(github_id,doi,rating,updated_at) VALUES(1,'10.1000/b',6,'now')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO annotations(github_id,doi,rating,updated_at) VALUES(99,'10.1000/b',3,'now')")
        conn.close()

    def test_optional_cloudflare_deploy_does_not_change_pages_allowlist(self):
        workflow = (ROOT / '.github/workflows/weekly-public-catalog.yml').read_text(encoding='utf-8')
        self.assertIn('CLOUDFLARE_TEAM_DEPLOY_ENABLED', workflow)
        self.assertIn('CLOUDFLARE_API_TOKEN', workflow)
        self.assertIn('build-team-assets', workflow)
        self.assertIn('git add -- docs/index.html docs/app.js docs/styles.css docs/logo.svg docs/logo.png docs/articles.json docs/.nojekyll', workflow)
        self.assertNotIn('git add -- cloud/public', workflow)
        gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        for item in ('/cloud/public/', '/cloud/node_modules/', '/cloud/.dev.vars'):
            self.assertIn(item, gitignore)


if __name__ == '__main__':
    unittest.main()
