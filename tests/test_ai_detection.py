import uuid
from unittest.mock import Mock
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from review.models import Manuscript, ManuscriptAIDetection
from review.services import ai_detection as ai
from tests.test_author_dashboard_actions import author_client, make_manuscript, env
from tests.test_manuscript_versions import upload

pytestmark = pytest.mark.django_db

@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def prepared():
    client, author = author_client()
    m = make_manuscript(author)
    r = ManuscriptAIDetection.objects.create(version=m.current_version, sha256=m.manuscript_sha256)
    return client, m, r


def test_real_upload_only_enqueues_after_commit(monkeypatch, django_capture_on_commit_callbacks):
    client, _ = author_client()
    monkeypatch.setenv('AI_DETECTOR_ENABLED','true')
    queue = Mock()
    monkeypatch.setattr('django_q.tasks.async_task', queue)
    monkeypatch.setattr(ai,'invoke_detector',Mock(side_effect=AssertionError('Must not execute on upload')))
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post('/api/author/manuscripts/', {'title':'Test manuscript','author':'Priya',
            'manuscript_type':'research_article','disclosure':'None','attestation':'true',
            'manuscript':SimpleUploadedFile('paper.md',b'Example manuscript text.')})
        assert response.status_code == 201
        queue.assert_not_called()
    assert queue.call_count == 1
    assert queue.call_args.kwargs['cluster'] == 'flexee_detector'
    assert queue.call_args.kwargs['sync'] is False
    m = Manuscript.objects.get(pk=response.json()['manuscript']['id'])
    assert ai.detection_payload(m)['ai_status'] == 'PENDING'
    with django_capture_on_commit_callbacks(execute=True):
        ai.enqueue_detection(m)
    assert queue.call_count == 1


def test_queue_failure_does_not_fail_upload(monkeypatch,django_capture_on_commit_callbacks):
    client, m, r = prepared()
    r.delete()
    monkeypatch.setenv('AI_DETECTOR_ENABLED','true')
    monkeypatch.setattr('django_q.tasks.async_task',Mock(side_effect=RuntimeError('offline')))
    with django_capture_on_commit_callbacks(execute=True):
        ai.enqueue_detection(m)
    assert ai.detection_payload(m)['ai_status'] == 'FAILED'


def test_saved_result_and_private_api(monkeypatch):
    client,m,r=prepared()
    result={'status':'estimated','ai_pattern_score':14.5,'label':'Leans human-written','coverage':'sampled',
            'model':'Gradient','passages':[{'text_excerpt':'private text'}]}
    invoke=Mock(return_value=result)
    monkeypatch.setattr(ai,'invoke_detector',invoke)
    ai.run_detection(r.pk,str(r.token))
    ai.run_detection(r.pk,str(r.token))
    invoke.assert_called_once()
    payload=client.get(f'/api/author/manuscripts/{m.pk}/').json()['manuscript']
    assert payload['ai_score']==14.5 and payload['ai_status']=='COMPLETED'
    assert payload['ai_analysis_label']=='Likely human-written'
    r.refresh_from_db()
    assert 'text_excerpt' not in r.result['passages'][0]
    other,_=author_client('other@example.com')
    assert other.get(f'/api/author/manuscripts/{m.pk}/').status_code in (401,403,404)


@pytest.mark.parametrize('score',[None,True,float('nan'),-1,101,'50'])
def test_invalid_score_never_published(monkeypatch,score):
    _,m,r=prepared()
    monkeypatch.setattr(ai,'invoke_detector',lambda v:{'status':'estimated','ai_pattern_score':score})
    ai.run_detection(r.pk,str(r.token))
    assert ai.detection_payload(m)['ai_status']=='FAILED'
    assert ai.detection_payload(m)['ai_score'] is None


def test_insufficient_text_is_not_zero(monkeypatch):
    _,m,r=prepared()
    monkeypatch.setattr(ai,'invoke_detector',lambda v:{'status':'not_checked','reason':'Under 100 words'})
    ai.run_detection(r.pk,str(r.token))
    assert ai.detection_payload(m)['ai_status']=='NOT_CHECKED'
    assert ai.detection_payload(m)['ai_score'] is None


