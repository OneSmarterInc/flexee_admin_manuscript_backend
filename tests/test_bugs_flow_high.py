"""Flow review: the states a real editor or admin cannot get out of.

Each test below fails on main at bfc4a06. They are about the journey, not the code: every one of them
is a screen where the only buttons on offer are refused, and nothing on the page leads anywhere else.
"""
import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import (
    EditorUser,
    Manuscript,
    Membership,
    Organization,
    Venue,
    VenueClaim,
    VenueIndexRun,
    VenueSubmission,
)
from tests.test_venue_claims import CLAIM, admin, claim, listed, outbox, post, token_from  # noqa: F401


# F1: one killed worker disabled every Venue Index button ------------------------------------------

@pytest.mark.django_db
def test_the_index_page_clears_a_run_a_killed_worker_left_behind():
    """A run left at 'processing' made the panel read 'Import running…' and disabled all five run
    buttons. close_stale_runs() existed, but its only caller was start_index_run — reachable only
    through those now-disabled buttons. The discovery page already clears its own stale runs on load;
    the index page did not, so the admin had no way out of the panel at all."""
    VenueIndexRun.objects.create(
        status='processing', mode='full', trigger='manual',
        started_at=timezone.now() - timedelta(hours=3),
    )
    response = admin().get('/api/admin/venue-index/')
    assert response.status_code == 200, response.content
    last_run = response.json().get('last_run') or {}
    assert last_run.get('status') == 'failed', (
        'the stale run still reads as live, so every run button stays disabled')
    assert 'Interrupted' in (last_run.get('summary') or '')


@pytest.mark.django_db
def test_the_index_page_leaves_a_run_that_may_still_be_working_alone():
    """The protection itself has to survive: a run started a minute ago is not stale."""
    VenueIndexRun.objects.create(
        status='processing', mode='full', trigger='manual',
        started_at=timezone.now() - timedelta(minutes=1),
    )
    response = admin().get('/api/admin/venue-index/')
    assert response.status_code == 200
    assert (response.json().get('last_run') or {}).get('status') == 'processing'


# F2: start review on a packet whose contents were deleted -----------------------------------------

def _packet_ready_for_review(**extra):
    org = Organization.objects.create(name='Retention editorial office')
    venue = Venue.objects.create(organization=org, name='Journal of Retention', slug='jor',
                                 venue_type='journal')
    manuscript = Manuscript.objects.create(
        author_name='A. Author', author_email='author@example.org', title='A manuscript',
        manuscript_type='research_article', abstract='Short.', keywords=['retention'],
        disclosure='None.')
    submission = VenueSubmission.objects.create(
        manuscript=manuscript, venue=venue, status='submitted',
        submitted_at=timezone.now() - timedelta(days=200), **extra)
    return org, venue, submission


@pytest.mark.django_db
def test_an_editor_cannot_start_review_on_a_packet_whose_contents_expired():
    """The retention sweep purges a packet that is still 'submitted': the packet, the editorial brief
    and every requirement file are wiped. The workspace still offered Start review, and the backend
    accepted it — moving the empty packet to 'under_review', where it is then permanently exempt from
    the sweep, with downloads disabled and no brief. The editor could only decide blind."""
    org, _venue, submission = _packet_ready_for_review(retention_purged_at=timezone.now())
    editor = EditorUser.objects.create(email='editor@jor.example', password_hash=hash_password('x' * 14))
    Membership.objects.create(user=editor, organization=org, role='owner')
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(editor.email)[0]

    started = post(client, f'/api/admin/venue-submissions/{submission.id}/start-review/', {})
    assert started.status_code == 409, started.content
    submission.refresh_from_db()
    assert submission.status == 'submitted', 'an expired packet was moved into review'


@pytest.mark.django_db
def test_an_editor_can_still_start_review_on_a_packet_that_is_there():
    org, _venue, submission = _packet_ready_for_review()
    editor = EditorUser.objects.create(email='editor2@jor.example', password_hash=hash_password('x' * 14))
    Membership.objects.create(user=editor, organization=org, role='owner')
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(editor.email)[0]

    started = post(client, f'/api/admin/venue-submissions/{submission.id}/start-review/', {})
    assert started.status_code == 200, started.content
    submission.refresh_from_db()
    assert submission.status == 'under_review'


# F3: an editor who misses the 7-day set-password link ---------------------------------------------

@pytest.mark.django_db
def test_a_superuser_can_send_an_editor_a_fresh_set_password_link(outbox):  # noqa: F811
    """The approval email carries one signed link that works for 7 days. After that
    editor_set_password answers 'This link has expired. Ask Flexee for a new one.' — and there was
    nothing to ask: an approved claim renders no buttons, re-approving is 409, rejecting is 409, and a
    fresh claim is refused because the journal is now claimed. Authors have Forgot password; editors
    had no route at all. Only a Django shell could let that editor in."""
    record = listed()
    assert claim(record).status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    pending = VenueClaim.objects.get(email='asha@jisp.example.org')
    root = admin()
    assert post(root, f'/api/admin/claims/{pending.id}/approve/', {}).status_code == 200
    pending.refresh_from_db()
    editor = pending.editor_user
    assert editor is not None and editor.password_hash == '!unset'
    outbox.clear()

    again = post(root, f'/api/admin/claims/{pending.id}/resend-access/', {})
    assert again.status_code == 200, again.content
    assert outbox and 'token=' in outbox[-1]['body'], 'no fresh link was sent'
    assert outbox[-1]['to'] == pending.email
    # The new link works, which is the whole point.
    token = token_from(outbox[-1])
    set_password = post(Client(), '/api/admin/set-password/',
                        {'token': token, 'password': 'y' * 16, 'confirm': 'y' * 16})
    assert set_password.status_code == 200, set_password.content


