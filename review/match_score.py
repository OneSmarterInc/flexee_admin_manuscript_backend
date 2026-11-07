"""Deterministic match score between a manuscript and one venue configuration.

The score explains fit from the manuscript's own data and the venue's configured
rules. It is not a quality ranking and it never changes eligibility.

The displayed score is topical relevance only. The legacy breakdown retains
separate type, requirements and methods diagnostics; these do not add to fit.
"""
import ast
import re

WEIGHTS = {'scope': 40, 'type': 25, 'requirements': 20, 'methods': 15}

STOPWORDS = set('''
a about above after again against all also among an and any are as at be because been before being below between
both but by can could did do does doing during each few for from further had has have having how into is it its
itself more most new not now of off on once only or other our out over own same should so some such than that the
their them then there these they this those through to too under until up upon use used using very was we were what
when where which while who whom why will with within without would you your journal journals article articles paper
papers research study studies international review based approach analysis case data new results submission
comparative comparison evaluation evaluating systematic performance small large model models
manuscript manuscripts publish publishes published publication publications scope aims author authors work works
'''.split())


ALIASES = {'ai': 'artificial intelligence', 'rag': 'retrieval augmented generation',
           'nlp': 'natural language processing', 'slm': 'small language model',
           'llm': 'large language model', 'ml': 'machine learning'}


def semantic_text(value):
    """Extract content, not field names or line numbers, from old/new profiles."""
    if isinstance(value, str) and value.strip().startswith('{'):
        try:
            value = ast.literal_eval(value[:10000])
        except (ValueError, SyntaxError, RecursionError):
            return ''
    if isinstance(value, dict):
        return str(value.get('text') or value.get('label') or '')
    return value if isinstance(value, str) else ''


def keyword_phrases(values):
    if isinstance(values, str):
        values = [values]
    return [part.strip() for value in (values or [])
            for part in re.split(r'[;,\n]', semantic_text(value)) if part.strip()]


def _terms(text):
    text = str(text or '').lower()
    text = re.sub(r'\b(?:small|large) language models?\b', lambda m: m.group(0) + ' artificial intelligence natural language processing', text)
    text = re.sub(r'\bintelligent\b', 'intelligence', text)
    text = re.sub(r'\b(' + '|'.join(ALIASES) + r')s?\b', lambda m: ALIASES[m.group(1)], text)
    out = set()
    for token in re.findall(r'[a-zA-Z][a-zA-Z\-]{2,}', str(text or '').lower()):
        for part in token.split('-'):
            if len(part) < 4 or part in STOPWORDS:
                continue
            out.add(part[:-1] if part.endswith('s') and len(part) > 4 else part)
    return out


def _profile_text(manuscript):
    semantic = (manuscript.parsed_profile or {}).get('semantic') or {}
    parts = []
    if isinstance(semantic, dict):
        for key in ('topics', 'keywords', 'research_questions', 'contribution', 'contributions', 'summary', 'methods', 'field', 'domains'):
            value = semantic.get(key)
            if isinstance(value, list):
                parts.extend(semantic_text(v) for v in value[:20])
            elif isinstance(value, str):
                parts.append(semantic_text(value))
    return ' '.join(parts)


def _normalise(label):
    return re.sub(r'[^a-z0-9]+', '_', str(label or '').strip().lower()).strip('_')


def domain_connection_unconfirmed(manuscript, config):
    """Conservative flag, not exclusion: shared technology does not establish
    the specialist disciplinary contribution required by a scope."""
    disciplines = (
        r'accounting|auditing|accountancy',
        r'law|legal|jurisprudence|litigation',
        r'microeconomics|macroeconomics|econometrics',
        r'clinical|medicine|medical|patient|healthcare',
        r'agricultural|agriculture|agronomy',
        r'psychology|psychological|psychometric',
    )
    scope = (config.aims_scope or '').casefold()
    manuscript_text = ' '.join([manuscript.title or '', manuscript.abstract or '',
                                ' '.join(keyword_phrases(manuscript.keywords))]).casefold()
    required = [pattern for pattern in disciplines if re.search(r'\b(?:' + pattern + r')\b', scope)]
    return bool(required) and not any(re.search(r'\b(?:' + pattern + r')\b', manuscript_text) for pattern in required)


def normalize_article_type(value):
    label = _normalise(value)
    return {'research_articles': 'research_article', 'research_paper': 'research_article',
            'research_papers': 'research_article', 'original_research_article': 'research_article',
            'review_articles': 'review_article', 'case_studies': 'case_study',
            'conference_papers': 'conference_paper'}.get(label, label)


