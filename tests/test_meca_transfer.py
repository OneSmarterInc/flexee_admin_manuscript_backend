import hashlib
import json
import zipfile
from io import BytesIO
from xml.etree import ElementTree as ET

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from review.auth import issue_author_session
from review.models import (
    Author,
    EditorFeedback,
    EvidenceFinding,
    Manuscript,
    Organization,
    SubmissionTransfer,
    Venue,
    VenueSubmission,
)


MANIFEST_NS = 'https://manuscriptexchange.org/schema/manifest'
XLINK_NS = 'http://www.w3.org/1999/xlink'


@pytest.fixture
def transfer_context(tmp_path, settings, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'meca-test-session-secret')

    author = Author.objects.create(
        email='meca-author@example.com',
        password_hash='dummy',
        name='MECA Author',
        email_verified=True,
    )
    token, _ = issue_author_session(author.id)
    client = Client()
    client.cookies['flxee_author_session'] = token

    manuscript_content = b'# Transfer manuscript\n\nA manuscript body for MECA testing.'
    manuscript = Manuscript.objects.create(
        author_account=author,
        author_name='MECA Author',
        author_email=author.email,
        coauthors='Second Author',
        title='A transferable manuscript',
        manuscript_type='research_article',
        abstract='Transfer abstract',
        keywords=['transfer', 'meca'],
        disclosure='AI was used for copy editing.',
        manuscript_filename='paper.md',
        manuscript_file=SimpleUploadedFile(
            'paper.md',
            manuscript_content,
            content_type='text/markdown',
        ),
        manuscript_bytes=len(manuscript_content),
        manuscript_sha256=hashlib.sha256(manuscript_content).hexdigest(),
    )

    org = Organization.objects.create(
        name='MECA Publisher',
        organization_type='journal',
    )
    source_venue = Venue.objects.create(
        organization=org,
        name='Source Journal',
        slug='source-journal',
        venue_type='journal',
    )
    target_venue = Venue.objects.create(
        organization=org,
        name='Destination Journal',
        slug='destination-journal',
        venue_type='journal',
    )
    source = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=source_venue,
        status='rejected',
        packet={'manuscript_filename': 'paper.md'},
        editorial_brief={'editor_summary': 'Prior brief for the receiving editor.'},
        decision={
            'decision': 'rejected',
            'note': 'Scope mismatch at the original journal.',
            'decided_by': 'private-editor@example.com',
        },
    )
    EvidenceFinding.objects.create(
        manuscript=manuscript,
        venue_submission=source,
        finding_type='scope',
        claim='The manuscript was outside the original venue scope.',
        source_type='venue_policy',
        source_locator='source policy',
        verification={'status': 'verified'},
    )
    EditorFeedback.objects.create(
        venue=source_venue,
        venue_submission=source,
        assessment_field='scope_fit',
        editor_value={'fit': 'low'},
        reason='Editor confirmed the original scope mismatch.',
    )

    return {
        'client': client,
        'author': author,
        'manuscript': manuscript,
        'source': source,
        'target_venue': target_venue,
    }


def _transfer(ctx, *, share_review_history=None):
    data = {
        'venue_id': str(ctx['target_venue'].id),
        'reason': 'Author chose a new destination.',
    }
    if share_review_history is not None:
        data['share_review_history'] = share_review_history
    return ctx['client'].post(
        f"/api/author/venue-submissions/{ctx['source'].id}/transfer/",
        data=json.dumps(data),
        content_type='application/json',
    )


def _download_zip(response):
    payload = b''.join(response.streaming_content)
    return zipfile.ZipFile(BytesIO(payload))


def _manifest_hrefs(archive):
    root = ET.fromstring(archive.read('manifest.xml'))
    return {
        node.attrib[f'{{{XLINK_NS}}}href']
        for node in root.findall(f'.//{{{MANIFEST_NS}}}instance')
    }