@pytest.mark.django_db
def test_only_a_superuser_can_resend_an_editor_access_link(outbox):  # noqa: F811
    record = listed()
    assert claim(record).status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    pending = VenueClaim.objects.get(email='asha@jisp.example.org')
    assert post(admin(), f'/api/admin/claims/{pending.id}/approve/', {}).status_code == 200

    plain = admin(email='not-root@example.com', superuser=False)
    refused = post(plain, f'/api/admin/claims/{pending.id}/resend-access/', {})
    assert refused.status_code == 403, refused.content


# F4: a journal could only ever have one editor ----------------------------------------------------

@pytest.mark.django_db
def test_a_superuser_can_add_a_second_editor_to_a_journal_that_is_already_claimed(outbox):  # noqa: F811
    """Membership rows are created in one place only — claim approval — and there is no add-member
    endpoint or panel anywhere in the app. So the 409 a second editor got ('Ask its editorial office to
    add you') and the 409 the superuser got ('ask the current editor to add this person') both named an
    action no code implements. A journal whose sole editor left was stranded the same way: the venue
    stays claimed, new claims are refused, and re-approval is refused by the old approved claim.

    The fix keeps the protection — approval never silently moves a journal between organizations — and
    adds the one thing the messages already promised: a superuser can approve a claim as an additional
    editor of the organization that holds the journal."""
    record = listed()
    assert claim(record).status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    first = VenueClaim.objects.get(email='asha@jisp.example.org')
    root = admin()
    assert post(root, f'/api/admin/claims/{first.id}/approve/', {}).status_code == 200
    first.refresh_from_db()
    venue, org = first.venue, first.venue.organization
    venue.trust_tier = Venue.TIER_CLAIMED       # the first editor saved the journal's rules
    venue.save(update_fields=['trust_tier'])

    # A second editor at the same journal claims it.
    assert claim(record, email='ben@jisp.example.org').status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    second = VenueClaim.objects.get(email='ben@jisp.example.org')

    refused = post(root, f'/api/admin/claims/{second.id}/approve/', {})
    assert refused.status_code == 409, 'a quiet hand-over is still refused'
    assert refused.json().get('code') == 'already_managed'

    added = post(root, f'/api/admin/claims/{second.id}/approve/', {'add_to_existing': True})
    assert added.status_code == 200, added.content
    second.refresh_from_db()
    assert second.status == 'approved'
    assert Organization.objects.count() == 1, 'the journal was moved to a second organization'
    venue.refresh_from_db()
    assert venue.organization_id == org.id
    assert Membership.objects.filter(user=second.editor_user, organization=org).exists()
    # The first editor keeps the journal and stays an owner.
    assert Membership.objects.filter(user=first.editor_user, organization=org, role='owner').exists()


@pytest.mark.django_db
def test_adding_an_editor_needs_a_journal_that_is_really_held(outbox):  # noqa: F811
    """add_to_existing is not a way around the ordinary checks: a journal no organization holds has
    nobody to be added to, and the claim follows the normal path."""
    record = listed()
    assert claim(record).status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    only = VenueClaim.objects.get(email='asha@jisp.example.org')
    added = post(admin(), f'/api/admin/claims/{only.id}/approve/', {'add_to_existing': True})
    assert added.status_code == 200, added.content
    only.refresh_from_db()
    assert only.status == 'approved'
    assert Membership.objects.filter(user=only.editor_user, role='owner').exists()


@pytest.mark.django_db
def test_an_editor_added_to_a_journal_is_told_how_to_sign_in(outbox):  # noqa: F811
    record = listed()
    assert claim(record).status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    first = VenueClaim.objects.get(email='asha@jisp.example.org')
    root = admin()
    post(root, f'/api/admin/claims/{first.id}/approve/', {})
    first.refresh_from_db()
    first.venue.trust_tier = Venue.TIER_CLAIMED
    first.venue.save(update_fields=['trust_tier'])

    assert claim(record, email='ben@jisp.example.org').status_code == 201
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    second = VenueClaim.objects.get(email='ben@jisp.example.org')
    outbox.clear()
    assert post(root, f'/api/admin/claims/{second.id}/approve/', {'add_to_existing': True}).status_code == 200
    assert outbox, 'the new editor was never told'
    assert 'token=' in outbox[-1]['body'] and outbox[-1]['to'] == 'ben@jisp.example.org'


# F6: there was no way to stop an index run -------------------------------------------------------

@pytest.mark.django_db
def test_an_admin_can_stop_an_index_run_that_is_in_progress():
    """The discovery panel has had a Stop button all along; the index panel had none. Before the
    one-hour staleness cutoff an admin whose worker died had nothing at all to press."""
    run = VenueIndexRun.objects.create(status='processing', mode='full', trigger='manual',
                                       started_at=timezone.now() - timedelta(minutes=2))
    stopped = post(admin(), '/api/admin/venue-index/stop/', {})
    assert stopped.status_code == 200, stopped.content
    run.refresh_from_db()
    assert run.status == 'failed'
    assert 'Stopped by' in (run.summary or '')
    assert 'kept' in (run.summary or ''), 'the admin is not told the saved records survive'


@pytest.mark.django_db
def test_stopping_an_index_run_when_none_is_running_says_so():
    refused = post(admin(), '/api/admin/venue-index/stop/', {})
    assert refused.status_code == 409, refused.content


@pytest.mark.django_db
def test_only_a_superuser_can_stop_an_index_run():
    VenueIndexRun.objects.create(status='processing', mode='full', trigger='manual',
                                 started_at=timezone.now() - timedelta(minutes=2))
    refused = post(admin(email='plain@example.com', superuser=False), '/api/admin/venue-index/stop/', {})
    assert refused.status_code == 403, refused.content
