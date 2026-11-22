"""Explainable multi-label paper topic suggestions, not validated classifications.

Use title and any locally indexed abstract; only topic IDs, never abstract text,
are included in the public metadata catalog.
"""
import re
import json
from pathlib import Path

TOPIC_IDS = (
    'solar-cell', 'photodetector', 'led', 'film', 'stability',
    'fundamentals', 'other-devices',
)
PATTERNS = {
    'solar-cell': r'\b(?:solar cells?|photovoltaics?|pv devices?|solar modules?)\b',
    'photodetector': r'\b(?:photodetectors?|photodiodes?|photoconductors?|phototransistors?|light detectors?)\b',
    'led': r'\b(?:light[ -]emitting diodes?|leds?|electroluminescen\w*|light emitters?)\b',
    'film': r'\b(?:thin[ -]films?|film formation|spin[ -]coat\w*|blade[ -]coat\w*|slot[ -]die|crystallization|grain boundar\w*|deposition|solution process\w*)\b',
    'stability': r'\b(?:stabilit\w*|degrad\w*|aging|ageing|durability|decompos\w*|operational lifetime|ion migrat\w*)\b',
    'fundamentals': r'\b(?:exciton\w*|phonon\w*|bandgap\w*|band gap\w*|defect physics|carrier dynamics|charge transport|crystal structure|phase transition\w*)\b',
    'other-devices': r'\b(?:transistors?|memristors?|lasers?|sensors?|scintillators?|x[ -]ray detectors?)\b',
}
COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in PATTERNS.items()}


def read_topic_keywords(path=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / 'config' / 'topic-keywords.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != set(TOPIC_IDS):
        raise ValueError('Invalid topic keyword configuration')
    for terms in data.values():
        if not isinstance(terms, list) or len(terms) > 35 or any(not isinstance(x, str) or not x.strip() or len(x) > 65 for x in terms):
            raise ValueError('Invalid topic keywords')
    return data


def suggest_topics(title='', abstract='', keywords=None):
    text = f'{title or ""} {abstract or ""}'
    if keywords is None:
        return [name for name in TOPIC_IDS if COMPILED[name].search(text)]
    return [name for name in TOPIC_IDS if any(
        re.search(r'(?<!\w)' + re.escape(term.casefold()) + r'(?!\w)', text.casefold())
        for term in keywords[name])]
