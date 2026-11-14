from types import SimpleNamespace
import pytest
from review.match_score import _terms, _profile_text, keyword_phrases, compute_match_score
from review.services import venue_shortlist as vs
from tests.test_schedule_and_match_score import make_config, make_manuscript

def test_acronyms_and_structured_profile_content():
    assert _terms('AI NLP RAG SLM LLM') >= {'artificial', 'intelligence', 'retrieval', 'language'}
    m=SimpleNamespace(parsed_profile={'semantic': {'contributions': [
        {'text': 'RAG benchmarking', 'line_start': 44},
        "{'text': 'Language model evaluation', 'line_start': 55}"]}})
    assert _profile_text(m) == 'RAG benchmarking Language model evaluation'
    assert keyword_phrases(['AI; RAG, NLP']) == ['AI','RAG','NLP']

@pytest.mark.django_db
def test_missing_scope_unknown_and_no_free_points():
    m=make_manuscript()
    c=make_config(aims_scope='')
    assert compute_match_score(m,c)['score'] is None
    c.aims_scope='Medieval manuscripts and archaeology'
    result=compute_match_score(m,c,eligibility='eligible')
    assert result['score'] < 15
    assert result['breakdown']['requirements'] == 0

@pytest.mark.django_db
def test_partial_embeddings_preserved_without_treating_keywords_as_cosines(monkeypatch):
    m=make_manuscript()
    c=make_config()
    other=make_config(slug='history',venue_name='History',aims_scope='Medieval history')
    monkeypatch.setenv('VENUE_EMBED_ENABLED','true')
    monkeypatch.setattr(vs,'manuscript_vector',lambda *a,**k:[1.,0.])
    monkeypatch.setattr(vs,'ensure_venue_vectors',lambda *a,**k:{c.venue_id:[0.7,0.7]})
    kept,info=vs.build_shortlist(m,[other.venue,c.venue])
    assert kept[0].pk == c.venue_id
    assert info['method']=='hybrid'
    assert info['embedding_similarity']=={c.venue_id:0.7}

@pytest.mark.django_db
def test_primary_ai_contribution_beats_application_context():
    m=make_manuscript(title='Comparative Evaluation of Small Language Models in Retrieval-Augmented Generation: A Case Study in Supply Chain Education',
        abstract='Benchmarking language models in a supply-chain education application.',
        keywords=['Small language models; Retrieval-augmented generation; Natural language processing'])
    ai=make_config(aims_scope='Artificial intelligence, intelligent information retrieval, machine learning and knowledge discovery.')
    supply=make_config(slug='supply',aims_scope='Supply chain management, operations, education and business practice.')
    unrelated=make_config(slug='law',aims_scope='Comparative law and systematic evaluation of legal institutions.')
    assert compute_match_score(m,ai)['score'] > compute_match_score(m,supply)['score']
    assert compute_match_score(m,unrelated)['score'] < 15

@pytest.mark.django_db
def test_high_embedding_similarity_does_not_make_unrelated_scope_good_fit():
    m=make_manuscript(title='AI retrieval augmented generation',abstract='Language model benchmarking',keywords=['RAG'])
    c=make_config(aims_scope='Microeconomics, taxation and accounting reviews.')
    assert compute_match_score(m,c,topic_similarity=0.9)['score'] <= 20

@pytest.mark.django_db
def test_ai_findings_need_nonempty_policy_and_matching_quotes():
    from review.services.author_agents import _grounded_match_items
    m=make_manuscript(parsed_profile={'semantic':{'evidence_points':[
        {'id':'M001','text':'We evaluate AI agents in manufacturing.', 'source':{'excerpt':'We evaluate AI agents in manufacturing.'}}]}})
    c=make_config(aims_scope='Applied artificial intelligence in manufacturing operations.')
    item={'text':'Possible relevant scope.', 'venue_fields':['aims_scope'],
          'manuscript_evidence_ids':['M001'],'venue_quote':'artificial intelligence in manufacturing',
          'manuscript_quote':'AI agents in manufacturing'}
    assert _grounded_match_items([item],c,m)
    assert not _grounded_match_items([{**item,'venue_fields':['reporting_standards']}],c,m)
    assert not _grounded_match_items([{**item,'manuscript_quote':'Fabricated passage not in the paper'}],c,m)
    assert not _grounded_match_items([{**item,'manuscript_evidence_ids':[]}],c,m)

