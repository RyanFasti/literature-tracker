"""Regression check: automated abstract-first collection receives API secrets."""
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/weekly-public-catalog.yml'


class TestCIAbstractSecrets(unittest.TestCase):
    def test_provider_secrets_in_both_retrieval_steps(self):
        source = WORKFLOW.read_text(encoding='utf-8')
        for name in ('Restore and retry pending abstracts from earlier runs',
                     'Collect configured overlapping Crossref window'):
            with self.subTest(step=name):
                start = source.index('      - name: ' + name)
                end = source.find('      - name: ', start + 1)
                block = source[start:end if end >= 0 else None]
                self.assertIn('        env:', block)
                for provider in ('ELSEVIER_API_KEY', 'ELSEVIER_INSTTOKEN',
                                 'OPENALEX_API_KEY', 'SEMANTIC_SCHOLAR_API_KEY'):
                    self.assertIn(provider + ': ${{ secrets.' + provider + ' }}', block)

    def test_missing_key_warns_without_disclosing_secret(self):
        source = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn('::warning title=Elsevier abstracts unavailable::', source)
        self.assertNotIn('echo "$ELSEVIER_API_KEY"', source)


if __name__ == '__main__':
    unittest.main()
