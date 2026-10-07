"""Build plan step 5: a local embedding model shortlists venues by topic before any AI is used."""
import hashlib
import json
import math
import re

import httpx
import pytest
from django.core.management import call_command

from review.models import ManuscriptEmbedding, Venue, VenueAgentConfig, VenueEmbedding, VenueMatch
from review.services import venue_shortlist as vs
from tests.test_author_dashboard_actions import author_client, make_manuscript, make_venue

DIM = 64


def fake_vector(text):
    """Bag of words hashed into 64 buckets: texts sharing words point the same way."""
    v = [0.0] * DIM
    for word in re.findall(r'[a-z]{4,}', text.lower()):
        if word in {'search', 'query', 'document', 'aims', 'scope', 'article', 'types', 'journal'}:
            continue
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeEmbedder:
    def __init__(self):
        self.calls, self.texts = 0, []

    def __call__(self, texts, model=None):
        self.calls += 1
        self.texts += list(texts)
        return [fake_vector(t) for t in texts]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv('VENUE_EMBED_ENABLED', 'true')
    embedder = FakeEmbedder()
    monkeypatch.setattr(vs, '_embedder', lambda: embedder)
    return embedder


def scoped_venue(name, slug, scope):
    venue = make_venue(name, slug)
    VenueAgentConfig.objects.filter(venue=venue).update(aims_scope=scope)
    return venue


TOPICS = [
    ('Quality Inspection Review', 'qir', 'Machine vision for quality inspection of manufactured parts and defects.'),
    ('Marine Biology Letters', 'mbl', 'Coral reefs, plankton ecology and ocean fisheries.'),
    ('Medieval History Quarterly', 'mhq', 'Monasteries, crusades and feudal law in medieval Europe.'),
    ('Accounting Horizons', 'ach', 'Auditing standards, taxation and financial reporting.'),
    ('Poetry Today', 'ptd', 'Contemporary verse, sonnets and literary criticism.'),
]


def inspection_manuscript(author):
    m = make_manuscript(author)
    m.title = 'Machine vision for quality inspection'
    m.abstract = 'We detect defects in manufactured parts with machine vision quality inspection.'
    m.keywords = ['quality inspection', 'machine vision', 'defects']
    m.save()
    return m


# ---------------------------------------------------------------------------
# The shortlist itself
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_closest_venues_by_topic_are_kept(fake, monkeypatch):
    monkeypatch.setenv('VENUE_SHORTLIST_SIZE', '5')  # the minimum
    venues = [scoped_venue(*t) for t in TOPICS] + [scoped_venue(f'Filler {i}', f'f{i}', 'Gardening and cooking.')
                                                   for i in range(3)]
    _client, author = author_client()
    kept, info = vs.build_shortlist(inspection_manuscript(author), venues)
    assert info['method'] == 'embedding' and info['considered'] == 8 and info['kept'] == 5
    assert kept[0].name == 'Quality Inspection Review'
    assert info['rank'][kept[0].id] == 1 and info['similarity'][kept[0].id] > 0.5


@pytest.mark.django_db
def test_vectors_are_reused_until_the_text_changes(fake):
    venues = [scoped_venue(*t) for t in TOPICS]
    _client, author = author_client()
    m = inspection_manuscript(author)
    vs.build_shortlist(m, venues)
    assert VenueEmbedding.objects.count() == 5 and ManuscriptEmbedding.objects.count() == 1
    calls = fake.calls
    vs.build_shortlist(m, venues)
    assert fake.calls == calls  # nothing re-embedded

    VenueAgentConfig.objects.filter(venue=venues[1]).update(aims_scope='Now about quality inspection too.')
    fake.texts.clear()
    vs.build_shortlist(m, venues)
    assert len(fake.texts) == 1 and 'quality inspection too' in fake.texts[0]  # only the changed venue


@pytest.mark.django_db
def test_nomic_task_prefixes_are_used(fake):
    venue = scoped_venue(*TOPICS[0])
    _client, author = author_client()
    vs.build_shortlist(inspection_manuscript(author), [venue])
    assert any(t.startswith('search_query: ') for t in fake.texts)
    assert any(t.startswith('search_document: Quality Inspection Review') for t in fake.texts)


@pytest.mark.django_db
def test_falls_back_to_keywords_when_the_model_is_unavailable(monkeypatch):
    monkeypatch.setenv('VENUE_EMBED_ENABLED', 'true')

    def down(texts, model=None):
        raise vs.EmbeddingUnavailable('Could not reach Ollama at http://127.0.0.1:11434.')
    monkeypatch.setattr(vs, '_embedder', lambda: down)
    venues = [scoped_venue(*t) for t in TOPICS]
    _client, author = author_client()
    kept, info = vs.build_shortlist(inspection_manuscript(author), venues, size=2)
    assert info['method'] == 'keywords' and 'Ollama' in info['note']
    assert kept[0].name == 'Quality Inspection Review' and len(kept) == 2


@pytest.mark.django_db
def test_ollama_embed_request_and_errors():
    seen = {}

    def handler(request):
        seen['url'], seen['body'] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={'embeddings': [[3.0, 4.0]]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert vs.ollama_embed(['hello'], model='nomic-embed-text', client=client) == [[0.6, 0.8]]  # unit length
    assert seen['url'].endswith('/api/embed') and seen['body'] == {'model': 'nomic-embed-text', 'input': ['hello'],
                                                                   'truncate': True}

    missing = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={'error': 'model "nomic-embed-text" not found, try pulling it first'})))
    with pytest.raises(vs.EmbeddingUnavailable, match='ollama pull nomic-embed-text'):
        vs.ollama_embed(['hello'], model='nomic-embed-text', client=missing)

    def refuse(request):
        raise httpx.ConnectError('refused')
    with pytest.raises(vs.EmbeddingUnavailable, match='Start Ollama'):
        vs.ollama_embed(['hello'], client=httpx.Client(transport=httpx.MockTransport(refuse)))


