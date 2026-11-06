import pytest
from unittest.mock import Mock
from review.models import VenueMatch
from review.services import author_agents as a
from review.services.local_llm import request_context_size
from tests.test_schedule_and_match_score import make_config, make_manuscript

@pytest.mark.django_db
def test_matching_cache_reuses_exact_inputs_and_invalidates(monkeypatch):
    m=make_manuscript(); c=make_config()
    match=VenueMatch.objects.create(manuscript=m,venue=c.venue,venue_config=c)
    monkeypatch.setattr(a,'_match_prompt',lambda m,*args:m.title+c.aims_scope)
    monkeypatch.setattr(a,'ai_available',lambda:True)
    call=Mock(return_value=('test-model',{'reasons':[],'gaps':[]}))
    monkeypatch.setattr(a,'_agent_json',call)
    first=a._cached_match_call(m,match,c)
    match.refresh_from_db()
    assert a._cached_match_call(m,match,c)==first
    assert call.call_count==1
    m.title+=' revised'
    a._cached_match_call(m,match,c)
    assert call.call_count==2
    match.refresh_from_db(); monkeypatch.setenv('OLLAMA_MODEL','changed-model')
    a._cached_match_call(m,match,c)
    assert call.call_count==3
    match.refresh_from_db(); c.aims_scope+=' changed rules'
    a._cached_match_call(m,match,c)
    assert call.call_count==4

@pytest.mark.django_db
def test_failed_call_is_not_cached(monkeypatch):
    m=make_manuscript(); c=make_config()
    match=VenueMatch.objects.create(manuscript=m,venue=c.venue,venue_config=c)
    monkeypatch.setattr(a,'_match_prompt',lambda *args:'prompt')
    monkeypatch.setattr(a,'ai_available',lambda:True)
    monkeypatch.setattr(a,'_agent_json',Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(RuntimeError): a._cached_match_call(m,match,c)
    match.refresh_from_db(); assert match.semantic_cache=={}

@pytest.mark.django_db
def test_version_purge_clears_cache():
    from review.manuscript_versions import purge_version_file
    from django.utils import timezone
    m=make_manuscript(); c=make_config()
    match=VenueMatch.objects.create(manuscript=m,venue=c.venue,semantic_cache={'data':{'reasons':['private']}})
    purge_version_file(match.version,timezone.now())
    match.refresh_from_db(); assert match.semantic_cache=={}

def test_context_sizing_keeps_headroom_and_ceiling(monkeypatch):
    monkeypatch.setenv('OLLAMA_ADAPTIVE_CONTEXT','true')
    assert request_context_size('x'*1000,16384,700)==4096
    assert request_context_size('x'*12000,16384,700)==8192
    assert request_context_size('x'*30000,16384,700)==16384
    assert request_context_size('x'*1000,2048,700)==2048
    monkeypatch.setenv('OLLAMA_ADAPTIVE_CONTEXT','false')
    assert request_context_size('x',16384,700)==16384
