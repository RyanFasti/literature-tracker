import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collector.core import classify, normalize_crossref, parse_crossref_date
from collector.__main__ import collect, refresh_existing_metadata
from collector.db import (connect, enrich_abstract, export_json, quality_report,
                          reclassify_database, record_rejected, sample_rejected,
                          set_user_state, upsert_article)
from collector.openalex import reconstruct_abstract, enrich_missing_abstracts, openalex_doi


def paper(doi='10.1234/example', title='Halide perovskite LEDs', abstract=None):
    return {'doi': doi, 'title': title, 'abstract': abstract,
            'abstract_status': 'available' if abstract else 'missing',
            'journal': 'Energy &amp; Environmental Science', 'authors': ['A Author'],
            'issns': ['1754-5692'], 'published_date': '2026',
            'published_date_precision': 'year', 'publication_date_source': 'published',
            'metadata_created': '2026-11-18', 'source': 'crossref'}


class ScientificClassificationTests(unittest.TestCase):
    def test_known_halide_photovoltaics_and_excitons(self):
        cases = [
            'MAPbI₃ photovoltaic devices',
            'FAPbI3 thin films',
            'CsPbBr3 LEDs',
            'Cs2AgBiBr6 double perovskite scintillator',
            'Cs2HfCl6 double perovskite',
            'Lead bromide perovskite single crystals',
            'Constructing radiative dipolar excitons in 2D perovskites',
            'Spin-polarized lasing in quasi-2D perovskites',
            'Perovskite/silicon tandem solar cells',
            'Perovskite quantum dot memristors',
            'Addressing the 1000-h stability challenge in perovskite LEDs',
            'Chiral 2D perovskite (S/R-NEA)2PbBr4 piezoelectric sensors',
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(classify({'title': title})['decision'], 'include')

    def test_metal_oxide_contact_does_not_turn_perovskite_led_into_oxide(self):
        p = paper(title='Stable perovskite LEDs',
                  abstract='Metal oxide contacts improve carrier injection in perovskite LEDs.')
        self.assertEqual(classify(p)['decision'], 'include')

    def test_oxide_and_nonperovskite_rejected(self):
        for title in (
            'BaTiO3 oxide perovskites',
            'Solid oxide fuel cells with perovskite cathodes',
            'Oxygen-vacancy-engineered perovskite catalysts for CO2 electrolysis',
            'Organic helical polymers for photodetection',
            'LiFePO4 battery cathodes',
        ):
            with self.subTest(title=title):
                self.assertEqual(classify({'title': title})['decision'], 'reject')

    def test_unqualified_perovskite_goes_to_review(self):
        self.assertEqual(classify({'title': 'Ferroelectric perovskite single crystals'})['decision'], 'review')
        self.assertNotIn('probability', classify({'title': 'Halide perovskites'})['reason'])


class DateAndMigrationTests(unittest.TestCase):
    def test_year_and_month_are_not_invented_days(self):
        self.assertEqual(parse_crossref_date({'published': {'date-parts': [[2026]]}}, 'published'),
                         ('2026', 'year'))
        self.assertEqual(parse_crossref_date({'published': {'date-parts': [[2026, 11]]}}, 'published'),
                         ('2026-11', 'month'))
        self.assertEqual(parse_crossref_date({'published': {'date-parts': [[2026, 11, 18]]}}, 'published'),
                         ('2026-11-18', 'day'))
        self.assertEqual(parse_crossref_date({'published': {'date-parts': [[2026, 2, 30]]}}, 'published'),
                         (None, None))

    def test_created_is_not_a_publication_date(self):
        m = {'DOI': '10.1234/test', 'title': ['Perovskite LEDs'],
             'container-title': ['Energy &amp; Environmental Science'],
             'created': {'date-parts': [[2026, 11, 18]]},
             'published': {'date-parts': [[2026]]}}
        normalized = normalize_crossref(m)
        self.assertEqual(normalized['published_date'], '2026')
        self.assertEqual(normalized['published_date_precision'], 'year')
        self.assertEqual(normalized['metadata_created'], '2026-11-18')
        self.assertEqual(normalized['journal'], 'Energy & Environmental Science')
        m.pop('published')
        normalized = normalize_crossref(m)
        self.assertIsNone(normalized['published_date'])
        self.assertEqual(normalized['metadata_created'], '2026-11-18')

    def test_migrate_old_sqlite_preserves_annotations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.sqlite'
            raw = sqlite3.connect(path)
            raw.execute('''CREATE TABLE articles (doi TEXT PRIMARY KEY,title TEXT NOT NULL,
                abstract TEXT,abstract_status TEXT NOT NULL,journal TEXT,published_date TEXT,
                authors_json TEXT NOT NULL,issns_json TEXT NOT NULL,type TEXT,url TEXT,source TEXT,
                decision TEXT NOT NULL,score REAL NOT NULL,classifier_version TEXT NOT NULL,
                added_at TEXT NOT NULL,updated_at TEXT NOT NULL,manually_added INTEGER NOT NULL)''')
            raw.execute('''CREATE TABLE user_articles (doi TEXT NOT NULL,user_id TEXT NOT NULL,
                acknowledged INTEGER,hidden INTEGER,rating INTEGER,note TEXT,updated_at TEXT,
                PRIMARY KEY(doi,user_id))''')
            raw.execute('''INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        ('10.1234/old', 'Old title', None, 'missing',
                         'Energy &amp; Environmental Science', '2026-01-01', '[]', '[]',
                         'journal-article', None, 'crossref', 'review', .4, 'rules_v1',
                         '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', 0))
            raw.execute('''INSERT INTO user_articles VALUES (?,?,?,?,?,?,?)''',
                        ('10.1234/old', 'local', 1, 1, 5, 'Do not lose', '2026-11-02'))
            raw.commit()
            raw.close()
            conn = connect(path)
            try:
                a = conn.execute('SELECT * FROM articles').fetchone()
                self.assertEqual(a['first_seen_at'], '2026-11-01T00:00:00Z')
                self.assertEqual(a['journal'], 'Energy & Environmental Science')
                # Old publication-date precision is unknowable until DOI is refreshed.
                self.assertIsNone(a['published_date_precision'])
                s = conn.execute('SELECT * FROM user_articles').fetchone()
                self.assertEqual((s['acknowledged'], s['hidden'], s['rating'], s['note']),
                                 (1, 1, 5, 'Do not lose'))
                upsert_article(conn, paper('10.1234/old'), classify(paper('10.1234/old')))
                a = conn.execute('SELECT * FROM articles').fetchone()
                self.assertEqual(a['published_date'], '2026')
                self.assertEqual(a['published_date_precision'], 'year')
                self.assertEqual(a['first_seen_at'], '2026-11-01T00:00:00Z')
            finally:
                conn.close()


class PreservationAndAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.temp.name) / 'db.sqlite')

    def tearDown(self):
        self.conn.close()  # Windows requires this before removing the SQLite file.
        self.temp.cleanup()

    def test_crossref_missing_does_not_overwrite_openalex_abstract(self):
        p = paper()
        upsert_article(self.conn, p, classify(p))
        enrich_abstract(self.conn, p['doi'], 'The reconstructed abstract.')
        upsert_article(self.conn, p, classify(p))
        a = self.conn.execute('SELECT * FROM articles').fetchone()
        self.assertEqual(a['abstract'], 'The reconstructed abstract.')
        self.assertEqual(a['abstract_source'], 'openalex')

    def test_manual_paper_stays_included_and_state_persists(self):
        p = paper(title='Unrelated topic')
        upsert_article(self.conn, p, classify(p), manual=True)
        set_user_state(self.conn, p['doi'], rating=5, note='Keep')
        result = reclassify_database(self.conn, classify)
        self.assertEqual(result['articles_checked'], 1)
        a = self.conn.execute('SELECT decision FROM articles').fetchone()
        self.assertEqual(a['decision'], 'include')
        self.assertEqual(self.conn.execute('SELECT note FROM user_articles').fetchone()[0], 'Keep')

    def test_rejected_records_can_be_audited_and_promoted_without_deleting(self):
        unrelated = paper('10.1234/unrelated', 'BaTiO3 oxide perovskite cathode', 'Synthetic oxide material study.')
        borderline = paper('10.1234/borderline', 'Ferroelectric perovskite crystals', 'Synthetic crystallographic study.')
        record_rejected(self.conn, unrelated, classify(unrelated), 'ees')
        record_rejected(self.conn, borderline, {'decision': 'reject', 'score': 0,
                         'reason': 'old rule', 'version': 'rules_v1'}, 'ees')
        before = reclassify_database(self.conn, classify, dry_run=True)
        self.assertEqual(before['promoted'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 0)
        after = reclassify_database(self.conn, classify)
        self.assertEqual(after['promoted'], 1)
        self.assertEqual(self.conn.execute('SELECT decision FROM articles').fetchone()[0], 'review')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 1)
        output = Path(self.temp.name) / 'rejected.csv'
        self.assertEqual(sample_rejected(self.conn, output), 1)
        with output.open(encoding='utf-8-sig', newline='') as f:
            sample = list(csv.DictReader(f))
        self.assertEqual(sample[0]['doi'], '10.1234/unrelated')
        self.assertEqual(sample[0]['human_relevance_label'], '')

    def test_export_is_public_by_default(self):
        p = paper()
        upsert_article(self.conn, p, classify(p))
        set_user_state(self.conn, p['doi'], note='Confidential', rating=5)
        out = Path(self.temp.name) / 'export.json'
        export_json(self.conn, out)
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(data['articles'][0]['published_date_precision'], 'year')
        self.assertNotIn('user_states', data['articles'][0])
        export_json(self.conn, out, include_private=True)
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(data['articles'][0]['user_states'][0]['note'], 'Confidential')

    def test_refresh_legacy_dates_does_not_erase_user_notes(self):
        p = paper()
        p.pop('published_date_precision')
        upsert_article(self.conn, p, classify(p))
        set_user_state(self.conn, p['doi'], note='Persistent note')
        def fake_fetch(doi):
            return normalize_crossref({'DOI': doi, 'title': ['Halide perovskite LEDs'],
                'published': {'date-parts': [[2026]]}})
        result = refresh_existing_metadata(self.conn, fetch=fake_fetch)
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(result['remaining_unknown_precision'], 0)
        self.assertEqual(self.conn.execute('SELECT published_date_precision FROM articles').fetchone()[0], 'year')
        self.assertEqual(self.conn.execute('SELECT note FROM user_articles').fetchone()[0], 'Persistent note')

    def test_created_mode_is_always_checked_and_rejects_archived(self):
        irrelevant = paper('10.1234/oxide', 'BaTiO3 oxide perovskites', 'Synthetic oxide cathode study.')
        relevant = paper('10.1234/halide', 'MAPbI3 solar cells', 'Synthetic perovskite photovoltaic study.')
        calls = []

        def fake_crossref(issn, start, end, mode='pub'):
            calls.append(mode)
            return iter([irrelevant] if mode == 'created' else [relevant] if mode == 'pub' else [])

        with patch('collector.__main__.crossref_journal', side_effect=fake_crossref):
            with redirect_stdout(io.StringIO()):
                result = collect(self.conn, [{'id': 'ees', 'issns': ['1754-5692']}],
                                 '2026-11-01', '2026-11-18')[0]
        self.assertEqual(calls, ['pub', 'online', 'created', 'updated'])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['created_only'], 1)
        self.assertEqual(result['imprecise_publication_dates'], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM rejected_candidates').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
        self.assertEqual(quality_report(self.conn)['rejected_candidates_archived'], 1)


class OpenAlexTests(unittest.TestCase):
    def test_inverted_index_reconstruction(self):
        inverted = {'Perovskite': [0, 3], 'solar': [1], 'cells': [2], 'improve.': [4]}
        self.assertEqual(reconstruct_abstract(inverted),
                         'Perovskite solar cells Perovskite improve.')
        self.assertIsNone(reconstruct_abstract(None))
        self.assertIsNone(reconstruct_abstract({'bad': [-1]}))

    def test_enrichment_is_idempotent_and_doi_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'db.sqlite')
            try:
                p = paper()
                upsert_article(conn, p, classify(p))
                def fake_fetch(doi):
                    return {'doi': 'https://doi.org/' + doi,
                            'abstract_inverted_index': {'Valid': [0], 'abstract': [1]}}
                result = enrich_missing_abstracts(conn, limit=1, fetch=fake_fetch)
                self.assertEqual((result['checked'], result['filled']), (1, 1))
                self.assertEqual(conn.execute('SELECT abstract_source FROM articles').fetchone()[0], 'openalex')
                result = enrich_missing_abstracts(conn, limit=1, fetch=fake_fetch)
                self.assertEqual(result['checked'], 0)
            finally:
                conn.close()

    def test_wrong_doi_does_not_write_abstract(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = connect(Path(folder) / 'db.sqlite')
            try:
                p = paper()
                upsert_article(conn, p, classify(p))
                result = enrich_missing_abstracts(conn, fetch=lambda _: {
                    'doi': 'https://doi.org/10.1234/wrong',
                    'abstract_inverted_index': {'wrong': [0]}})
                self.assertEqual(result['checked'], 0)
                self.assertEqual(len(result['errors']), 1)
                self.assertIsNone(conn.execute('SELECT abstract FROM articles').fetchone()[0])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
