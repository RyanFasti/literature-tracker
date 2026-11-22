"""Step 9: abstract-first ingestion, DOI verification, CEJ recovery and safe CI retry."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collector.abstracts import (AbstractResolver, clean_abstract, from_openalex,
                                 from_semantic_scholar, from_europe_pmc,
                                 from_elsevier, parse_elsevier_xml)
from collector.abstract_workflow import repair_abstracts
from collector.__main__ import collect
from collector.core import classify, normalize_crossref
from collector.db import (connect, get_known_abstract, quality_report,
                          record_pending_abstract, record_rejected,
                          set_user_state, upsert_article)
from collector.pending_index import export_pending, import_pending
from collector.site import build_public_site

CEJ = {'id': 'chemical_engineering_journal', 'name': 'Chemical Engineering Journal',
       'issns': ['1385-8947'], 'crossref_issns': ['1385-8947']}
ABSTRACT = ('We investigate lead halide perovskite photovoltaic devices under realistic '
            'operating conditions and identify the dominant charge-transport mechanisms '
            'using optical and electrical spectroscopy.')


def paper(doi='10.1016/j.cej.2026.180540', title='Perovskite/PbS tandem solar cells', abstract=None):
    return {'doi': doi, 'title': title, 'abstract': abstract, 'journal': CEJ['name'],
            'authors': ['A. Researcher'], 'issns': CEJ['issns'],
            'published_date': '2026-11', 'published_date_precision': 'month',
            'source': 'crossref', 'abstract_source': 'crossref' if abstract else None}


class ResolverTests(unittest.TestCase):
    def test_jats_punctuation_and_heading(self):
        raw = ('<jats:abstract><jats:title>Abstract</jats:title>'
               '<jats:p>We test (FAPbI<jats:sub>3</jats:sub>) - based cells '
               'at 25 °C; efficiency is 21.4%.</jats:p></jats:abstract>')
        text = clean_abstract(raw)
        self.assertEqual(text, 'We test (FAPbI3) - based cells at 25 °C; efficiency is 21.4%.')
        self.assertNotIn('Abstract', text)

    def test_crossref_abstract_normalized(self):
        p = normalize_crossref({'DOI': '10.1016/j.cej.2026.180540',
                                'title': ['Test'],
                                'abstract': '<jats:p>Abstract: An (MAPbI<jats:sub>3</jats:sub>) device.</jats:p>'})
        self.assertEqual(p['abstract'], 'An (MAPbI3) device.')

    def test_openalex_verifies_doi_and_reconstructs(self):
        work = {'doi': 'https://doi.org/10.1016/j.cej.2026.180540',
                'abstract_inverted_index': {'This': [0], 'study': [1], 'works.': [2]}}
        with patch('collector.abstracts._json_get', return_value=work):
            self.assertEqual(from_openalex(paper()), 'This study works.')
        work['doi'] = 'https://doi.org/10.1000/wrong'
        with patch('collector.abstracts._json_get', return_value=work):
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                from_openalex(paper())

    def test_semantic_scholar_verifies_external_doi(self):
        with patch('collector.abstracts._json_get', return_value={
            'externalIds': {'DOI': paper()['doi']}, 'abstract': ABSTRACT}):
            self.assertEqual(from_semantic_scholar(paper()), ABSTRACT)
        with patch('collector.abstracts._json_get', return_value={'abstract': ABSTRACT}):
            with self.assertRaisesRegex(ValueError, 'mismatched'):
                from_semantic_scholar(paper())

    def test_europe_pmc_selects_matching_doi_only(self):
        result = {'resultList': {'result': [
            {'doi': '10.1000/other', 'abstractText': 'WRONG'},
            {'doi': paper()['doi'], 'abstractText': ABSTRACT}]}}
        with patch('collector.abstracts._json_get', return_value=result):
            self.assertEqual(from_europe_pmc(paper()), ABSTRACT)

    def test_elsevier_xml_extracts_abstract_not_title(self):
        payload = (b'<root xmlns:ce="http://www.elsevier.com/xml/common/dtd" '
                   b'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
                   b'<prism:doi>10.1016/j.cej.2026.180540</prism:doi>'
                   b'<ce:abstract><ce:section-title>Abstract</ce:section-title>'
                   b'<ce:para>' + ABSTRACT.encode() + b'</ce:para></ce:abstract></root>')
        result = parse_elsevier_xml(payload, paper()['doi'])
        self.assertIn('charge-transport', result)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            parse_elsevier_xml(payload, '10.1016/j.cej.2026.wrong')

    def test_elsevier_description_fallback_requires_substantive_text(self):
        payload = (b'<root xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   b'<dc:description>' + (ABSTRACT * 2).encode() + b'</dc:description></root>')
        self.assertIn('charge-transport', parse_elsevier_xml(payload, paper()['doi']))
        short = b'<root><description>Short teaser.</description></root>'
        self.assertIsNone(parse_elsevier_xml(short, paper()['doi']))

    def test_elsevier_only_uses_configured_key(self):
        with patch.dict(os.environ, {'ELSEVIER_API_KEY': 'PRIVATE_KEY'}, clear=False):
            with patch('collector.abstracts._text_get', return_value=None) as get:
                self.assertIsNone(from_elsevier(paper()))
                self.assertEqual(get.call_args.args[1]['X-ELS-APIKey'], 'PRIVATE_KEY')
                self.assertIn('META_ABS', get.call_args.args[0])

    def test_fallback_and_provider_errors_not_silent(self):
        def bad(_):
            raise urllib.error.HTTPError('https://api.example', 429, 'rate limit', {}, None)
        resolver = AbstractResolver({'openalex': bad, 'semantic_scholar': lambda _: ABSTRACT})
        out = resolver.resolve(paper())
        self.assertEqual(out['status'], 'available')
        self.assertEqual(out['source'], 'semantic_scholar')
        self.assertEqual(out['attempts'][0]['status'], 'http_429')
        self.assertIn('openalex', resolver.blocked)
        self.assertEqual(resolver.resolve(paper())['attempts'][0]['status'], 'skipped_rate_limit')

    def test_missing_not_misrepresented_as_success(self):
        resolver = AbstractResolver({'openalex': lambda _: None})
        out = resolver.resolve(paper())
        self.assertEqual(out['status'], 'not_found')
        self.assertIsNone(out['abstract'])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / 'papers.sqlite3')
        self.journals = {CEJ['id']: CEJ}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cejs_missing_abstract_are_pending_not_rejected(self):
        def crossref(issn, start, end, mode='pub'):
            return iter([paper()] if mode == 'pub' else [])
        resolver = AbstractResolver({'openalex': lambda _: None})
        with patch('collector.__main__.crossref_journal', side_effect=crossref):
            with redirect_stdout(io.StringIO()):
                report = collect(self.conn, [CEJ], '2026-11-01', '2026-11-18',
                                 max_abstract_lookups=1, resolver=resolver)[0]
        self.assertEqual(report['abstract_pending'], 1)
        self.assertEqual(report['abstract_remote_lookups'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0)

    def test_titleless_crossref_doi_with_abstract_is_pending_not_published(self):
        incomplete = paper(doi='10.1016/j.cej.2026.199998', title='')
        def crossref(issn, start, end, mode='pub'):
            return iter([incomplete] if mode == 'pub' else [])
        resolver = AbstractResolver({'synthetic': lambda _: ABSTRACT})
        with patch('collector.__main__.crossref_journal', side_effect=crossref):
            with redirect_stdout(io.StringIO()):
                report = collect(self.conn, [CEJ], '2026-11-01', '2026-11-18',
                                 max_abstract_lookups=1, resolver=resolver)[0]
        self.assertEqual(report['abstract_resolved'], 1)
        self.assertEqual(report['metadata_pending'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0)
        target = Path(self.tmp.name) / 'pending-titleless.json'
        self.assertEqual(export_pending(self.conn, target)['missing_titles'], 1)

    def test_cejs_classified_only_after_abstract(self):
        unrelated = paper('10.1016/j.cej.2026.999991', 'Battery recycling processes')
        relevant = paper('10.1016/j.cej.2026.999992', 'Transport mechanisms in thin films')
        def crossref(issn, start, end, mode='pub'):
            return iter([unrelated, relevant] if mode == 'pub' else [])
        def fetch(p):
            if p['doi'].endswith('999991'):
                return 'This paper examines lithium-ion battery recycling processes and metal recovery, without any photovoltaic applications. '
            return ABSTRACT
        resolver = AbstractResolver({'synthetic': fetch})
        with patch('collector.__main__.crossref_journal', side_effect=crossref):
            with redirect_stdout(io.StringIO()):
                report = collect(self.conn, [CEJ], '2026-11-01', '2026-11-18',
                                 max_abstract_lookups=2, resolver=resolver)[0]
        self.assertEqual(report['abstract_resolved'], 2)
        self.assertEqual(report['include'], 1)
        self.assertEqual(report['reject'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 0)

    def test_repair_legacy_cejs_preserves_user_annotations(self):
        p = paper()
        upsert_article(self.conn, p, classify(p))
        set_user_state(self.conn, p['doi'], acknowledged=True, rating=4, note='Private note')
        resolver = AbstractResolver({'synthetic': lambda _: ABSTRACT})
        report = repair_abstracts(self.conn, self.journals, CEJ['id'], limit=5,
                                  sources=('articles',), resolver=resolver)
        self.assertEqual(report['available'], 1)
        self.assertEqual(get_known_abstract(self.conn, p['doi'])[0], ABSTRACT)
        self.assertEqual(self.conn.execute('SELECT note FROM user_articles').fetchone()[0], 'Private note')
        self.assertEqual(self.conn.execute('SELECT rating FROM user_articles').fetchone()[0], 4)
        self.assertEqual(self.conn.execute('SELECT abstract_source FROM articles').fetchone()[0], 'synthetic')

    def test_archived_cejs_without_abstract_can_be_promoted(self):
        p = paper(title='Perovskite tandem solar cells')
        record_rejected(self.conn, p, {'decision': 'reject', 'score': 0,
                                       'reason': 'legacy title-only', 'version': 'rules_v2'}, CEJ['id'])
        resolver = AbstractResolver({'synthetic': lambda _: ABSTRACT})
        report = repair_abstracts(self.conn, self.journals, CEJ['id'], limit=5,
                                  sources=('archived',), resolver=resolver)
        self.assertEqual(report['promoted'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)

    def test_repair_dry_run_has_no_database_writes(self):
        p = paper()
        record_pending_abstract(self.conn, p, CEJ['id'])
        resolver = AbstractResolver({'synthetic': lambda _: ABSTRACT})
        report = repair_abstracts(self.conn, self.journals, CEJ['id'], limit=1,
                                  sources=('pending',), resolver=resolver, dry_run=True)
        self.assertEqual(report['available'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM abstract_checks').fetchone()[0], 0)

    def test_repair_does_not_publish_titleless_record(self):
        p = paper(doi='10.1016/j.cej.2026.199997', title='')
        record_pending_abstract(self.conn, p, CEJ['id'])
        resolver = AbstractResolver({'synthetic': lambda _: ABSTRACT})
        report = repair_abstracts(self.conn, self.journals, CEJ['id'], limit=1,
                                  sources=('pending',), resolver=resolver)
        self.assertEqual(report['available'], 1)
        self.assertEqual(report['metadata_pending'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 1)

    def test_public_catalog_skips_legacy_blank_title_without_deleting_it(self):
        p = paper(doi='10.1016/j.cej.2026.199996', title='', abstract=ABSTRACT)
        upsert_article(self.conn, p, {'decision': 'include', 'score': 4,
                                     'version': 'rules_v2'})
        result = build_public_site(self.conn, Path(self.tmp.name) / 'public')
        self.assertEqual(result['public_articles'], 0)
        self.assertEqual(result['titleless_excluded'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)

    def test_public_pending_index_excludes_abstracts_and_private_state(self):
        p = paper()
        p['secret'] = 'PRIVATE NOTE'
        p['abstract'] = 'Not allowed to export'
        record_pending_abstract(self.conn, p, CEJ['id'])
        target = Path(self.tmp.name) / 'pending.json'
        export_pending(self.conn, target)
        raw = target.read_text()
        self.assertNotIn('PRIVATE NOTE', raw)
        self.assertNotIn('Not allowed to export', raw)
        self.assertNotIn('abstract', raw)
        other = connect(Path(self.tmp.name) / 'other.sqlite3')
        try:
            result = import_pending(other, target)
            self.assertEqual(result['imported'], 1)
            self.assertEqual(other.execute('SELECT count(*) FROM pending_abstracts').fetchone()[0], 1)
        finally:
            other.close()

    def test_pending_export_preserves_crossref_doi_with_empty_title(self):
        # Crossref occasionally has a DOI but no deposited title. Such a
        # record must remain retryable, not fail the entire weekly workflow.
        p = normalize_crossref({'DOI': '10.1016/j.cej.2026.199999',
                                'title': [], 'container-title': [CEJ['name']]})
        self.assertEqual(p['title'], '')
        record_pending_abstract(self.conn, p, CEJ['id'])
        target = Path(self.tmp.name) / 'titleless-pending.json'
        result = export_pending(self.conn, target)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['missing_titles'], 1)
        payload = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(payload['papers'][0]['paper']['title'], '')
        self.assertEqual(payload['papers'][0]['paper']['doi'], p['doi'])
        other = connect(Path(self.tmp.name) / 'titleless-import.sqlite3')
        try:
            self.assertEqual(import_pending(other, target)['imported'], 1)
            self.assertEqual(other.execute('SELECT doi FROM pending_abstracts').fetchone()[0], p['doi'])
        finally:
            other.close()

    def test_pending_export_rejects_non_string_title(self):
        p = paper()
        p['title'] = None
        record_pending_abstract(self.conn, p, CEJ['id'])
        target = Path(self.tmp.name) / 'invalid-title.json'
        with self.assertRaisesRegex(ValueError, 'Invalid pending paper title'):
            export_pending(self.conn, target)
        self.assertFalse(target.exists())

    def test_public_pending_index_rejects_private_field_injection(self):
        target = Path(self.tmp.name) / 'unsafe.json'
        target.write_text(json.dumps({'schema_version': 1, 'generated_at': 'now',
                                       'papers': [{'journal_id': CEJ['id'], 'paper': {
                                           'doi': paper()['doi'], 'title': 'Test', 'note': 'SECRET'}}]}))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            import_pending(self.conn, target)

    def test_quality_reports_pending(self):
        record_pending_abstract(self.conn, paper(), CEJ['id'])
        self.assertEqual(quality_report(self.conn)['pending_abstracts'], 1)


if __name__ == '__main__':
    unittest.main()
