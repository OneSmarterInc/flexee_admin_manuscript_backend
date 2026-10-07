"""Topical shortlist for matching (build plan step 5, section 7.1).

Before any language model looks at a manuscript-venue pair, a local embedding model (Ollama, CPU,
no per-query cost) narrows the matchable venues to the ~30 whose scope is closest to the manuscript.
Only those get the policy gate and the AI fit explanation.

No language model is involved and nothing escalates. If the embedding model cannot be reached, the
shortlist falls back to keyword overlap, so matching never stops because of it.

Vectors are stored unit-length, so cosine similarity is a plain dot product (no numpy needed:
2,500 venues x 768 dimensions takes well under a second).
"""
import hashlib
import logging
import math
import os
import time

from .local_llm import DEFAULT_OLLAMA_URL

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = 'nomic-embed-text'
DEFAULT_SHORTLIST_SIZE = 30
MAX_TEXT_CHARS = 6000   # nomic-embed-text reads about 2,000 tokens; the rest is cut by Ollama anyway
BATCH = 32


class EmbeddingUnavailable(Exception):
    """The embedding model could not be reached or returned nothing usable."""


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def embed_model():
    return os.getenv('VENUE_EMBED_MODEL', DEFAULT_EMBED_MODEL).strip() or DEFAULT_EMBED_MODEL


def shortlist_size():
    return _env_int('VENUE_SHORTLIST_SIZE', DEFAULT_SHORTLIST_SIZE, 5, 500)


def embeddings_enabled():
    return os.getenv('VENUE_EMBED_ENABLED', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}


def _prefixes(model):
    """nomic-embed-text (and similar) are trained with task prefixes; other models get none."""
    if 'nomic' in model:
        return 'search_query: ', 'search_document: '
    return '', ''


def _clip(text):
    return ' '.join(str(text or '').split())[:MAX_TEXT_CHARS]


def _hash(model, text):
    return hashlib.sha256(f'{model}\n{text}'.encode('utf-8')).hexdigest()


def _unit(vector):
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else None


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------

def active_config(venue):
    return venue.agent_configs.filter(active=True).order_by('-version', '-created_at').first()


def active_configs(venues):
    """{venue_id: active config} in one query (2,500 venues must not mean 2,500 queries)."""
    from ..models import VenueAgentConfig
    out = {}
    for config in (VenueAgentConfig.objects.filter(venue__in=[v.id for v in venues], active=True)
                   .order_by('venue_id', '-version', '-created_at')):
        out.setdefault(config.venue_id, config)
    return out


_MISSING = object()


def venue_text(venue, config=_MISSING):
    config = active_config(venue) if config is _MISSING else config
    parts = [venue.name, venue.description or '']
    if config is not None:
        parts += [f'Aims and scope: {config.aims_scope}' if config.aims_scope else '',
                  'Article types: ' + ', '.join(map(str, config.article_types or [])) if config.article_types else '',
                  'Reviewers look for: ' + '; '.join(map(str, config.reviewer_criteria or []))
                  if config.reviewer_criteria else '',
                  'Methods: ' + ', '.join(map(str, config.accepted_methods or [])) if config.accepted_methods else '']
    return _clip('. '.join(p for p in parts if p))


def manuscript_text(manuscript):
    semantic = (manuscript.parsed_profile or {}).get('semantic') or {}
    parts = [manuscript.title or '',
             'Keywords: ' + ', '.join(map(str, manuscript.keywords or [])) if manuscript.keywords else '',
             manuscript.abstract or '']
    if isinstance(semantic, dict):
        for key in ('summary', 'topics', 'contribution', 'field', 'domains', 'methods'):
            value = semantic.get(key)
            if isinstance(value, list):
                parts.append(', '.join(str(v) for v in value[:15] if not isinstance(v, dict)))
            elif isinstance(value, str) and value != 'Semantic analysis unavailable':
                parts.append(value)
    return _clip('. '.join(p for p in parts if p))


# ---------------------------------------------------------------------------
# Ollama /api/embed
# ---------------------------------------------------------------------------

def ollama_embed(texts, *, model=None, timeout=None, client=None):
    """Embed a list of texts with Ollama. Returns unit-length vectors in the same order."""
    import httpx
    model = model or embed_model()
    base_url = os.getenv('OLLAMA_BASE_URL', DEFAULT_OLLAMA_URL).rstrip('/')
    timeout = timeout or float(os.getenv('VENUE_EMBED_TIMEOUT_SECONDS', '60'))
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(f'{base_url}/api/embed', json={'model': model, 'input': list(texts), 'truncate': True})
    except httpx.HTTPError as exc:
        raise EmbeddingUnavailable(f'Could not reach Ollama at {base_url} ({exc.__class__.__name__}). '
                                   f'Start Ollama and run: ollama pull {model}') from exc
    finally:
        if owns:
            client.close()
    if response.status_code == 404 or (response.status_code >= 400 and 'not found' in response.text.lower()):
        raise EmbeddingUnavailable(f'The embedding model {model} is not installed. Run: ollama pull {model}')
    if response.status_code >= 400:
        raise EmbeddingUnavailable(f'Ollama embedding failed with HTTP {response.status_code}: {response.text[:200]}')
    vectors = (response.json() or {}).get('embeddings') or []
    if len(vectors) != len(texts):
        raise EmbeddingUnavailable('Ollama returned a different number of embeddings than texts sent.')
    out = [_unit([float(x) for x in v]) for v in vectors]
    if any(v is None for v in out):
        raise EmbeddingUnavailable('Ollama returned an empty embedding.')
    return out


