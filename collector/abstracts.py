"""DOI-verified, abstract-first metadata retrieval.

All provider calls are best-effort and auditable. No provider is allowed to
silently attach text to a different DOI. The caller must not classify a paper
as irrelevant when its abstract has not been resolved.

No API credentials are stored in the database, logs, or exports.
"""
import html
from html.parser import HTMLParser
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .core import USER_AGENT, normalize_doi
from .openalex import reconstruct_abstract


class _AbstractHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.title_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower().split(':')[-1]
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('h1', 'h2', 'h3', 'title'):
            self.title_depth += 1
        elif tag in ('br', 'p', 'div', 'section'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        tag = tag.lower().split(':')[-1]
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        elif tag in ('h1', 'h2', 'h3', 'title') and self.title_depth:
            self.title_depth -= 1
        elif tag in ('p', 'div', 'section'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip and not self.title_depth:
            self.parts.append(data)


def clean_abstract(value):
    """Flatten XML/HTML without inserting spaces around inline scientific markup.

    Keep punctuation, chemical notation and parenthetical expressions intact.
    Do not strip a meaningful first word merely because it is 'abstract'.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _AbstractHTML()
    try:
        parser.feed(value)
        text = ''.join(parser.parts)
    except (ValueError, AssertionError):
        text = re.sub(r'<[^>]*>', '', value)
    text = html.unescape(text).replace('\u00a0', ' ')
    text = re.sub(r'[\t\r\n ]+', ' ', text).strip()
    text = re.sub(r'^Abstract\s*[:：]\s*', '', text, flags=re.I)
    text = re.sub(r'\s+([,.;:!?%)\]])', r'\1', text)
    text = re.sub(r'([([\u2014])\s+', r'\1', text)
    text = re.sub(r'\s+([\u2010-\u2013])\s+', r'\1', text)
    return text or None


def _json_get(url, headers=None, attempts=2, timeout=18):
    for i in range(attempts):
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
            retry = exc.headers.get('Retry-After') if exc.headers else None
            try:
                delay = min(max(float(retry), 0), 15) if retry else 2 ** i
            except ValueError:
                delay = 2 ** i
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


def _text_get(url, headers=None, attempts=2, timeout=18):
    for i in range(attempts):
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/xml', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
            time.sleep(2 ** i)
        except (urllib.error.URLError, TimeoutError):
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


def _matches_doi(value, doi):
    try:
        return normalize_doi(value) == normalize_doi(doi)
    except (TypeError, ValueError):
        return False


def from_openalex(paper):
    doi = normalize_doi(paper['doi'])
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    url = ('https://api.openalex.org/works/https://doi.org/' +
           urllib.parse.quote(doi, safe='/'))
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    data = _json_get(url, headers)
    if not data:
        return None
    if not _matches_doi(data.get('doi'), doi):
        raise ValueError('OpenAlex DOI mismatch')
    return reconstruct_abstract(data.get('abstract_inverted_index'))


def from_semantic_scholar(paper):
    doi = normalize_doi(paper['doi'])
    key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY', '').strip()
    url = ('https://api.semanticscholar.org/graph/v1/paper/' +
           urllib.parse.quote('DOI:' + doi, safe='') +
           '?fields=title,abstract,externalIds')
    headers = {'x-api-key': key} if key else {}
    data = _json_get(url, headers)
    if not data:
        return None
    if not _matches_doi((data.get('externalIds') or {}).get('DOI'), doi):
        raise ValueError('Semantic Scholar DOI missing or mismatched')
    return data.get('abstract')


def from_europe_pmc(paper):
    doi = normalize_doi(paper['doi'])
    url = ('https://www.ebi.ac.uk/europepmc/webservices/rest/search?' +
           urllib.parse.urlencode({'query': 'DOI:' + doi, 'format': 'json',
                                   'resultType': 'core', 'pageSize': 5}))
    data = _json_get(url)
    for record in ((data or {}).get('resultList') or {}).get('result', []):
        if _matches_doi(record.get('doi'), doi):
            return record.get('abstractText')
    return None


def parse_elsevier_xml(payload, doi):
    if not payload:
        return None
    root = ET.fromstring(payload)
    found_dois = []
    for element in root.iter():
        name = element.tag.rsplit('}', 1)[-1].lower()
        if name in ('doi', 'prism:doi'):
            text = ''.join(element.itertext()).strip()
            if text:
                found_dois.append(text)
    if found_dois and not any(_matches_doi(d, doi) for d in found_dois):
        raise ValueError('Elsevier DOI mismatch')
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1].lower() == 'abstract':
            parts = []
            for node in element.iter():
                name = node.tag.rsplit('}', 1)[-1].lower()
                if name in ('title', 'label', 'section-title'):
                    if node.tail:
                        parts.append(node.tail)
                    continue
                if node.text:
                    parts.append(node.text)
                if node.tail:
                    parts.append(node.tail)
            value = clean_abstract(''.join(parts))
            if value and len(value) >= 40:
                return value
    # Some entitled META_ABS responses put the full abstract in dc:description
    # rather than ce:abstract. Accept only a substantive description, not a
    # short SEO snippet or copyright notice.
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1].lower() == 'description':
            value = clean_abstract(''.join(element.itertext()))
            if value and len(value) >= 160 and not value.lower().startswith(('copyright ', 'all rights reserved')):
                return value
    return None


def from_elsevier(paper):
    doi = normalize_doi(paper['doi'])
    if not doi.startswith('10.1016/'):
        return None
    key = os.environ.get('ELSEVIER_API_KEY', '').strip()
    if not key:
        return None
    headers = {'X-ELS-APIKey': key}
    token = os.environ.get('ELSEVIER_INSTTOKEN', '').strip()
    if token:
        headers['X-ELS-Insttoken'] = token
    url = ('https://api.elsevier.com/content/article/doi/' +
           urllib.parse.quote(doi, safe='/') + '?view=META_ABS&httpAccept=text/xml')
    return parse_elsevier_xml(_text_get(url, headers), doi)


PROVIDERS = {
    'openalex': from_openalex,
    'semantic_scholar': from_semantic_scholar,
    'europe_pmc': from_europe_pmc,
    'elsevier': from_elsevier,
}


class AbstractResolver:
    """Reusable across papers; avoid repeatedly hitting a rate-limited service."""
    def __init__(self, providers=None):
        self.providers = providers or PROVIDERS
        self.blocked = set()

    def resolve(self, paper):
        if paper.get('abstract'):
            value = clean_abstract(paper['abstract'])
            if value:
                return {'abstract': value, 'source': paper.get('abstract_source') or 'crossref',
                        'status': 'available', 'attempts': []}
        # For CEJ, prefer publisher-authorized API if available.
        keys = list(self.providers)
        if (paper['doi'].startswith('10.1016/') and os.environ.get('ELSEVIER_API_KEY')
                and 'elsevier' in keys):
            keys.remove('elsevier')
            keys.insert(0, 'elsevier')
        attempts = []
        for name in keys:
            if name in self.blocked:
                attempts.append({'provider': name, 'status': 'skipped_rate_limit'})
                continue
            if name == 'elsevier' and not os.environ.get('ELSEVIER_API_KEY'):
                attempts.append({'provider': name, 'status': 'not_configured'})
                continue
            try:
                value = self.providers[name](paper)
                abstract = clean_abstract(value)
                if abstract and len(abstract) >= 40:
                    attempts.append({'provider': name, 'status': 'available'})
                    return {'abstract': abstract, 'source': name,
                            'status': 'available', 'attempts': attempts}
                attempts.append({'provider': name, 'status': 'no_abstract'})
            except Exception as exc:
                status = 'error'
                if isinstance(exc, urllib.error.HTTPError):
                    status = 'http_' + str(exc.code)
                    if exc.code == 429:
                        self.blocked.add(name)
                attempts.append({'provider': name, 'status': status,
                                 'error': type(exc).__name__})
        has_error = any(x['status'].startswith('http_') or x['status'] == 'error'
                        for x in attempts)
        return {'abstract': None, 'source': None,
                'status': 'provider_error' if has_error else 'not_found',
                'attempts': attempts}
