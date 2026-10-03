import hashlib
import json

import pytest
from django.core.files.base import ContentFile
from django.test import Client

from review.auth import AUTHOR_COOKIE_NAME, hash_password, issue_author_session
from review.models import (
    Author, Manuscript, Organization, ReadinessAssessment, Venue, VenueAgentConfig, VenueMatch,
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'dash-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def author_client(email='priya@example.com'):
    author = Author.objects.create(email=email, name='Priya', password_hash=hash_password('pass12345'), email_verified=True)
    client = Client()
    client.cookies[AUTHOR_COOKIE_NAME] = issue_author_session(author.id)[0]
    return client, author


def make_venue(name, slug, types=('Practitioner article',)):
    org, _ = Organization.objects.get_or_create(name='Flexee Publishing')
    venue = Venue.objects.create(organization=org, name=name, slug=slug, venue_type='journal')
    VenueAgentConfig.objects.create(venue=venue, version=1, active=True,
                                    aims_scope='AI in manufacturing quality.', article_types=list(types))
    return venue


def make_manuscript(author, ready=True):
    data = b'# AI quality inspection\n\nSome text.\n'
    m = Manuscript(author_account=author, author_name='Priya', author_email=author.email, title='AI inspection',
                   abstract='Quality inspection.', keywords=['quality'], manuscript_type='practitioner_article',
                   disclosure='None', manuscript_filename='paper.md', manuscript_bytes=len(data),
                   manuscript_sha256=hashlib.sha256(data).hexdigest())
    m.manuscript_file.save('paper.md', ContentFile(data), save=False)
    m.save()
    ReadinessAssessment.objects.create(manuscript=m, status='completed', summary={'ready_for_matching': ready, 'word_count': 600})
    return m


@pytest.mark.django_db
def test_generate_matches_unchanged_after_refactor():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    make_venue('Book Press', 'bp', types=('Book manuscript',))
    m = make_manuscript(author)
    body = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()
    assert body['matching_stage'] == 'deterministic_policy_gate_v1'
    by_name = {item['venue']['name']: item for item in body['matches']}
    assert by_name['Field Notes Journal']['eligibility'] == 'eligible'
    assert by_name['Book Press']['eligibility'] == 'needs_changes'


@pytest.mark.django_db
def test_new_admin_venue_appears_for_author_and_is_marked_new():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    client.post(f'/api/author/manuscripts/{m.id}/matches/seen/')

    make_venue('Journal of Applied AI', 'jaai')  # admin adds a venue later

    listing = client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]
    assert listing['match_count'] == 2 and listing['new_match_count'] == 1 and listing['has_file'] is True

    matches = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert {x['venue']['name']: x['is_new'] for x in matches} == {'Field Notes Journal': False, 'Journal of Applied AI': True}

    client.post(f'/api/author/manuscripts/{m.id}/matches/seen/')
    assert client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]['new_match_count'] == 0


@pytest.mark.django_db
def test_unmatched_or_not_ready_manuscripts_are_not_auto_matched():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    never_matched = make_manuscript(author)
    listing = client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]
    assert listing['match_count'] == 0 and VenueMatch.objects.filter(manuscript=never_matched).count() == 0

    blocked = make_manuscript(author, ready=False)
    VenueMatch.objects.create(manuscript=blocked, venue=Venue.objects.get(slug='fnj'), eligibility='needs_changes')
    make_venue('Another', 'another')
    client.get('/api/author/manuscripts/list/')
    assert VenueMatch.objects.filter(manuscript=blocked).count() == 1


@pytest.mark.django_db
def test_author_can_view_and_download_only_their_own_file():
    client, author = author_client()
    m = make_manuscript(author)
    response = client.get(f'/api/author/manuscripts/{m.id}/file/')
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'# AI quality inspection\n\nSome text.\n'
    assert 'attachment' in response['Content-Disposition']

    other, _ = author_client('other@example.com')
    assert other.get(f'/api/author/manuscripts/{m.id}/file/').status_code in (401, 403)
    assert Client().get(f'/api/author/manuscripts/{m.id}/file/').status_code == 401
    assert other.post(f'/api/author/manuscripts/{m.id}/matches/seen/').status_code in (401, 403)


@pytest.mark.django_db
def test_purged_file_is_gone():
    from django.utils import timezone
    client, author = author_client()
    m = make_manuscript(author)
    Manuscript.objects.filter(id=m.id).update(content_purged_at=timezone.now())
    assert client.get(f'/api/author/manuscripts/{m.id}/file/').status_code == 410
    assert client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]['has_file'] is False