# ---------------------------------------------------------------------------
# Matching uses the shortlist
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_matching_only_gates_the_shortlist_and_orders_by_topic(fake, monkeypatch):
    monkeypatch.setenv('VENUE_SHORTLIST_SIZE', '5')
    for t in TOPICS:
        scoped_venue(*t)
    for i in range(4):
        scoped_venue(f'Filler {i}', f'f{i}', 'Gardening and cooking.')
    client, author = author_client()
    m = inspection_manuscript(author)
    body = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()
    assert body['shortlist'] == {'method': 'embedding', 'considered': 9, 'kept': 5, 'size': 5, 'note': ''}
    assert len(body['matches']) == 5 and VenueMatch.objects.count() == 5
    assert body['matches'][0]['venue']['name'] == 'Quality Inspection Review'
    assert body['matches'][0]['shortlist_rank'] == 1 and body['matches'][0]['topic_similarity'] > 0.5

    listed = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert [x['shortlist_rank'] for x in listed] == sorted(x['shortlist_rank'] for x in listed)


@pytest.mark.django_db
def test_small_catalogues_keep_every_venue():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    make_venue('Book Press', 'bp')
    m = make_manuscript(author)
    body = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()
    assert body['shortlist']['method'] == 'keywords' and body['shortlist']['kept'] == 2  # embeddings off in tests
    assert len(body['matches']) == 2


@pytest.mark.django_db
def test_rerun_drops_matches_that_left_the_shortlist(fake, monkeypatch):
    monkeypatch.setenv('VENUE_SHORTLIST_SIZE', '5')
    venues = [scoped_venue(*t) for t in TOPICS] + [scoped_venue('Filler', 'fil', 'Gardening and cooking.')]
    client, author = author_client()
    m = inspection_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    names = set(VenueMatch.objects.values_list('venue__name', flat=True))
    assert len(names) == 5
    # Another venue moves closer to the manuscript's topic; re-running replaces the furthest one.
    dropped = (set(v.name for v in venues) - names).pop()
    VenueAgentConfig.objects.filter(venue__name=dropped).update(
        aims_scope='Machine vision quality inspection of manufactured parts, defects and inspection.')
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    after = set(VenueMatch.objects.values_list('venue__name', flat=True))
    assert dropped in after and len(after) == 5


@pytest.mark.django_db
def test_new_venue_is_added_only_if_it_makes_the_shortlist(fake, monkeypatch):
    monkeypatch.setenv('VENUE_SHORTLIST_SIZE', '5')
    for t in TOPICS:
        scoped_venue(*t)
    client, author = author_client()
    m = inspection_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    scoped_venue('Garden Monthly', 'gm', 'Gardening and cooking.')        # unrelated: stays out
    assert len(client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']) == 5
    scoped_venue('Vision Systems', 'vsy', 'Machine vision quality inspection of defects in manufactured parts.')
    names = [x['venue']['name'] for x in client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']]
    assert 'Vision Systems' in names and 'Garden Monthly' not in names


@pytest.mark.django_db
def test_semantic_ai_stage_only_sees_shortlisted_venues(fake, monkeypatch):
    from review.services import author_agents
    monkeypatch.setenv('VENUE_SHORTLIST_SIZE', '5')
    for t in TOPICS:
        scoped_venue(*t)
    for i in range(5):
        scoped_venue(f'Filler {i}', f'f{i}', 'Gardening and cooking.')
    client, author = author_client()
    m = inspection_manuscript(author)
    m.parsed_profile = {'semantic': {'summary': 'Machine vision inspection.', 'topics': ['quality inspection']}}
    m.save()
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    prompts = []
    monkeypatch.setattr(author_agents, '_agent_json', lambda prompt, **kw: (prompts.append(prompt) or
                                                                           ('mock', {'fit_summary': 'ok'})))
    monkeypatch.setattr(author_agents, 'ai_available', lambda: True)
    author_agents.run_semantic_matching(m)
    assert len(prompts) == 5  # 10 venues, 5 reasoned over


@pytest.mark.django_db
def test_deleting_a_manuscript_deletes_its_vector(fake):
    venue = scoped_venue(*TOPICS[0])
    _client, author = author_client()
    m = inspection_manuscript(author)
    vs.build_shortlist(m, [venue])
    assert ManuscriptEmbedding.objects.filter(manuscript=m).exists()
    m.delete()
    assert not ManuscriptEmbedding.objects.exists()


@pytest.mark.django_db
def test_embed_venues_command(fake, monkeypatch, capsys):
    monkeypatch.setattr(vs, 'ollama_embed', lambda texts, model=None, **kw: fake(texts, model))
    for t in TOPICS:
        scoped_venue(*t)
    listed = scoped_venue('Spine Only', 'so', 'Quality.')
    Venue.objects.filter(id=listed.id).update(trust_tier='listed')  # never matched, never embedded
    call_command('embed_venues')
    out = capsys.readouterr().out
    assert '5 of 5 matchable venues have a current topic vector' in out
    assert VenueEmbedding.objects.count() == 5
