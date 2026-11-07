import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client, RequestFactory
from django.utils import timezone
from docx import Document
from pypdf import PdfWriter

from review.models import Author, Manuscript, ReadinessAssessment, EvidenceFinding
from review.services import author_agents as a
from review.services.document_processing import extract_document
from review.tasks import sweep_retention_task
from review.author_api import _analysis_limit
from tests.test_retention_policy import retention_env
from tests.test_security_audit_retention import _expired_manuscript


@pytest.mark.django_db(transaction=True)
def test_retention_during_analysis_does_not_restore_content(monkeypatch, retention_env):
    m = _expired_manuscript()
    ReadinessAssessment.objects.create(manuscript=m, status='completed', engine_version='mechanical-v1', summary={'ready_for_matching': True})
    monkeypatch.setattr(a, 'load_manuscript_text', lambda m: 'Confidential unpublished result.')
    monkeypatch.setattr(a, 'ai_available', lambda: True)
    monkeypatch.setattr(a, '_agent_json', lambda *args, **kw: ('mock', {'summary': 'Confidential unpublished result.'}))
    def purge(*args, **kw):
        sweep_retention_task()
        return {'checked': 0, 'results': []}
    monkeypatch.setattr(a, '_citation_checks', purge)
    with pytest.raises(a.AgentInputError):
        a.run_semantic_readiness(m)
    m.refresh_from_db()
    assert m.content_purged_at and m.parsed_profile == {}
    assert not ReadinessAssessment.objects.filter(manuscript=m).exists()
    assert not EvidenceFinding.objects.filter(manuscript=m).exists()


@pytest.mark.django_db
def test_checkpoint_refuses_purged_or_replaced_manuscript(retention_env):
    m = _expired_manuscript()
    row = ReadinessAssessment.objects.create(manuscript=m, engine_version='test')
    Manuscript.objects.filter(pk=m.pk).update(content_purged_at=timezone.now())
    with pytest.raises(a.AgentInputError):
        a._save_readiness_checkpoint(m, row, {'results': {'1': 'private'}}, 1)
    row.refresh_from_db()
    assert not row.summary


@pytest.mark.django_db
def test_analysis_limit_shared_across_manuscripts_and_ips(monkeypatch):
    monkeypatch.setenv('AUTHOR_ANALYSIS_PER_MINUTE', '2')
    factory = RequestFactory()
    m1 = SimpleNamespace(author_account_id='same-author', pk='one')
    m2 = SimpleNamespace(author_account_id='same-author', pk='two')
    assert _analysis_limit(factory.post('/', REMOTE_ADDR='1.1.1.1'), m1) is None
    assert _analysis_limit(factory.post('/', REMOTE_ADDR='2.2.2.2'), m2) is None
    limited = _analysis_limit(factory.post('/', REMOTE_ADDR='3.3.3.3'), m1)
    assert limited.status_code == 429 and int(limited['Retry-After']) > 0
    assert _analysis_limit(factory.post('/'), SimpleNamespace(author_account_id='other', pk='three')) is None


def docx_bytes():
    doc = Document()
    doc.add_paragraph('Abstract')
    doc.add_paragraph('A normal manuscript remains readable.')
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


def test_bounded_parser_preserves_docx_text():
    assert extract_document(docx_bytes(), 'paper.docx') == 'Abstract\nA normal manuscript remains readable.'


def test_bounded_parser_rejects_pdf_page_limit(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    stream = BytesIO(); writer.write(stream)
    monkeypatch.setenv('DOCUMENT_MAX_PAGES', '1')
    with pytest.raises(ValueError, match='safe processing limits'):
        extract_document(stream.getvalue(), 'paper.pdf')


def test_parser_timeout_has_safe_error():
    import subprocess
    with patch('review.services.document_processing.subprocess.run', side_effect=subprocess.TimeoutExpired('private-path', 60)):
        with pytest.raises(ValueError, match='took too long'):
            extract_document(docx_bytes(), 'paper.docx')


def test_parser_rejects_oversized_text(monkeypatch):
    monkeypatch.setenv('DOCUMENT_MAX_TEXT_CHARS', '1000')
    with pytest.raises(ValueError, match='extracted-text limit'):
        extract_document(b'x' * 1001, 'paper.md')