@pytest.mark.django_db
def test_transfer_defaults_to_no_review_history_and_exports_minimal_meca(transfer_context):
    response = _transfer(transfer_context)

    assert response.status_code == 201, response.content
    payload = response.json()
    target_id = payload['submission']['id']
    transfer = SubmissionTransfer.objects.get(to_submission_id=target_id)

    assert transfer.share_review_history is False
    assert transfer.review_history_consented_at is None
    assert payload['transfer']['share_review_history'] is False
    assert payload['submission']['transfer']['id'] == str(transfer.id)

    download = transfer_context['client'].get(
        f'/api/author/venue-submissions/{target_id}/meca/'
    )
    assert download.status_code == 200
    assert download['Content-Type'] == 'application/zip'
    assert download['Cache-Control'] == 'private, no-store, max-age=0'
    assert download['X-MECA-Version'] == '2.0.1'
    assert download['X-Review-History-Included'] == 'false'
    assert f'{transfer.id}-meca.zip' in download['Content-Disposition']

    with _download_zip(download) as archive:
        names = set(archive.namelist())
        assert names == {'manifest.xml', 'transfer.xml', 'SourceFiles/paper.md'}
        assert _manifest_hrefs(archive) == {'transfer.xml', 'SourceFiles/paper.md'}
        transfer_xml = archive.read('transfer.xml').decode('utf-8')
        assert 'Source Journal' in transfer_xml
        assert 'Destination Journal' in transfer_xml
        assert 'Prior review-history sharing consent: no' in transfer_xml


@pytest.mark.django_db
def test_explicit_consent_includes_redacted_prior_review_history(transfer_context):
    response = _transfer(transfer_context, share_review_history=True)

    assert response.status_code == 201, response.content
    target_id = response.json()['submission']['id']
    transfer = SubmissionTransfer.objects.get(to_submission_id=target_id)

    assert transfer.share_review_history is True
    assert transfer.review_history_consented_at is not None
    assert response.json()['transfer']['review_history_consented_at']

    download = transfer_context['client'].get(
        f'/api/author/venue-submissions/{target_id}/meca/'
    )
    assert download.status_code == 200
    assert download['X-Review-History-Included'] == 'true'

    with _download_zip(download) as archive:
        names = set(archive.namelist())
        assert 'reviews.xml' in names
        assert _manifest_hrefs(archive) == {
            'transfer.xml',
            'reviews.xml',
            'SourceFiles/paper.md',
        }
        reviews = archive.read('reviews.xml').decode('utf-8')
        assert 'Prior brief for the receiving editor.' in reviews
        assert 'outside the original venue scope' in reviews
        assert 'Editor confirmed the original scope mismatch.' in reviews
        assert 'Scope mismatch at the original journal.' in reviews
        assert 'permission-to-transfer="yes"' in reviews
        assert 'private-editor@example.com' not in reviews


@pytest.mark.django_db
def test_false_value_never_counts_as_transfer_history_consent(transfer_context):
    response = _transfer(transfer_context, share_review_history=False)

    assert response.status_code == 201
    transfer = SubmissionTransfer.objects.get(
        to_submission_id=response.json()['submission']['id']
    )
    assert transfer.share_review_history is False
    assert transfer.review_history_consented_at is None


@pytest.mark.django_db
def test_meca_download_requires_author_access(transfer_context, monkeypatch):
    response = _transfer(transfer_context)
    target_id = response.json()['submission']['id']

    other = Author.objects.create(
        email='other-author@example.com',
        password_hash='dummy',
        name='Other Author',
        email_verified=True,
    )
    token, _ = issue_author_session(other.id)
    client = Client()
    client.cookies['flxee_author_session'] = token

    denied = client.get(f'/api/author/venue-submissions/{target_id}/meca/')
    assert denied.status_code == 401


@pytest.mark.django_db
def test_meca_export_fails_closed_after_manuscript_content_purge(transfer_context):
    response = _transfer(transfer_context, share_review_history=True)
    target_id = response.json()['submission']['id']

    manuscript = transfer_context['manuscript']
    manuscript.content_purged_at = timezone.now()
    manuscript.save(update_fields=['content_purged_at'])

    download = transfer_context['client'].get(
        f'/api/author/venue-submissions/{target_id}/meca/'
    )
    assert download.status_code == 410
    assert 'expired under the retention policy' in download.json()['detail']
