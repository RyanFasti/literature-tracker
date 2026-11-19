"""Step 4: public catalog persistence, privacy and scheduled deployment safety."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from collector.db import connect, set_user_state, upsert_article
from collector.publication import (merge_public_catalog, read_public_catalog,
                                   read_public_exclusions)
from collector.site import build_public_site


def article(doi='10.1000/a', decision='include', **kwargs):
    item = {'doi': doi, 'title': 'Halide perovskite research', 'journal': 'ACS Energy Letters',
            'published_date': '2026-11', 'published_date_precision': 'month',
            'published_online': None, 'type': 'journal-article',
            'decision': decision, 'abstract_status': 'missing',
            'first_seen_at': '2026-11-18T01:00:00+00:00', 'manually_added': 0,
            'authors': ['A. Scientist']}
    item.update(kwargs)
    return item


def catalog(*papers):
    return {'schema_version': 1, 'generated_at': '2026-11-18T01:00:00+00:00',
            'abstracts_included': False, 'articles': list(papers)}


class PublicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.previous = self.dir / 'previous.json'
        self.incoming = self.dir / 'incoming.json'
        self.output = self.dir / 'output.json'
        self.exclusions = self.dir / 'exclusions.json'
        self.exclusions.write_text('{"dois": []}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, previous, incoming):
        self.previous.write_text(json.dumps(previous), encoding='utf-8')
        self.incoming.write_text(json.dumps(incoming), encoding='utf-8')

    def test_union_never_drops_prior_paper(self):
        self.write(catalog(article()), catalog())
        result = merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(result['public_articles'], 1)
        self.assertEqual(read_public_catalog(self.output)['articles'][0]['doi'], '10.1000/a')

    def test_new_doi_added_without_duplicates(self):
        self.write(catalog(article()), catalog(article('10.1000/b'), article()))
        result = merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(result['public_articles'], 2)
        self.assertEqual(result['new_dois'], 1)
        self.assertTrue(all('manually_added' not in p for p in read_public_catalog(self.output)['articles']))

    def test_prior_include_never_demoted_and_first_seen_preserved(self):
        self.write(catalog(article(first_seen_at='2024-01-01T00:00:00+00:00')),
                   catalog(article(decision='review', first_seen_at='2026-11-18T00:00:00+00:00')))
        merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        result = read_public_catalog(self.output)['articles'][0]
        self.assertEqual(result['decision'], 'include')
        self.assertEqual(result['first_seen_at'], '2024-01-01T00:00:00+00:00')

    def test_review_can_be_promoted(self):
        self.write(catalog(article(decision='review')), catalog(article(decision='include')))
        merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(read_public_catalog(self.output)['articles'][0]['decision'], 'include')

    def test_date_precision_never_regresses(self):
        self.write(catalog(article(published_date='2026-11-18', published_date_precision='day')),
                   catalog(article(published_date='2026', published_date_precision='year')))
        merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(read_public_catalog(self.output)['articles'][0]['published_date'], '2026-11-18')

    def test_explicit_exclusion_is_sticky(self):
        self.write(catalog(article()), catalog(article(), article('10.1000/b')))
        self.exclusions.write_text('{"dois": ["https://doi.org/10.1000/a"]}', encoding='utf-8')
        result = merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(result['public_articles'], 1)
        self.assertEqual(read_public_catalog(self.output)['articles'][0]['doi'], '10.1000/b')

    def test_refuses_abstract_and_private_notes(self):
        for field in ('abstract', 'user_states', 'note', 'rating', 'hidden', 'acknowledged'):
            with self.subTest(field=field):
                bad = article(**{field: 'sensitive'})
                self.previous.write_text(json.dumps(catalog(bad)), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Unsafe/invalid'):
                    read_public_catalog(self.previous)
        bad = catalog(article())
        bad['abstracts_included'] = True
        self.previous.write_text(json.dumps(bad), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Refusing to publish abstracts'):
            read_public_catalog(self.previous)

    def test_refuses_duplicate_and_invalid_doi(self):
        for papers in ((article(), article()), (article('invalid'),)):
            self.previous.write_text(json.dumps(catalog(*papers)), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_public_catalog(self.previous)

    def test_missing_previous_fails_closed_and_does_not_overwrite(self):
        self.incoming.write_text(json.dumps(catalog(article())), encoding='utf-8')
        self.output.write_text('DO NOT OVERWRITE', encoding='utf-8')
        with self.assertRaises(FileNotFoundError):
            merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(self.output.read_text(encoding='utf-8'), 'DO NOT OVERWRITE')

    def test_invalid_incoming_fails_without_touching_existing_output(self):
        self.write(catalog(article()), catalog(article(abstract='private')))
        self.output.write_text('EXISTING', encoding='utf-8')
        with self.assertRaises(ValueError):
            merge_public_catalog(self.previous, self.incoming, self.output, self.exclusions)
        self.assertEqual(self.output.read_text(encoding='utf-8'), 'EXISTING')

    def test_rejects_bad_exclusions(self):
        self.exclusions.write_text('{"dois": ["not-a-doi"]}', encoding='utf-8')
        with self.assertRaises(ValueError):
            read_public_exclusions(self.exclusions)

    def test_public_site_with_private_state_passes_validation(self):
        conn = connect(self.dir / 'private.sqlite3')
        paper = {'doi': '10.1000/a', 'title': 'Halide perovskite paper',
                 'journal': 'ACS Energy Letters', 'authors': ['A. Scientist'],
                 'abstract': 'Copyrighted abstract', 'published_date': '2026-11',
                 'published_date_precision': 'month'}
        upsert_article(conn, paper, {'decision': 'include', 'score': 0.9, 'version': 'rules_v2'})
        set_user_state(conn, '10.1000/a', rating=5, note='Private experiment notes', hidden=True)
        build_public_site(conn, self.dir / 'docs')
        read_public_catalog(self.dir / 'docs/articles.json')
        content = (self.dir / 'docs/articles.json').read_text(encoding='utf-8')
        for private in ('Private experiment notes', 'Copyrighted abstract', 'user_states'):
            self.assertNotIn(private, content)
        conn.close()

    def test_workflow_does_not_publish_sqlite_and_has_fail_closed_checks(self):
        workflow = Path(__file__).resolve().parent.parent / '.github/workflows/weekly-public-catalog.yml'
        text = workflow.read_text(encoding='utf-8')
        for required in ('schedule:', 'workflow_dispatch:', 'update --days 45',
                         'check-public --path docs/articles.json',
                         'git add -- docs/index.html docs/app.js docs/styles.css docs/articles.json docs/.nojekyll',
                         'path: public_deploy', 'actions/deploy-pages@v4'):
            self.assertIn(required, text)
        self.assertNotIn('git add .\n', text)
        self.assertNotIn('git add data/', text)


if __name__ == '__main__':
    unittest.main()
