"""Step 6 private backup and CI configuration safety tests."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector.db import connect, set_user_state, upsert_article
from collector.team_state import export_local_state, ROOT


class LocalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = connect(self.root / 'private.sqlite3')
        paper = {'doi': '10.1000/abc', 'title': 'Test study', 'journal': 'Other', 'authors': [], 'issns': []}
        cls = {'decision': 'include', 'score': 1.0, 'version': 'test'}
        upsert_article(self.conn, paper, cls, manual=True)
        set_user_state(self.conn, '10.1000/abc', rating=5, note='Sensitive research note', hidden=True)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_private_export_contains_only_local_state_and_manual_dois(self):
        path = self.root / 'private' / 'state.json'
        result = export_local_state(self.conn, path)
        self.assertEqual(result['annotations'], 1)
        self.assertEqual(result['manual_papers'], 1)
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data['format'], 'literature-tracker-user-state')
        self.assertEqual(data['annotations'][0]['rating'], 5)
        self.assertFalse(data['annotations'][0]['share_note'])
        self.assertEqual(data['manual_papers'][0]['title'], 'Test study')
        self.assertNotIn('abstract', path.read_text(encoding='utf-8'))
        self.assertNotIn('user_id', path.read_text(encoding='utf-8'))
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_backup_is_not_overwritten(self):
        path = self.root / 'backup.json'
        path.write_text('old private data', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            export_local_state(self.conn, path)
        self.assertEqual(path.read_text(encoding='utf-8'), 'old private data')

    def test_refuses_public_output(self):
        with self.assertRaisesRegex(ValueError, 'public or source'):
            export_local_state(self.conn, ROOT / 'docs' / 'private.json')
        with self.assertRaisesRegex(ValueError, 'public or source'):
            export_local_state(self.conn, ROOT / 'cloud' / 'public' / 'private.json')

    def test_reports_notes_too_long_for_cloud_without_truncation(self):
        set_user_state(self.conn, '10.1000/abc', note='x'*4001)
        path = self.root / 'long.json'
        result = export_local_state(self.conn, path)
        self.assertEqual(result['notes_exceeding_cloud_limit'], 1)
        self.assertEqual(len(json.loads(path.read_text())['annotations'][0]['note']), 4001)

    def test_team_assets_include_backup_module_without_private_data(self):
        from collector.team import build_team_assets
        catalog_dir = self.root / 'docs'
        catalog_dir.mkdir()
        for name in ('index.html', 'app.js', 'styles.css', 'logo.svg', 'logo.png', '.nojekyll'):
            (catalog_dir / name).write_text('public static file', encoding='utf-8')
        (catalog_dir / 'articles.json').write_text(json.dumps({
            'schema_version': 1, 'generated_at': '2026-11-18T00:00:00Z',
            'abstracts_included': False, 'articles': [{
                'doi': '10.1000/test', 'title': 'Test paper', 'journal': 'Other',
                'decision': 'include', 'authors': []}]}), encoding='utf-8')
        result = build_team_assets(catalog_dir / 'articles.json', self.root / 'output')
        self.assertEqual(result['static_files'], 15)
        self.assertTrue((self.root / 'output/team/backup.mjs').exists())
        html = (self.root / 'output/team/index.html').read_text(encoding='utf-8')
        self.assertIn('id="exportBackup"', html)
        self.assertIn('type="module"', html)
        self.assertNotIn('Sensitive research note',
                         (self.root / 'output/articles.json').read_text(encoding='utf-8'))

    def test_config_renderer_never_accepts_placeholder_values(self):
        from cloud.render_ci_config import render_config
        template = (ROOT / 'cloud' / 'wrangler.toml').read_text(encoding='utf-8')
        valid = {'CLOUDFLARE_D1_DATABASE_ID': 'e2819125-e804-4ee9-9a11-a484c3261f9d',
                 'CLOUDFLARE_TEAM_BASE_URL': 'https://literature-tracker-team.ryanfasti.workers.dev',
                 'CLOUDFLARE_ALLOWED_GITHUB_USERS': 'RyanFasti'}
        result = render_config(template, valid)
        self.assertIn('binding = "DB"', result)
        self.assertIn(valid['CLOUDFLARE_D1_DATABASE_ID'], result)
        self.assertIn(valid['CLOUDFLARE_TEAM_BASE_URL'], result)
        self.assertNotIn('REPLACE_WITH', result)
        self.assertNotIn('GITHUB_CLIENT_SECRET', result)
        with self.assertRaises(ValueError):
            render_config(template, {**valid, 'CLOUDFLARE_D1_DATABASE_ID': 'REPLACE_WITH_D1_DATABASE_ID'})
        with self.assertRaises(ValueError):
            render_config(template, {**valid, 'CLOUDFLARE_ALLOWED_GITHUB_USERS': 'RyanFasti\nMALICIOUS=1'})
        with self.assertRaisesRegex(ValueError, 'owner RyanFasti'):
            render_config(template, {**valid, 'CLOUDFLARE_ALLOWED_GITHUB_USERS': 'SomeoneElse'})


if __name__ == '__main__':
    unittest.main()