def _embedder():
    """The function used to embed texts. Tests replace it."""
    return ollama_embed


# ---------------------------------------------------------------------------
# Stored vectors
# ---------------------------------------------------------------------------

def manuscript_vector(manuscript, *, embed=None):
    from ..models import ManuscriptEmbedding
    model = embed_model()
    query_prefix, _doc = _prefixes(model)
    text = manuscript_text(manuscript)
    if not text:
        raise EmbeddingUnavailable('The manuscript has no title, abstract or keywords to compare.')
    digest = _hash(model, text)
    stored = ManuscriptEmbedding.objects.filter(manuscript=manuscript).first()
    if stored and stored.text_hash == digest and stored.model == model and stored.vector:
        return stored.vector
    vector = (embed or _embedder())([query_prefix + text], model=model)[0]
    ManuscriptEmbedding.objects.update_or_create(manuscript=manuscript,
                                                 defaults={'model': model, 'text_hash': digest, 'vector': vector})
    return vector


def ensure_venue_vectors(venues, *, embed=None, time_limit=None, say=lambda m: None):
    """Make sure each venue has a current vector. Returns {venue_id: vector} for those that do.
    Only venues whose text (or the model) changed are sent to the model."""
    from ..models import VenueEmbedding
    model = embed_model()
    _query, doc_prefix = _prefixes(model)
    embed = embed or _embedder()
    deadline = time.monotonic() + (time_limit if time_limit is not None
                                   else _env_int('VENUE_EMBED_TIME_LIMIT_SECONDS', 120, 5, 3600))
    stored = {e.venue_id: e for e in VenueEmbedding.objects.filter(venue__in=[v.id for v in venues])}
    configs = active_configs(venues)
    vectors, todo = {}, []
    for venue in venues:
        text = venue_text(venue, configs.get(venue.id))
        digest = _hash(model, text)
        current = stored.get(venue.id)
        if current and current.text_hash == digest and current.model == model and current.vector:
            vectors[venue.id] = current.vector
        elif text:
            todo.append((venue, text, digest))
    for start in range(0, len(todo), BATCH):
        if time.monotonic() > deadline:
            say(f'Time limit reached; {len(todo) - start} venues are embedded next time.')
            break
        chunk = todo[start:start + BATCH]
        new = embed([doc_prefix + text for _v, text, _d in chunk], model=model)
        for (venue, _text, digest), vector in zip(chunk, new):
            VenueEmbedding.objects.update_or_create(venue=venue, defaults={'model': model, 'text_hash': digest,
                                                                           'vector': vector})
            vectors[venue.id] = vector
        say(f'Embedded {min(start + BATCH, len(todo))} of {len(todo)} venues.')
    return vectors


# ---------------------------------------------------------------------------
# The shortlist
# ---------------------------------------------------------------------------

def _manuscript_terms(manuscript):
    from ..match_score import _profile_text, _terms
    return _terms(' '.join([manuscript.title or '', manuscript.abstract or '',
                            ' '.join(map(str, manuscript.keywords or [])), _profile_text(manuscript)]))


def _keyword_similarity(ms, venue, config):
    """Fallback when embeddings are unavailable: share of the manuscript's topic terms the venue uses."""
    from ..match_score import _terms
    vt = _terms(venue_text(venue, config))
    if not ms or not vt:
        return 0.0
    return round(len(ms & vt) / min(len(ms), 40), 4)


def build_shortlist(manuscript, venues, *, size=None, embed=None, prefer=()):
    """Rank venues by topical closeness to the manuscript and keep the top `size`.

    Returns (kept_venues, info). info = {'method', 'considered', 'kept', 'size', 'similarity': {id: float},
    'rank': {id: int}, 'note'}. With `size` or fewer venues everything is kept (still ranked).
    On equal similarity, venues in `prefer` (already matched) stay ahead of newcomers."""
    size = size or shortlist_size()
    venues = list(venues)
    info = {'method': 'embedding', 'considered': len(venues), 'size': size, 'note': ''}
    similarity = {}
    if venues and embeddings_enabled():
        try:
            query = manuscript_vector(manuscript, embed=embed)
            vectors = ensure_venue_vectors(venues, embed=embed)
            if len(vectors) < len(venues):
                raise EmbeddingUnavailable(f'{len(venues) - len(vectors)} venues are not embedded yet.')
            similarity = {v.id: round(dot(query, vectors[v.id]), 4) for v in venues}
        except EmbeddingUnavailable as exc:
            logger.warning('Topical shortlist falls back to keywords: %s', exc)
            info['note'] = str(exc)
            similarity = {}
    if venues and not similarity:
        info['method'] = 'keywords'
        ms_terms, configs = _manuscript_terms(manuscript), active_configs(venues)
        similarity = {v.id: _keyword_similarity(ms_terms, v, configs.get(v.id)) for v in venues}
    prefer = set(prefer)
    ranked = sorted(venues, key=lambda v: (-similarity.get(v.id, 0.0), v.id not in prefer, v.name.lower()))
    kept = ranked[:size]
    info.update(kept=len(kept), similarity=similarity, rank={v.id: i for i, v in enumerate(ranked, 1)})
    if not venues:
        info['method'] = 'none'
    return kept, info


def shortlist_summary(info):
    """What the author and the API see (no vectors)."""
    return {'method': info['method'], 'considered': info['considered'], 'kept': info.get('kept', 0),
            'size': info['size'], 'note': info.get('note', '')}
