"""Build plan step 9: an editor claims a journal; an admin approves; the editor's own save confirms it."""
import json
import re

import pytest
from django.test import Client

from review import claims_api
from review.auth import COOKIE_NAME, issue_session, verify_password
from review.models import EditorUser, IndexedVenue, Membership, Organization, Venue, VenueClaim
from tests.test_author_dashboard_actions import make_venue
from tests.test_index_rules import journal


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(claims_api, '_send', lambda to, subject, body: sent.append({'to': to, 'subject': subject,
                                                                                     'body': body}) or {'sent': True})
    return sent


def post(client, url, data):
    return client.post(url, data=json.dumps(data), content_type='application/json')


def admin(email='root@example.com', superuser=True):
    user = EditorUser.objects.filter(email=email).first() or EditorUser.objects.create(
        email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


def listed(**extra):
    return journal('Journal of Information Systems Practice', openalex_id='S9', homepage='https://jisp.example.org/home',
                   **extra)


CLAIM = {'name': 'Dr Asha Rao', 'role_title': 'Managing editor', 'evidence_url': 'https://jisp.example.org/board',
         'confirm': True}


def claim(record, email='asha@jisp.example.org', **extra):
    return post(Client(), '/api/journals/claim/', {'kind': 'indexed', 'key': str(record.id), 'email': email,
                                                   **CLAIM, **extra})


def token_from(mail):
    return re.search(r'token=([^\s]+)', mail['body']).group(1)


# ---------------------------------------------------------------------------
# Claiming and confirming the email
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_claim_sends_a_confirmation_link_and_checks_the_domain(outbox):
    record = listed()
    response = claim(record)
    assert response.status_code == 201 and response.json()['status'] == 'received'
    item = VenueClaim.objects.get()
    assert item.status == 'pending_email'
    assert item.domain_matches and item.indexed_id == record.id and item.journal_url == 'https://jisp.example.org/home'
    assert outbox[0]['to'] == 'asha@jisp.example.org' and '/journals/claim/verify?token=' in outbox[0]['body']

    claim(record, email='asha@gmail.com')
    assert not VenueClaim.objects.get(email='asha@gmail.com').domain_matches


@pytest.mark.django_db
def test_confirming_the_email_puts_the_claim_in_review(outbox):
    claim(listed())
    response = post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[0])})
    assert response.status_code == 200 and response.json()['status'] == 'pending_review'
    item = VenueClaim.objects.get()
    assert item.email_verified_at and item.status == 'pending_review'
    assert post(Client(), '/api/journals/claim/verify/', {'token': 'forged'}).status_code == 400


@pytest.mark.django_db
def test_claiming_again_answers_the_same_and_resends_only_spaced_and_capped(outbox):
    from datetime import timedelta
    from django.utils import timezone
    record = listed()
    first = claim(record).json()
    again = claim(record)
    assert again.status_code == 201 and again.json() == first  # no way to tell a repeat from a new claim
    assert VenueClaim.objects.count() == 1 and len(outbox) == 1  # too soon to re-send
    for _ in range(6):
        VenueClaim.objects.update(last_sent_at=timezone.now() - timedelta(minutes=11))
        claim(record)
    assert len(outbox) == claims_api.MAX_SENDS


@pytest.mark.django_db
def test_names_go_into_emails_on_one_line(outbox):
    claim(listed(), name='Asha\n\nURGENT: verify at https://evil.example', role_title='Editor\r\nX')
    item = VenueClaim.objects.get()
    assert '\n' not in item.name and '\r' not in item.role_title
    assert 'Hello Asha URGENT' in outbox[0]['body']


@pytest.mark.django_db
@pytest.mark.parametrize('payload,status', [
    ({'email': 'not-an-email'}, 400),
    ({'confirm': False}, 400),
    ({'evidence_url': 'javascript:alert(1)'}, 400),
])
def test_claim_validation(outbox, payload, status):
    assert claim(listed(), **payload).status_code == status
    assert not VenueClaim.objects.exists()