def compute_match_score(manuscript, config, *, eligibility='needs_changes', violations=0, topic_similarity=None):
    """Return {'score', 'label', 'breakdown', 'matched_terms'} for one manuscript-venue pair."""
    if config is None:
        return {'score': 0, 'label': 'Not configured', 'breakdown': {k: 0 for k in WEIGHTS}, 'matched_terms': []}
    from .models import Manuscript

    # 1. Scope fit: manuscript title/abstract/keywords/profile vs venue aims, scope and reviewer focus.
    keywords = keyword_phrases(manuscript.keywords)
    ms_terms = _terms(' '.join([manuscript.title or '', manuscript.abstract or '', ' '.join(map(str, keywords)),
                                _profile_text(manuscript)]))
    venue_text = ' '.join([config.aims_scope or '', ' '.join(map(str, config.reviewer_criteria or [])),
                           config.quality_threshold or ''])
    venue_terms = _terms(venue_text)
    overlap = ms_terms & venue_terms
    if not config.aims_scope:
        scope_ratio = 0.0
    else:
        term_ratio = min(1.0, len(overlap) / max(1, min(len(ms_terms), 20)) * 2.5)
        if keywords:
            keyword_hits = sum(1 for k in keywords if _terms(k) and _terms(k) <= venue_terms)
            keyword_ratio = keyword_hits / len(keywords)
            primary = _terms(re.sub(r'^\s*manuscript\s+title\s*:\s*', '', manuscript.title or '', flags=re.I))
            primary_ratio = min(1.0, len(primary & venue_terms) / max(1, len(primary)) * 2.5)
            scope_ratio = 0.6 * primary_ratio + 0.25 * keyword_ratio + 0.15 * term_ratio
        else:
            scope_ratio = term_ratio
        if topic_similarity is not None:
            # Build plan step 5/8: the local embedding similarity also counts for scope, so a venue whose
            # aims are close in meaning (not just in shared words) is not scored as off-topic.
            # nomic-embed-text cosines run about 0.40 (unrelated) to 0.70 (very close).
            semantic_ratio = max(0.0, min(1.0, (topic_similarity - 0.40) / 0.30))
            # Similar embeddings alone do not establish subject relevance.
            # Generic academic language must not turn an unrelated scope into a good fit.
            scope_ratio = max(scope_ratio, min(semantic_ratio, scope_ratio + 0.20))
    scope = round(WEIGHTS['scope'] * min(1.0, scope_ratio))

    # 2. Accepted article type.
    accepted = {normalize_article_type(t) for t in (config.article_types or [])}
    type_label = _normalise(dict(Manuscript.TYPE_CHOICES).get(manuscript.manuscript_type, manuscript.manuscript_type))
    if not accepted:
        type_points = round(WEIGHTS['type'] * 0.4)  # not configured: unknown, partial credit
    elif _normalise(manuscript.manuscript_type) in accepted or type_label in accepted:
        type_points = WEIGHTS['type']
    else:
        type_points = 0

    # 3. Venue requirements met (structured desk-rejection rules).
    rules = len(config.structured_desk_rejection_rules or [])
    requirements = round(WEIGHTS['requirements'] * (1 - min(violations, rules) / rules)) if rules else 0

    # 4. Methods & quality signals.
    methods = [m for m in (config.accepted_methods or []) if str(m).strip()]
    if not methods:
        method_points = 0  # no recorded method restriction is not evidence of topical fit
    else:
        method_terms = _terms(' '.join(map(str, methods)))
        method_points = WEIGHTS['methods'] if ms_terms & method_terms else round(WEIGHTS['methods'] / 3)

    # Subject relevance is independent of submission eligibility. Missing rules
    # must never inflate fit; routing still uses the existing deterministic gates.
    score = round(100 * scope_ratio)
    domain_unknown = domain_connection_unconfirmed(manuscript, config)
    if domain_unknown:
        score = min(score, 34)
    label = 'Strong fit' if score >= 75 else 'Good fit' if score >= 55 else 'Partial fit' if score >= 35 else 'Low fit'
    if domain_unknown:
        label = 'Discipline needs review'
    if not config.aims_scope.strip():
        score, label = None, 'Scope not recorded'
    return {
        'score': score,
        'label': label,
        'breakdown': {'scope': scope, 'type': type_points, 'requirements': requirements, 'methods': method_points},
        'matched_terms': sorted(overlap)[:8],
    }
