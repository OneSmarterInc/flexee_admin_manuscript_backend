import copy
from types import SimpleNamespace
import pytest
from tests.test_schedule_and_match_score import make_manuscript, make_config
from review.match_score import estimated_match_score, semantic_score_signature

@pytest.fixture
def match(db):
    m = make_manuscript(title='Language model evaluation', abstract='We benchmark language models for information retrieval.',
        keywords=['language models'], parsed_profile={'semantic': {'evidence_points': [
        {'id': 'M001', 'text': 'We benchmark language models for information retrieval.'}]}})
    c = make_config(aims_scope='Artificial intelligence and information retrieval research.')
    item = {'rating': 3, 'text': 'The research directly evaluates information retrieval.',
        'venue_fields': ['aims_scope'], 'venue_quote': 'information retrieval research',
        'manuscript_evidence_ids': ['M001'], 'manuscript_quote': 'benchmark language models for information retrieval'}
    rubric = {'application_context_only': False, 'research_contribution': item,
              'subject_alignment': copy.deepcopy(item)}
    return SimpleNamespace(manuscript=m, venue_config=c, topic_similarity=None,
        semantic_cache={'score_signature': semantic_score_signature(m,c), 'data': {'subject_fit_rubric': rubric}})

def test_grounded_rubric(match):
    result=estimated_match_score(match)
    assert result['basis']=='semantic_rubric' and result['score']==75

@pytest.mark.parametrize('change', ['quote','rating','stale','missing','scope'])
def test_invalid_or_stale_rubric_stays_preliminary(match, change):
    rubric=match.semantic_cache['data']['subject_fit_rubric']
    if change=='quote':rubric['research_contribution']['manuscript_quote']='fabricated evidence text'
    if change=='rating':rubric['research_contribution']['rating']=99
    if change=='stale':match.manuscript.abstract='A changed manuscript abstract'
    if change=='missing':del rubric['subject_alignment']
    if change=='scope':match.venue_config.aims_scope='Changed scope'
    assert estimated_match_score(match)['basis']=='preliminary'

def test_application_context_alone_cannot_be_strong_fit(match):
    match.semantic_cache['data']['subject_fit_rubric']['application_context_only']=True
    assert estimated_match_score(match)['score']==49


@pytest.mark.parametrize('data', [None, [], 'invalid cached response'])
def test_malformed_cache_falls_back_without_breaking_matches(match, data):
    match.semantic_cache['data'] = data
    assert estimated_match_score(match)['basis'] == 'preliminary'