@pytest.mark.django_db
def test_claimed_excluded_and_undecided_journals_cannot_be_claimed(outbox):
    make_venue('Field Notes Journal', 'fnj')  # already editor-managed
    response = post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'fnj', 'email': 'a@b.org', **CLAIM})
    assert response.status_code == 409
    assert claim(listed(excluded=True)).status_code == 404
    flagged = journal('Flagged', openalex_id='S10', screening_status='flagged')
    assert claim(flagged).status_code == 404


@pytest.mark.django_db
def test_claims_are_rate_limited(outbox, monkeypatch):
    monkeypatch.setenv('VENUE_CLAIMS_PER_HOUR', '2')
    record = listed()
    assert claim(record, email='a@x.org').status_code == 201
    assert claim(record, email='b@x.org').status_code == 201
    assert claim(record, email='c@x.org').status_code == 429


# ---------------------------------------------------------------------------
# Review and approval
# ---------------------------------------------------------------------------

def verified_claim(outbox, record=None, email='asha@jisp.example.org'):
    record = record or listed()
    claim(record, email=email)
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    return VenueClaim.objects.get(email=email)


@pytest.mark.django_db
def test_approving_a_listed_journal_creates_an_owned_venue_and_an_invite(outbox):
    item = verified_claim(outbox)
    response = post(admin(), f'/api/admin/claims/{item.id}/approve/', {'note': 'Board page checked.'})
    assert response.status_code == 200 and response.json()['new_account']
    venue = Venue.objects.get()
    assert venue.trust_tier == 'listed' and IndexedVenue.objects.get().venue_id == venue.id  # not yet editor-confirmed
    user = EditorUser.objects.get(email='asha@jisp.example.org')
    assert not user.platform_superuser and not verify_password('anything', user.password_hash)
    assert Membership.objects.get(user=user).role == 'owner' and venue.organization.name.endswith('editorial office')
    invite = outbox[-1]
    assert 'You now manage' in invite['subject'] and '/admin/set-password?token=' in invite['body']

    # The set-password link works once.
    token = token_from(invite)
    weak = post(Client(), '/api/admin/set-password/', {'token': token, 'password': 'short', 'confirm': 'short'})
    assert weak.status_code == 400
    ok = post(Client(), '/api/admin/set-password/', {'token': token, 'password': 'a-long-secret-1', 'confirm': 'a-long-secret-1'})
    assert ok.status_code == 200
    user.refresh_from_db()
    assert verify_password('a-long-secret-1', user.password_hash)
    again = post(Client(), '/api/admin/set-password/', {'token': token, 'password': 'another-secret-2', 'confirm': 'another-secret-2'})
    assert again.status_code == 400


@pytest.mark.django_db
def test_claiming_one_journal_never_reaches_the_publishers_other_journals(outbox):
    venue = make_venue('Journal of Applied AI', 'jaai')
    sibling = make_venue('Another Flexee Journal', 'afj')  # same 'Flexee Publishing' organization
    Venue.objects.filter(id=venue.id).update(trust_tier='verified_index', source_urls=['https://jaai.example/'])
    claim_ = post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'jaai', 'email': 'ed@jaai.example', **CLAIM})
    assert claim_.status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    item = VenueClaim.objects.get()
    assert item.domain_matches
    assert post(admin(), f'/api/admin/claims/{item.id}/approve/', {}).status_code == 200
    venue.refresh_from_db(); sibling.refresh_from_db()
    assert venue.organization_id != sibling.organization_id
    user = EditorUser.objects.get(email='ed@jaai.example')
    assert list(Membership.objects.filter(user=user).values_list('organization_id', flat=True)) == [venue.organization_id]
    assert venue.trust_tier == 'verified_index'  # stays until the editor saves their own rules


