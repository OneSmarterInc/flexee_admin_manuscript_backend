import os
import random
import re
from difflib import SequenceMatcher

import httpx


REFERENCE_STATES = ('verified', 'weak match', 'not found')
_REFERENCE_HEADING = re.compile(
    r'^\s*(?:#{1,6}\s*)?(references|bibliography|works cited|literature cited)\s*:?[\s\-]*$',
    re.I,
)
_BACK_MATTER_HEADING = re.compile(
    r'^\s*(?:#{1,6}\s*)?(appendix(?:\s+[a-z0-9]+)?|index|glossary|endnotes|notes|'
    r'acknowledg(?:e)?ments|about the author|author bio|figure credits|tables|exhibits)\b',
    re.I,
)
_REFERENCE_PREFIX = re.compile(r'^\s*(?:\[\d{1,3}\]|\d{1,3}[.)]|[-*•])\s+')
_YEAR = re.compile(r'(?:\(|\b)(?:19|20)\d{2}[a-z]?(?:\)|\b)')
_DOI = re.compile(r'\b10\.\d{4,9}/\S+\b', re.I)


def _clean_space(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _strip_reference_prefix(value):
    return _clean_space(_REFERENCE_PREFIX.sub('', str(value or '')).strip())


def _extract_references_section(raw_text):
    """Extract only the references/bibliography section before any AI/model work.

    The old implementation sent the final 30,000 manuscript characters to the
    local model.  That polluted the small context window and let body text leak
    into citation extraction.  This deliberately slices the back-matter section
    first using deterministic headings.
    """
    lines = str(raw_text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    heading_indexes = [idx for idx, line in enumerate(lines) if _REFERENCE_HEADING.match(line.strip())]
    if not heading_indexes:
        return ''

    start = heading_indexes[-1] + 1
    collected = []
    for line in lines[start:]:
        stripped = line.strip()
        if collected and stripped and _BACK_MATTER_HEADING.match(stripped):
            break
        if collected and stripped.startswith('#') and not _REFERENCE_PREFIX.match(stripped):
            break
        collected.append(line)
    return '\n'.join(collected).strip()


def _looks_like_reference(value):
    text = _clean_space(value)
    if len(text) < 30:
        return False
    if word_like_count(text) < 6:
        return False
    return bool(_YEAR.search(text) or _DOI.search(text) or re.search(r'\bdoi\b|\bet al\.\b', text, re.I))


def word_like_count(value):
    return len(re.findall(r"[\w'-]+", str(value or ''), flags=re.UNICODE))


def _split_reference_entries(section_text):
    entries = []
    current = []

    def flush():
        if not current:
            return
        item = _strip_reference_prefix(' '.join(current))
        current.clear()
        if _looks_like_reference(item):
            entries.append(item)

    for raw_line in str(section_text or '').split('\n'):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if _REFERENCE_PREFIX.match(line):
            flush()
            current.append(line)
            continue
        if current:
            current.append(line)
        else:
            current.append(line)
    flush()

    # De-duplicate while preserving order.
    unique = []
    seen = set()
    for entry in entries:
        key = _normalize_title(entry)[:160]
        if key and key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _extract_citations(raw_text):
    """Extract citation strings from the references section without using the LLM."""
    section = _extract_references_section(raw_text)
    if not section:
        return 0, []
    citations = _split_reference_entries(section)
    return len(citations), citations[:15]


def _candidate_titles(citation_text):
    text = _clean_space(citation_text)
    candidates = []

    for quoted in re.findall(r'[“\"]([^“”\"]{12,180})[”\"]', text):
        candidates.append(quoted)

    year_match = _YEAR.search(text)
    if year_match:
        after_year = text[year_match.end():].lstrip(').,;: ')
        parts = [part.strip() for part in re.split(r'\.\s+', after_year) if part.strip()]
        if parts:
            candidates.append(parts[0])
            if len(parts) > 1 and len(parts[0]) < 45:
                candidates.append(f'{parts[0]}. {parts[1]}')

    doi_removed = _DOI.sub('', text)
    chunks = [chunk.strip() for chunk in re.split(r'\.\s+', doi_removed) if chunk.strip()]
    for chunk in chunks[:4]:
        if 4 <= word_like_count(chunk) <= 24 and not re.search(r'\b(journal|press|vol|volume|issue|doi)\b', chunk, re.I):
            candidates.append(chunk)

    cleaned = []
    seen = set()
    for candidate in candidates:
        candidate = _clean_space(candidate.strip(' .,:;'))
        key = _normalize_title(candidate)
        if len(key) >= 15 and key not in seen:
            seen.add(key)
            cleaned.append(candidate)
    return cleaned


def _normalize_title(value):
    text = str(value or '').lower()
    text = re.sub(r'https?://\S+', ' ', text)
    text = _DOI.sub(' ', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return _clean_space(text)


def _crossref_title(item):
    title = item.get('title') if isinstance(item, dict) else None
    if isinstance(title, list) and title:
        return _clean_space(title[0])
    if isinstance(title, str):
        return _clean_space(title)
    return ''


def _title_similarity(citation_text, matched_title):
    matched = _normalize_title(matched_title)
    if not matched:
        return 0.0
    whole_citation = _normalize_title(citation_text)
    if len(matched.split()) >= 4 and matched in whole_citation:
        return 1.0

    candidates = _candidate_titles(citation_text) or [citation_text]
    scores = []
    for candidate in candidates:
        candidate_norm = _normalize_title(candidate)
        if not candidate_norm:
            continue
        if candidate_norm in matched or matched in candidate_norm:
            scores.append(1.0)
        else:
            scores.append(SequenceMatcher(None, candidate_norm, matched).ratio())
    return max(scores or [0.0])


def _verify_citation_crossref(citation_text):
    """Return Crossref verification status for one citation.

    States:
    - verified: Crossref score is strong and matched title is similar.
    - weak match: Crossref returned something plausible but not strong enough.
    - not found: no result, request failure, or unrelated best-match result.
    """
    url = 'https://api.crossref.org/works'
    params = {
        'query.bibliographic': citation_text,
        'rows': 1,
        'select': 'title,score,DOI',
    }
    verified_score = float(os.getenv('CROSSREF_VERIFIED_SCORE', '35'))
    verified_similarity = float(os.getenv('CROSSREF_VERIFIED_TITLE_SIMILARITY', '0.72'))
    weak_score = float(os.getenv('CROSSREF_WEAK_SCORE', '12'))
    weak_similarity = float(os.getenv('CROSSREF_WEAK_TITLE_SIMILARITY', '0.45'))

    try:
        headers = {'User-Agent': 'Flexee Review Engine (editor@flexee.org)'}
        resp = httpx.get(url, params=params, headers=headers, timeout=10.0)
        if resp.status_code != 200:
            return {'status': 'not found', 'matched_title': None, 'score': 0.0, 'title_similarity': 0.0}
        data = resp.json()
        items = data.get('message', {}).get('items', [])
        if not items:
            return {'status': 'not found', 'matched_title': None, 'score': 0.0, 'title_similarity': 0.0}

        item = items[0]
        matched_title = _crossref_title(item)
        score = float(item.get('score') or 0.0)
        similarity = _title_similarity(citation_text, matched_title)

        if score >= verified_score and similarity >= verified_similarity:
            status = 'verified'
        elif score >= weak_score and similarity >= weak_similarity:
            status = 'weak match'
        else:
            status = 'not found'
        return {
            'status': status,
            'matched_title': matched_title or None,
            'score': score,
            'title_similarity': similarity,
            'doi': item.get('DOI') or None,
        }
    except Exception:
        return {'status': 'not found', 'matched_title': None, 'score': 0.0, 'title_similarity': 0.0}


def _shorten(value, limit):
    text = _clean_space(value)
    if len(text) > limit:
        return text[:limit - 1].rstrip() + '…'
    return text


def run_field_agent(raw_text):
    """
    Extract references, sample up to seven, verify with strict Crossref checks,
    and return a human-readable field briefing.
    """
    total_citations, citations = _extract_citations(raw_text)

    if not citations:
        return '\n\nFIELD BRIEFING:\nNo citations or references were found in this manuscript.'

    sample_size = min(7, len(citations))
    sampled = random.sample(citations, sample_size)

    results = []
    counts = {state: 0 for state in REFERENCE_STATES}
    for citation in sampled:
        result = _verify_citation_crossref(citation)
        status = result.get('status') if result.get('status') in REFERENCE_STATES else 'not found'
        counts[status] += 1
        results.append((citation, result | {'status': status}))

    briefing = '\n\nFIELD BRIEFING:\n'
    briefing += (
        f'The manuscript contains {total_citations} references. A random sample of {sample_size} citations was checked against Crossref.\n'
    )
    briefing += (
        f'Verification Summary: {counts["verified"]} verified, {counts["weak match"]} weak match, '
        f'{counts["not found"]} not found.\n\n'
    )

    briefing += 'Sampled Citations Checked:\n'
    for idx, (citation, result) in enumerate(results, 1):
        line = f'{idx}. [{result["status"]}] {_shorten(citation, 180)}'
        if result.get('matched_title'):
            line += f' | Crossref title: {_shorten(result["matched_title"], 140)}'
        if result.get('score') is not None:
            line += f' | score={result.get("score", 0):.1f}, title_similarity={result.get("title_similarity", 0):.2f}'
        briefing += line + '\n'

    flagged = [(citation, result) for citation, result in results if result.get('status') != 'verified']
    if flagged:
        briefing += '\nWARNING: Some sampled citations were weak matches or were not found in Crossref. Human review should confirm these references before acceptance.\n'
        for citation, result in flagged:
            briefing += f'- [{result["status"]}] {_shorten(citation, 150)}\n'

    return briefing