def test_research_article_synonyms_preserve_other_types():
    from review.match_score import normalize_article_type
    assert normalize_article_type('Research Papers') == 'research_article'
    assert normalize_article_type('Conference paper') == 'conference_paper'

@pytest.mark.django_db
def test_shared_ai_is_not_enough_for_accounting_or_law():
    m=make_manuscript(title='Artificial intelligence and RAG benchmarking',abstract='We benchmark language models in education.',keywords=['AI'])
    c=make_config(aims_scope='Artificial intelligence and emerging technologies in accounting.')
    assert compute_match_score(m,c,topic_similarity=.9)['score'] <= 34
    c.aims_scope='Law and legal scholarship on artificial intelligence.'
    assert compute_match_score(m,c,topic_similarity=.9)['label']=='Discipline needs review'
    m.abstract='We examine legal implications of artificial intelligence and law.'
    assert compute_match_score(m,c,topic_similarity=.9)['score'] > 34


@pytest.mark.django_db
def test_abstract_and_contribution_outweigh_case_study_title():
    m=make_manuscript(title='Comparative Evaluation of SLMs: A Case Study on FLEXEE Supply Chain Manual',
        abstract='We benchmark small language models and retrieval augmented generation, measuring reasoning and information retrieval accuracy.',
        keywords=['SLMs', 'RAG', 'language model benchmarking'],
        parsed_profile={'semantic': {'topics': ['Supply chain management'] * 20,
            'contributions': ['Benchmark of small language models in retrieval augmented generation']}})
    ai=make_config(aims_scope='Artificial intelligence, natural language processing, information retrieval and machine learning.')
    supply=make_config(slug='supply-context',aims_scope='Supply chain management, operations, logistics and business education.')
    assert compute_match_score(m,ai)['score'] > compute_match_score(m,supply)['score']
    m.abstract='We study supply chain management, logistics, inventory and procurement operations.'
    m.keywords=['supply chain management', 'logistics', 'procurement']
    m.parsed_profile={'semantic': {'contributions': ['New procurement and inventory methods for supply chain management']}}
    assert compute_match_score(m,supply)['score'] > compute_match_score(m,ai)['score']


@pytest.mark.django_db
def test_quality_and_reviewer_text_cannot_inflate_topic_score():
    m = make_manuscript(title='AI language model benchmarking',
        abstract='We benchmark artificial intelligence for information retrieval.', keywords=['AI', 'RAG'])
    c = make_config(aims_scope='Medieval history and archaeological excavations.')
    baseline = compute_match_score(m, c)
    c.reviewer_criteria = ['Artificial intelligence and language model expertise']
    c.quality_threshold = 'Novel information retrieval and retrieval augmented generation performance'
    assert compute_match_score(m, c)['score'] == baseline['score']
    assert compute_match_score(m, c)['matched_terms'] == baseline['matched_terms']

@pytest.mark.django_db
@pytest.mark.parametrize('similarity', [float('nan'), float('inf'), float('-inf')])
def test_invalid_embedding_does_not_change_score(similarity):
    m, c = make_manuscript(), make_config()
    assert compute_match_score(m, c, topic_similarity=similarity)['score'] == compute_match_score(m, c)['score']

@pytest.mark.django_db
def test_complete_scope_and_literal_quotes_are_validated():
    from review.services.author_agents import _grounded_match_items
    m = make_manuscript(parsed_profile={'semantic': {'evidence_points': [
        {'id': 'M001', 'text': 'We evaluate language models in information retrieval.'}]}})
    c = make_config(aims_scope=('General background. ' * 70) + 'We welcome "information retrieval" research.')
    item = {'text': 'Relevant research contribution.', 'venue_fields': ['aims_scope'],
        'venue_quote': '"information retrieval" research',
        'manuscript_evidence_ids': ['M001'], 'manuscript_quote': 'language models in information retrieval'}
    assert _grounded_match_items([item], c, m)
    assert not _grounded_match_items([{**item, 'venue_quote': 'fabricated unrelated research'}], c, m)