def test_timeout_does_not_modify_readiness(monkeypatch):
    import subprocess
    _,m,r=prepared()
    monkeypatch.setattr(ai,'invoke_detector',Mock(side_effect=subprocess.TimeoutExpired('detector',600)))
    before=list(m.current_readiness().values())
    ai.run_detection(r.pk,str(r.token))
    assert ai.detection_payload(m)['ai_status']=='FAILED'
    assert list(m.current_readiness().values())==before


def test_old_version_result_cannot_replace_new(monkeypatch,django_capture_on_commit_callbacks):
    client,m,r=prepared()
    monkeypatch.setenv('AI_DETECTOR_ENABLED','true')
    queue=Mock()
    monkeypatch.setattr('django_q.tasks.async_task',queue)
    with django_capture_on_commit_callbacks(execute=True):
        response=upload(client,m,name='revised.md')
    assert response.status_code==201
    m.refresh_from_db()
    monkeypatch.setattr(ai,'invoke_detector',lambda v:{'status':'estimated','ai_pattern_score':80,'label':'Leans AI-generated'})
    ai.run_detection(r.pk,str(r.token))
    assert ai.detection_payload(m)['ai_status']=='PENDING'
    assert ai.detection_payload(m)['ai_score'] is None
    r.refresh_from_db()
    assert r.ai_score==80


def test_stale_token_cannot_write(monkeypatch):
    _,m,r=prepared()
    new=uuid.uuid4()
    def invoke(version):
        ManuscriptAIDetection.objects.filter(pk=r.pk).update(token=new,ai_status='PENDING')
        return {'status':'estimated','ai_pattern_score':99}
    monkeypatch.setattr(ai,'invoke_detector',invoke)
    ai.run_detection(r.pk,str(r.token))
    r.refresh_from_db()
    assert r.token==new and r.ai_status=='PENDING' and r.ai_score is None


def test_purged_content_not_scored(monkeypatch):
    from django.utils import timezone
    _,m,r=prepared()
    m.content_purged_at=timezone.now();m.save()
    invoke=Mock()
    monkeypatch.setattr(ai,'invoke_detector',invoke)
    ai.run_detection(r.pk,str(r.token))
    invoke.assert_not_called()
    assert ai.detection_payload(m)['ai_score'] is None


def test_replaced_content_resets_result(monkeypatch,django_capture_on_commit_callbacks):
    from review.manuscript_versions import sync_current_version
    _,m,r=prepared()
    r.ai_status='COMPLETED';r.ai_score=33;r.save()
    m.manuscript_sha256='b'*64;m.save();sync_current_version(m)
    monkeypatch.setenv('AI_DETECTOR_ENABLED','true')
    queue=Mock();monkeypatch.setattr('django_q.tasks.async_task',queue)
    old_token=r.token
    with django_capture_on_commit_callbacks(execute=True):
        ai.enqueue_detection(m)
    r.refresh_from_db()
    assert r.ai_status=='PENDING' and r.ai_score is None and r.token!=old_token
    queue.assert_called_once()


def test_disabled_detector_does_not_queue(monkeypatch,django_capture_on_commit_callbacks):
    _,m,r=prepared();r.delete()
    monkeypatch.setenv('AI_DETECTOR_ENABLED','false')
    queue=Mock();monkeypatch.setattr('django_q.tasks.async_task',queue)
    with django_capture_on_commit_callbacks(execute=True):
        ai.enqueue_detection(m)
    queue.assert_not_called()
    assert not ManuscriptAIDetection.objects.exists()


def test_version_purge_removes_derived_result():
    from review.manuscript_versions import purge_version_file
    from django.utils import timezone
    _,m,r=prepared()
    purge_version_file(m.current_version,timezone.now())
    assert not ManuscriptAIDetection.objects.filter(pk=r.pk).exists()


def test_extraction_docx_pdf_txt(tmp_path):
    from docx import Document
    from pypdf import PdfWriter
    from review.services.ai_detector_runtime.runner import extract
    txt=tmp_path/'text.txt';txt.write_text('Some English prose',encoding='utf-32')
    assert extract(txt)[0]=='Some English prose'
    doc=Document();doc.add_paragraph('Main manuscript paragraph')
    path=tmp_path/'document.docx';doc.save(path)
    assert 'Main manuscript paragraph' in extract(path)[0]
    pdf=tmp_path/'scanned.pdf'
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(pdf)
    text,notes=extract(pdf)
    assert not text.strip() and any('OCR' in n for n in notes)
    other=tmp_path/'bundle.zip';other.write_bytes(b'not read')
    assert extract(other)[0] is None
