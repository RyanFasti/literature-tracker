"""Step 8: safe collector configuration, new branding and non-destructive migration."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from collector.configuration import apply_export, validate_export, valid_issn
from collector.site import refresh_public_site_assets
from collector.team import build_team_assets
from collector.topics import read_topic_keywords, suggest_topics

ROOT = Path(__file__).resolve().parent.parent


class Step8Tests(unittest.TestCase):
    def test_valid_issn_and_strict_config_import(self):
        self.assertTrue(valid_issn('2041-1723'))
        self.assertFalse(valid_issn('2041-1724'))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            payload={
                'schema_version':1,
                'journals':[{'id':'nature_communications','name':'Nature Communications',
                             'issns':['2041-1723'],'crossref_issns':['2041-1723']}],
                'topic_keywords':{k:[] for k in ('solar-cell','photodetector','led','film','stability','fundamentals','other-devices')},
                'collection':{'lookback_days':60,'backfill_years':3}}
            payload['topic_keywords']['solar-cell']=['photovoltaic','solar cell']
            source=root/'download.json';source.write_text(json.dumps(payload),encoding='utf-8')
            result=apply_export(source,root)
            self.assertEqual(result['journals'],1)
            self.assertEqual(json.loads((root/'config/collection.json').read_text())['lookback_days'],60)
            self.assertEqual(read_topic_keywords(root/'config/topic-keywords.json')['solar-cell'],['photovoltaic','solar cell'])
            self.assertEqual(suggest_topics('Novel solar cell',keywords=payload['topic_keywords']),['solar-cell'])
            prior=(root/'config/journals.json').read_bytes()
            payload['journals'][0]['issns']=['2041-1724']
            source.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError,'ISSN'):
                apply_export(source,root)
            self.assertEqual(prior,(root/'config/journals.json').read_bytes())
            payload['journals'][0]['issns']=['2041-1723']
            payload['private_note']='must not enter collector config'
            with self.assertRaises(ValueError):validate_export(payload)

    def test_non_destructive_d1_migration_and_type_constraints(self):
        conn=sqlite3.connect(':memory:')
        conn.execute('PRAGMA foreign_keys=ON')
        for name in ('0001_init.sql','0002_paper_overrides.sql'):
            conn.executescript((ROOT/'cloud/migrations'/name).read_text())
        conn.execute("INSERT INTO users(github_id,login) VALUES(1,'RyanFasti')")
        conn.execute("INSERT INTO annotations(github_id,doi,note,updated_at) VALUES(1,'10.1000/a','Private note','now')")
        conn.execute("INSERT INTO paper_overrides(github_id,doi,updated_at) VALUES(1,'10.1000/a','now')")
        conn.executescript((ROOT/'cloud/migrations/0003_workspace_settings.sql').read_text())
        self.assertEqual(conn.execute('SELECT note FROM annotations').fetchone()[0],'Private note')
        conn.execute("UPDATE paper_overrides SET article_kind='review' WHERE github_id=1")
        self.assertEqual(conn.execute('SELECT article_kind FROM paper_overrides').fetchone()[0],'review')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE paper_overrides SET article_kind='not-a-type' WHERE github_id=1")
        conn.execute("INSERT INTO workspace_settings(github_id,config_json,updated_at) VALUES(1,'{}','now')")
        self.assertEqual(conn.execute('SELECT count(*) FROM workspace_settings').fetchone()[0],1)
        conn.close()

    def test_public_catalog_preserved_and_new_logo_is_in_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);docs=root/'docs';docs.mkdir()
            payload={'schema_version':1,'generated_at':'2026-11-19T00:00:00Z','abstracts_included':False,
                     'articles':[{'doi':'10.1000/a','title':'Perovskite solar cell','decision':'include','authors':[]}]}
            catalog=docs/'articles.json';catalog.write_text(json.dumps(payload),encoding='utf-8')
            before=catalog.read_bytes();refresh_public_site_assets(docs)
            self.assertEqual(catalog.read_bytes(),before)
            self.assertTrue((docs/'logo.png').is_file())
            self.assertIn('logo.png',(docs/'index.html').read_text())
            result=build_team_assets(catalog,root/'cloud-public')
            self.assertEqual(result['static_files'],15)
            self.assertTrue((root/'cloud-public/team/workspace-config.mjs').is_file())
            self.assertTrue((root/'cloud-public/team/kinds.mjs').is_file())
            self.assertEqual(catalog.read_bytes(),before)

    def test_local_dashboard_uses_supplied_logo(self):
        self.assertIn('logo.png',(ROOT/'web/index.html').read_text())
        self.assertIn("'/logo.png'",(ROOT/'collector/web.py').read_text())


if __name__=='__main__':unittest.main()
