"""Normalize model presentation without inventing missing content."""
import json
import re


def model_json(text):
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)[:-3].strip()
    return json.loads(text)


def normalized(text):
    return ' '.join(text.translate(str.maketrans({
        '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    })).split())


def source_quote(quote, passage):
    if not isinstance(quote, str) or not quote.strip() or not passage:
        return None
    if quote in passage:
        return quote
    text, source = normalized(quote), normalized(passage)
    if text in source:
        return passage
    parts = [p.strip() for p in re.split(r'\.{3,}|\u2026', text) if p.strip()]
    if len(parts) < 2:
        return None
    position = 0
    for part in parts:
        start = source.find(part, position)
        if start < 0:
            return None
        position = start + len(part)
    return passage
