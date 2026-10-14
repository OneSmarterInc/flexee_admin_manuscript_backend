"""Bug review 2 (14 October), Low findings (backend). Each test fails on the code before its fix."""
import json
from unittest import mock

import pytest
from django.test import Client

from review import submission_plan as plans
from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import AIUsageEvent, EditorUser, Membership, SubmissionPlanStep, Venue, VenueSubmission
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_bugs_high import _claim_all, _venue_of
from tests.test_submission_plan import activate, generate, post, setup_plan, step


def post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type='application/json', **extra)


@pytest.mark.django_db
def test_r7_generation_racing_i_have_sent_it_never_abandons_a_plan_with_a_deciding_venue():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    real_check = plans.check_can_generate

    def check_then_author_marks_sent(manuscript):
        real_check(manuscript)
        plans.mark_submitted(plan['id'], first['id'])   # commits between the check and the lock

    with mock.patch.object(plans, 'check_can_generate', side_effect=check_then_author_marks_sent):
        try:
            plans.generate_plan(m)
        except plans.PlanError:
            pass
    s = SubmissionPlanStep.objects.select_related('plan').get(id=first['id'])
    assert not (s.state == 'submitted' and s.plan.status == 'abandoned'), \
        'the venue is deciding inside an abandoned plan: its outcome can never be recorded'


@pytest.mark.django_db
def test_shared_qwen_calls_are_not_counted_as_unpriced_cloud_calls():
    """ai_usage_snapshot excludes only ollama and mock; LOCAL_PROVIDERS also has shared_qwen."""
    from review.ai_usage import ai_usage_snapshot
    AIUsageEvent.objects.create(provider='shared_qwen', model='qwen2.5-shared', operation='semantic_readiness',
                                status='completed', priced=False)
    assert ai_usage_snapshot()['unpriced_cloud_calls_total'] == 0


@pytest.mark.django_db
def test_index_report_cloud_calls_count_only_calls_that_were_made():
    """escalation_stats counts every AIUsageEvent with the cloud operation, including 'blocked' (budget said
    no, nothing sent) and 'failed' rows, so the report overstates cloud use."""
    from review.services.rules_escalation import escalation_stats
    AIUsageEvent.objects.create(provider='anthropic', model='m', operation='index_rules_cloud', status='blocked',
                                priced=True)
    AIUsageEvent.objects.create(provider='anthropic', model='m', operation='index_rules_cloud', status='completed',
                                priced=True)
    assert escalation_stats()['cloud_calls'] == 1


def test_calls_read_at_discovery_or_rule_reading_use_the_still_open_anywhere_day(monkeypatch):
    """Bug M8 fixed check_calls only. candidate_from_raw -> apply_calls_for_papers -> detect_calls_for_papers
    (Venue Discovery and Venue Index rule reading) still default to timezone.localdate() (UTC): at 02:00 UTC
    on 16 March a call closing 15 March is still open in the Americas (deadline_today() == 15 March) but is
    dropped, and the venue loses its 'accepting' evidence."""
    from datetime import datetime, timezone as dt_tz
    from django.utils import timezone
    from review.services import venue_discovery as vd
    from review.services.freshness import deadline_today
    now = datetime(2027, 3, 16, 2, 0, tzinfo=dt_tz.utc)
    monkeypatch.setattr(timezone, 'now', lambda: now)
    assert deadline_today(now).isoformat() == '2027-03-15'
    page = vd.FetchedPage(url='https://ops.example/jdo/special-issues', title='Special issues',
                          text='Special Issue: AI in Supply Chain Resilience\nSubmission deadline: 15 March 2027')
    calls = vd.detect_calls_for_papers([page])
    assert [c['deadline'] for c in calls] == ['2027-03-15']


def superuser_client(email='root@example.com'):
    user = EditorUser.objects.create(email=email, password_hash=hash_password('root-password-123'),
                                     platform_superuser=True)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, user


@pytest.mark.django_db
def test_cors_preflight_allows_delete_used_by_the_admin_portal():
    response = Client().options('/api/admin/venue-index/blocked-publishers/00000000-0000-0000-0000-000000000000/',
                                HTTP_ORIGIN='http://localhost:5173',
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='DELETE')
    assert 'DELETE' in response.get('Access-Control-Allow-Methods', '')


@pytest.mark.django_db
def test_editor_feedback_cannot_reference_a_draft_submission():
    from review.manuscript_versions import create_initial_version
    _, author = author_client()
    venue = make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    if not m.current_version_id:
        create_initial_version(m)
    draft = VenueSubmission.objects.create(manuscript=m, venue=venue, status='draft', version=m.current_version)
    editor = EditorUser.objects.create(email='ed@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=venue.organization, role='editor')
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(editor.email)[0]
    # The draft is invisible in every editor list/detail view ...
    assert client.get(f'/api/admin/venue-submissions/{draft.id}/').status_code == 404
    # ... but feedback endpoint accepts it, confirming it exists and linking feedback/audit to it.
    response = post_json(client, f'/api/admin/venues/{venue.id}/feedback/',
                    {'assessment_field': 'aims_scope', 'venue_submission_id': str(draft.id), 'editor_value': 'x'})
    assert response.status_code == 404


@pytest.mark.django_db
def test_bad_uuid_filters_give_400_or_empty_not_500():
    client, _ = superuser_client()
    client.raise_request_exception = False
    assert client.get('/api/admin/venue-submissions/?venue_id=abc').status_code != 500
    assert client.get('/api/admin/audit-events/?organization_id=abc').status_code != 500


@pytest.mark.django_db
def test_an_older_packet_at_the_same_venue_does_not_decide_the_step():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    live = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{live.id}/submit/').status_code == 200
    older = VenueSubmission.objects.create(manuscript=m, venue=venue, status='rejected', version=m.current_version)
    plans.sync_from_submission(older)
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'submitted'


@pytest.mark.django_db
def test_a_second_answer_for_an_outside_packet_is_refused():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    packet = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    VenueSubmission.objects.filter(id=packet.id).update(status='rejected')  # another answer got there first
    r = step(client, plan, first, 'outcome', outcome='accepted', note='late')
    assert r.status_code == 409 and r.json()['code'] == 'already_decided'


@pytest.mark.django_db
def test_a_readiness_check_that_cannot_read_the_file_says_why():
    client, author = author_client()
    m = make_manuscript(author)
    with mock.patch('review.author_api.load_manuscript_text', side_effect=ValueError('File is not a zip file')):
        r = client.post(f'/api/author/manuscripts/{m.id}/readiness/run/')
    assert r.status_code == 422
    assert r.json()['detail'] == 'The readiness check could not read the manuscript file: File is not a zip file'
