import pytest
from review.models import ReadinessAssessment, ReviewJob
from review.services import author_agents as a
from tests.test_schedule_and_match_score import make_manuscript

@pytest.fixture
def prepared(monkeypatch):
    m=make_manuscript()
    ReadinessAssessment.objects.create(manuscript=m,status='completed',engine_version='mechanical-v1',
        summary={'ready_for_matching':True},findings=[])
    m.refresh_from_db()
    monkeypatch.setattr(a,'load_manuscript_text',lambda m:'First section\nSecond section')
    monkeypatch.setattr(a,'_chunk_numbered_lines',lambda text:[
        {'index':1,'start_line':1,'end_line':1,'text':text.splitlines()[0]},
        {'index':2,'start_line':2,'end_line':2,'text':text.splitlines()[1]}])
    monkeypatch.setattr(a,'ai_available',lambda:True)
    monkeypatch.setattr(a,'_citation_checks',lambda *x,**kw:{'checked':0,'results':[]})
    return m

@pytest.mark.django_db
def test_retry_only_failed_sections_and_records_progress(prepared,monkeypatch):
    calls=[]
    def partial(prompt,**kw):
        calls.append(prompt)
        if 'CHUNK 2' in prompt: raise ValueError('invalid JSON')
        return 'test-model',{'summary':'First section','topics':['AI']}
    monkeypatch.setattr(a,'_agent_json',partial)
    job=ReviewJob.objects.create(job_type='semantic_readiness',reference_id=str(prepared.pk),status='processing')
    prepared._readiness_job_id=job.pk
    first=a.run_semantic_readiness(prepared)
    assert first.summary['chunks_analyzed']==1
    job.refresh_from_db();assert job.progress==50
    calls.clear()
    monkeypatch.setattr(a,'_agent_json',lambda prompt,**kw:(calls.append(prompt) or ('test-model',{'summary':'Second section'})))
    second=a.run_semantic_readiness(prepared)
    assert len(calls)==1 and 'CHUNK 2' in calls[0]
    assert second.summary['coverage']['complete'] is True
    assert second.summary['chunks_analyzed']==2

@pytest.mark.django_db
def test_interruption_keeps_checkpoint_and_changed_metadata_invalidates(prepared,monkeypatch):
    calls=[]
    def interrupted(prompt,**kw):
        calls.append(prompt)
        if 'CHUNK 2' in prompt: raise KeyboardInterrupt()
        return 'test-model',{'summary':'First section'}
    monkeypatch.setattr(a,'_agent_json',interrupted)
    with pytest.raises(KeyboardInterrupt): a.run_semantic_readiness(prepared)
    assert prepared.current_readiness().filter(engine_version__contains='semantic-readiness').first().summary['chunks_analyzed']==1
    prepared.title='Changed title'
    calls.clear()
    monkeypatch.setattr(a,'_agent_json',lambda prompt,**kw:(calls.append(prompt) or ('test-model',{'summary':'Checked'})))
    a.run_semantic_readiness(prepared)
    assert len(calls)==2

@pytest.mark.django_db
def test_chunk_limit_advances_on_retry(prepared,monkeypatch):
    monkeypatch.setenv('AUTHOR_AGENT_MAX_CHUNKS','1')
    calls=[]
    monkeypatch.setattr(a,'_agent_json',lambda prompt,**kw:(calls.append(prompt) or ('test-model',{'summary':'Checked'})))
    first=a.run_semantic_readiness(prepared)
    assert first.summary['chunks_not_attempted']==1
    second=a.run_semantic_readiness(prepared)
    assert second.summary['coverage']['complete'] is True
    assert len(calls)==2 and 'CHUNK 2' in calls[1]
