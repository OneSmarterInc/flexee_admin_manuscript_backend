import json
from datetime import datetime, timezone as dt_timezone

import pytest
from django.test import Client

from review.auth import COOKIE_NAME, issue_session
from review.models import AuditEvent, EditorUser, Manuscript, Membership, Organization, Venue, VenueAgentConfig


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'schedule-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def admin(superuser=True, email='root@example.com'):
    user = EditorUser.objects.create(email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, user


URL = '/api/admin/venue-discovery/schedule/'


# ---------------- schedule ----------------

def test_next_run_is_today_or_tomorrow_in_the_chosen_zone():
    from review.discovery_schedule import next_run_at
    now = datetime(2026, 10, 2, 10, 0, tzinfo=dt_timezone.utc)  # 15:30 in India
    later_today = next_run_at(18, 0, 'Asia/Kolkata', now=now)
    assert later_today.isoformat() == '2026-10-02T18:00:00+05:30'
    tomorrow = next_run_at(2, 0, 'Asia/Kolkata', now=now)
    assert tomorrow.isoformat() == '2026-10-03T02:00:00+05:30'


@pytest.mark.django_db
def test_admin_sets_moves_and_turns_off_the_daily_schedule():
    from django_q.models import Schedule
    client, _ = admin()
    assert client.get(URL).json()['schedule']['enabled'] is False

    body = client.post(URL, data=json.dumps({'enabled': True, 'time': '06:30', 'timezone': 'Asia/Kolkata'}),
                       content_type='application/json').json()['schedule']
    assert body['enabled'] is True and body['time'] == '06:30' and body['timezone'] == 'Asia/Kolkata'
    schedules = Schedule.objects.filter(func='review.tasks.run_venue_discovery_task')
    assert schedules.count() == 1 and schedules.first().schedule_type == Schedule.DAILY

    body = client.post(URL, data=json.dumps({'enabled': True, 'time': '21:15', 'timezone': 'Asia/Kolkata'}),
                       content_type='application/json').json()['schedule']
    assert body['time'] == '21:15' and Schedule.objects.filter(func='review.tasks.run_venue_discovery_task').count() == 1
    assert client.get(URL).json()['schedule']['time'] == '21:15'

    body = client.post(URL, data=json.dumps({'enabled': False}), content_type='application/json').json()['schedule']
    assert body['enabled'] is False and not Schedule.objects.filter(func='review.tasks.run_venue_discovery_task').exists()
    assert AuditEvent.objects.filter(action='venue_discovery.schedule_updated').count() == 3


@pytest.mark.django_db
def test_schedule_validation_and_permissions():
    client, _ = admin()
    bad_time = client.post(URL, data=json.dumps({'enabled': True, 'time': '25:00'}), content_type='application/json')
    assert bad_time.status_code == 400
    bad_zone = client.post(URL, data=json.dumps({'enabled': True, 'time': '02:00', 'timezone': 'Mars/Base'}),
                           content_type='application/json')
    assert bad_zone.status_code == 400
    editor_client, editor = admin(superuser=False, email='editor@example.com')
    Membership.objects.create(user=editor, organization=Organization.objects.create(name='O'), role='owner')
    assert editor_client.get(URL).status_code == 403
    assert Client().get(URL).status_code == 401


@pytest.mark.django_db
def test_scheduled_task_accepts_the_time_zone_kwarg(monkeypatch):
    from review.tasks import run_venue_discovery_task
    monkeypatch.setenv('VENUE_DISCOVERY_ENABLED', 'false')
    assert run_venue_discovery_task(schedule_tz='Asia/Kolkata')  # returns the run id; disabled run fails cleanly


# ---------------- match score ----------------

def make_config(**fields):
    org = Organization.objects.create(name='Org')
    venue = Venue.objects.create(organization=org, name=fields.pop('venue_name', 'Journal of Operations AI'),
                                 slug=fields.pop('slug', 'ops-ai'), venue_type='journal')
    defaults = {'aims_scope': 'Applied artificial intelligence in manufacturing operations, quality inspection and '
                              'production systems.', 'article_types': ['Practitioner article', 'Research article']}
    defaults.update(fields)
    return VenueAgentConfig.objects.create(venue=venue, version=1, active=True, **defaults)


def make_manuscript(**fields):
    defaults = {'title': 'AI quality inspection on a mid-size production line', 'author_name': 'A',
                'abstract': 'We report deploying computer vision for quality inspection in manufacturing operations.',
                'keywords': ['quality inspection', 'manufacturing'], 'manuscript_type': 'practitioner_article',
                'disclosure': 'None.'}
    defaults.update(fields)
    return Manuscript.objects.create(**defaults)


@pytest.mark.django_db
def test_strong_fit_scores_high():
    from review.match_score import compute_match_score
    result = compute_match_score(make_manuscript(), make_config(), eligibility='eligible')
    assert result['score'] >= 75 and result['label'] == 'Strong fit'
    assert result['breakdown']['type'] == 25 and result['breakdown']['requirements'] == 20
    assert 'inspection' in result['matched_terms']


@pytest.mark.django_db
def test_wrong_type_and_unrelated_scope_score_low():
    from review.match_score import compute_match_score
    config = make_config(aims_scope='Medieval European history and archival studies.', article_types=['Book manuscript'])
    result = compute_match_score(make_manuscript(), config)
    assert result['breakdown']['type'] == 0 and result['breakdown']['scope'] <= 8
    assert result['score'] < 15 and result['label'] == 'Low fit'  # few rules do not make an unrelated venue fit


@pytest.mark.django_db
def test_failed_desk_rule_caps_the_score():
    from review.match_score import compute_match_score
    config = make_config(structured_desk_rejection_rules=[{'field': 'word_count', 'operator': '>', 'value': 100,
                                                            'message': 'Too long'}])
    result = compute_match_score(make_manuscript(), config, eligibility='ineligible', violations=1)
    assert result['breakdown']['requirements'] == 0 and result['score'] <= 30


@pytest.mark.django_db
def test_missing_config_and_unconfigured_types():
    from review.match_score import compute_match_score
    assert compute_match_score(make_manuscript(), None)['score'] == 0
    partial = compute_match_score(make_manuscript(), make_config(article_types=[]))
    assert partial['breakdown']['type'] == 10


@pytest.mark.django_db
def test_match_payload_includes_score():
    from review.author_api import _match_payload
    from review.models import VenueMatch
    config = make_config()
    match = VenueMatch.objects.create(manuscript=make_manuscript(), venue=config.venue, venue_config=config,
                                      eligibility='eligible')
    payload = _match_payload(match)
    assert set(payload['match_score']) == {'score', 'label', 'breakdown', 'matched_terms'}
    assert 0 <= payload['match_score']['score'] <= 100


# ---------------- the schedule really fires (Django-Q scheduler) ----------------

@pytest.mark.django_db
def test_india_time_zone_is_offered_first_and_default(monkeypatch):
    monkeypatch.delenv('VENUE_DISCOVERY_TIMEZONE', raising=False)
    client, _ = admin()
    schedule = client.get(URL).json()['schedule']
    assert schedule['timezone'] == 'Asia/Kolkata'
    assert schedule['timezone_options'][0] == {'value': 'Asia/Kolkata', 'label': 'India — IST (UTC+05:30)'}


@pytest.mark.django_db
def test_django_q_scheduler_queues_discovery_at_the_set_time_and_keeps_the_local_time():
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    from django_q.models import OrmQ, Schedule
    from django_q.scheduler import scheduler
    from review.discovery_schedule import set_schedule

    set_schedule(enabled=True, hour=19, minute=12, tz_name='Asia/Kolkata')
    schedule = Schedule.objects.get(name='flexee-venue-discovery-daily')
    local = schedule.next_run.astimezone(ZoneInfo('Asia/Kolkata'))
    assert (local.hour, local.minute) == (19, 12)

    # Pretend the time has come: the scheduler the worker runs every ~30 s must queue the task.
    due = timezone.now() - timedelta(minutes=1)
    Schedule.objects.filter(id=schedule.id).update(next_run=due)
    scheduler()
    queued = [q for q in OrmQ.objects.all() if q.func() == 'review.tasks.run_venue_discovery_task']
    assert len(queued) == 1
    assert queued[0].task['kwargs'] == {'schedule_tz': 'Asia/Kolkata'}

    schedule.refresh_from_db()
    assert schedule.next_run > timezone.now()  # moved on to the next day
    assert timedelta(hours=23) < schedule.next_run - due < timedelta(hours=25)


@pytest.mark.django_db
def test_type_not_accepted_caps_the_score_even_with_perfect_scope():
    from review.match_score import compute_match_score
    config = make_config(article_types=['Book manuscript'])
    result = compute_match_score(make_manuscript(), config)
    assert result['breakdown']['type'] == 0 and result['score'] <= 50 and result['label'] != 'Strong fit'
