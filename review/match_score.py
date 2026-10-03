"""Deterministic match score between a manuscript and one venue configuration.

The score explains fit from the manuscript's own data and the venue's configured
rules. It is not a quality ranking and it never changes eligibility.

    Scope fit with aims & topics          40
    Accepted article type                 25
    Venue requirements met                20
    Methods & quality signals             15
"""
import re

WEIGHTS = {'scope': 40, 'type': 25, 'requirements': 20, 'methods': 15}

STOPWORDS = set('''
a about above after again against all also among an and any are as at be because been before being below between
both but by can could did do does doing during each few for from further had has have having how into is it its
itself more most new not now of off on once only or other our out over own same should so some such than that the
their them then there these they this those through to too under until up upon use used using very was we were what
when where which while who whom why will with within without would you your journal journals article articles paper
papers research study studies international review based approach analysis case data new results submission
manuscript manuscripts publish publishes published publication publications scope aims author authors work works
'''.split())


def _terms(text):
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
        for key in ('topics', 'keywords', 'research_questions', 'contribution', 'summary', 'methods', 'field', 'domains'):
            value = semantic.get(key)
            if isinstance(value, list):
                parts.extend(str(v) for v in value[:20] if not isinstance(v, dict))
            elif isinstance(value, str):
                parts.append(value)
    return ' '.join(parts)


def _normalise(label):
    return re.sub(r'[^a-z0-9]+', '_', str(label or '').strip().lower()).strip('_')


def compute_match_score(manuscript, config, *, eligibility='needs_changes', violations=0):
    """Return {'score', 'label', 'breakdown', 'matched_terms'} for one manuscript-venue pair."""
    if config is None:
        return {'score': 0, 'label': 'Not configured', 'breakdown': {k: 0 for k in WEIGHTS}, 'matched_terms': []}
    from .models import Manuscript

    # 1. Scope fit: manuscript title/abstract/keywords/profile vs venue aims, scope and reviewer focus.
    keywords = [k for k in (manuscript.keywords or []) if str(k).strip()]
    ms_terms = _terms(' '.join([manuscript.title or '', manuscript.abstract or '', ' '.join(map(str, keywords)),
                                _profile_text(manuscript)]))
    venue_text = ' '.join([config.aims_scope or '', ' '.join(map(str, config.reviewer_criteria or [])),
                           config.quality_threshold or '', config.venue.name if config.venue_id else ''])
    venue_terms = _terms(venue_text)
    overlap = ms_terms & venue_terms
    if not config.aims_scope:
        scope_ratio = 0.0
    else:
        term_ratio = min(1.0, len(overlap) / max(1, min(len(ms_terms), 20)) * 2.5)
        if keywords:
            keyword_hits = sum(1 for k in keywords if _terms(k) and _terms(k) <= venue_terms)
            keyword_ratio = keyword_hits / len(keywords)
            scope_ratio = 0.6 * term_ratio + 0.4 * keyword_ratio
        else:
            scope_ratio = term_ratio
    scope = round(WEIGHTS['scope'] * min(1.0, scope_ratio))

    # 2. Accepted article type.
    accepted = {_normalise(t) for t in (config.article_types or [])}
    type_label = _normalise(dict(Manuscript.TYPE_CHOICES).get(manuscript.manuscript_type, manuscript.manuscript_type))
    if not accepted:
        type_points = round(WEIGHTS['type'] * 0.4)  # not configured: unknown, partial credit
    elif _normalise(manuscript.manuscript_type) in accepted or type_label in accepted:
        type_points = WEIGHTS['type']
    else:
        type_points = 0

    # 3. Venue requirements met (structured desk-rejection rules).
    rules = len(config.structured_desk_rejection_rules or [])
    requirements = round(WEIGHTS['requirements'] * (1 - min(violations, rules) / rules)) if rules else WEIGHTS['requirements']

    # 4. Methods & quality signals.
    methods = [m for m in (config.accepted_methods or []) if str(m).strip()]
    if not methods:
        method_points = WEIGHTS['methods']  # the venue does not restrict methods
    else:
        method_terms = _terms(' '.join(map(str, methods)))
        method_points = WEIGHTS['methods'] if ms_terms & method_terms else round(WEIGHTS['methods'] / 3)

    # Requirements and methods only count in proportion to actual topical/type fit, so a venue on an
    # unrelated subject cannot score well just because it has few rules.
    fit_factor = min(1.0, (scope + type_points) / ((WEIGHTS['scope'] + WEIGHTS['type']) / 2))
    requirements = round(requirements * fit_factor)
    method_points = round(method_points * fit_factor)
    score = scope + type_points + requirements + method_points
    if accepted and type_points == 0:
        score = min(score, 50)  # the venue does not take this kind of manuscript: never a strong fit
    if eligibility == 'ineligible':
        score = min(score, 30)  # a failed desk rule caps the score
    score = max(0, min(100, score))
    label = 'Strong fit' if score >= 75 else 'Good fit' if score >= 55 else 'Partial fit' if score >= 35 else 'Low fit'
    return {
        'score': score,
        'label': label,
        'breakdown': {'scope': scope, 'type': type_points, 'requirements': requirements, 'methods': method_points},
        'matched_terms': sorted(overlap)[:8],
    }