@pytest.mark.django_db
def test_the_editors_own_save_makes_it_editor_confirmed_but_an_admin_save_does_not(outbox):
    venue = make_venue('Journal of Applied AI', 'jaai')
    Venue.objects.filter(id=venue.id).update(trust_tier='verified_index')
    root = admin()
    rules = {'aims_scope': 'AI in operations.', 'article_types': ['Research article']}
    assert post(root, f'/api/admin/venues/{venue.id}/config/', rules).status_code in (200, 201)
    venue.refresh_from_db()
    assert venue.trust_tier == 'verified_index'

    post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'jaai', 'email': 'ed@jaai.example', **CLAIM})
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    post(root, f'/api/admin/claims/{VenueClaim.objects.get().id}/approve/', {})
    editor = admin('ed@jaai.example', superuser=False)
    assert post(editor, f'/api/admin/venues/{venue.id}/config/', rules).status_code in (200, 201)
    venue.refresh_from_db()
    assert venue.trust_tier == 'claimed' and venue.last_verified_at


@pytest.mark.django_db
def test_only_confirmed_claims_can_be_approved_and_rejection_is_recorded(outbox):
    record = listed()
    claim(record)
    pending = VenueClaim.objects.get()
    root = admin()
    assert post(root, f'/api/admin/claims/{pending.id}/approve/', {}).status_code == 409
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    response = post(root, f'/api/admin/claims/{pending.id}/reject/', {'note': 'Not listed on the board page.'})
    assert response.status_code == 200 and response.json()['claim']['status'] == 'rejected'
    assert 'not approved' in outbox[-1]['body'] and 'Not listed on the board page.' in outbox[-1]['body']
    assert post(root, f'/api/admin/claims/{pending.id}/reject/', {}).status_code == 409
    assert not Venue.objects.exists()


@pytest.mark.django_db
def test_admin_list_and_permissions(outbox):
    item = verified_claim(outbox)
    body = admin().get('/api/admin/claims/?status=pending_review').json()
    assert [c['id'] for c in body['claims']] == [str(item.id)] and body['counts']['pending_review'] == 1
    assert body['claims'][0]['checks']['domain_matches'] is True
    assert Client().get('/api/admin/claims/').status_code == 401
    owner = admin('owner@example.com', superuser=False)
    Membership.objects.create(user=EditorUser.objects.get(email='owner@example.com'),
                              organization=Organization.objects.create(name='Org'), role='owner')
    assert owner.get('/api/admin/claims/').status_code == 403
    assert post(owner, f'/api/admin/claims/{item.id}/approve/', {}).status_code == 403


@pytest.mark.django_db
def test_a_second_claim_cannot_take_a_journal_from_its_editor(outbox):
    record = listed()
    first = verified_claim(outbox, record, email='asha@jisp.example.org')
    second = verified_claim(outbox, record, email='intruder@example.com')
    root = admin()
    assert post(root, f'/api/admin/claims/{first.id}/approve/', {}).status_code == 200
    response = post(root, f'/api/admin/claims/{second.id}/approve/', {})
    assert response.status_code == 409 and 'already managed' in response.json()['detail']
    assert not EditorUser.objects.filter(email='intruder@example.com').exists()


@pytest.mark.django_db
def test_admin_edits_never_renew_an_outside_editors_confirmation(outbox):
    from datetime import timedelta
    from django.utils import timezone
    venue = make_venue('Journal of Applied AI', 'jaai')
    Venue.objects.filter(id=venue.id).update(trust_tier='verified_index')
    post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'jaai', 'email': 'ed@jaai.example', **CLAIM})
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    root = admin()
    post(root, f'/api/admin/claims/{VenueClaim.objects.get().id}/approve/', {})
    rules = {'aims_scope': 'AI in operations.', 'article_types': ['Research article']}
    post(admin('ed@jaai.example', superuser=False), f'/api/admin/venues/{venue.id}/config/', rules)
    old = timezone.now() - timedelta(days=40)
    Venue.objects.filter(id=venue.id).update(last_verified_at=old)
    assert post(root, f'/api/admin/venues/{venue.id}/config/', rules).status_code in (200, 201)
    venue.refresh_from_db()
    assert venue.trust_tier == 'claimed' and venue.last_verified_at == old
