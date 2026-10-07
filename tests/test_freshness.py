"""Build plan step 7: re-check on a cadence, and never show an expired or unconfirmed call."""
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import DiscoveredVenue, IndexedVenue, Venue, VenueAgentConfig, VenueIndexRun
from review.services import freshness as fr
from review.services import index_rules as ir
from review.services import venue_discovery as vd
from review.services import venue_index as vi
from tests.test_author_dashboard_actions import author_client, make_venue
from tests.test_index_rules import admin_client, env, journal, run_rules  # noqa: F401  (env: autouse fixture)
from tests.test_venue_discovery import CFP_HOME, CFP_PAGE, JOURNAL_PAGES, make_fetcher

NOW = timezone.now()
FUTURE = '2099-03-15'
PAST = '2020-01-01'


def call(title, deadline, confirmed_days_ago=None):
    item = {'title': title, 'deadline': deadline, 'url': 'https://j.example/cfp', 'evidence_text': title}
    if confirmed_days_ago is not None:
        item['confirmed_at'] = (NOW - timedelta(days=confirmed_days_ago)).isoformat()
    return item


def verified_venue(name='Journal of Digital Operations', slug='jdo', **fields):
    venue = make_venue(name, slug)
    Venue.objects.filter(id=venue.id).update(trust_tier=Venue.TIER_VERIFIED_INDEX, last_verified_at=NOW, **fields)
    venue.refresh_from_db()
    return venue


def config_of(venue):
    return venue.agent_configs.get(active=True)


# ---------------------------------------------------------------------------
# The suppression rule
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_verified_calls_need_recent_confirmation_and_a_future_deadline():
    venue = verified_venue(calls_checked_at=NOW, open_calls=[
        call('Fresh call', FUTURE, confirmed_days_ago=3),
        call('Unconfirmed for weeks', FUTURE, confirmed_days_ago=15),
        call('Expired call', PAST, confirmed_days_ago=1),
    ])
    shown, hidden = fr.visible_calls(venue, config_of(venue), NOW)
    assert [c['title'] for c in shown] == ['Fresh call'] and hidden == 2


@pytest.mark.django_db
def test_editor_calls_are_hidden_only_when_expired():
    venue = make_venue('Field Notes Journal', 'fnj')  # claimed: the editor maintains its calls
    VenueAgentConfig.objects.filter(venue=venue).update(current_demand={'calls_for_papers': [
        call('Editor call', FUTURE), call('Old editor call', PAST)]})
    shown, hidden = fr.visible_calls(venue, config_of(venue), NOW)
    assert [c['title'] for c in shown] == ['Editor call'] and hidden == 1


@pytest.mark.django_db
def test_verified_venue_not_yet_checked_uses_its_verified_date():
    venue = verified_venue()
    VenueAgentConfig.objects.filter(venue=venue).update(current_demand={'calls_for_papers': [call('Read at publish', FUTURE)]})
    assert [c['title'] for c in fr.visible_calls(venue, config_of(venue), NOW)[0]] == ['Read at publish']
    Venue.objects.filter(id=venue.id).update(last_verified_at=NOW - timedelta(days=30))
    venue.refresh_from_db()
    assert fr.visible_calls(venue, config_of(venue), NOW) == ([], 1)


@pytest.mark.django_db
def test_authors_never_see_stale_calls_but_editors_see_raw_config():
    venue = verified_venue(calls_checked_at=NOW, open_calls=[call('Fresh', FUTURE, 1), call('Stale', FUTURE, 20)])
    VenueAgentConfig.objects.filter(venue=venue).update(
        current_demand={'calls_for_papers': [call('Stale', FUTURE)], 'topics': ['AI']},
        deadlines={'Call: Stale': FUTURE, 'submission': 'rolling', 'Old round': PAST})
    client, _author = author_client()
    config = client.get('/api/author/venues/').json()['venues'][0]['config']
    assert [c['title'] for c in config['open_calls']] == ['Fresh'] and config['calls_hidden'] == 1
    assert config['current_demand'] == {'topics': ['AI']}
    assert config['deadlines'] == {'submission': 'rolling'}  # 'Call: ...' and passed dates removed

    admin = admin_client()
    raw = admin.get(f'/api/admin/venues/{venue.id}/').json()['venue']['config']
    assert 'calls_for_papers' in raw['current_demand'] and 'Call: Stale' in raw['deadlines']


@pytest.mark.django_db
def test_ai_prompts_get_only_live_calls():
    from review.services.author_agents import _config_context
    venue = verified_venue(calls_checked_at=NOW, open_calls=[call('Fresh', FUTURE, 1), call('Stale', FUTURE, 20)])
    VenueAgentConfig.objects.filter(venue=venue).update(deadlines={'Call: Stale': FUTURE, 'submission': 'rolling'})
    context = _config_context(config_of(venue))
    assert [c['title'] for c in context['current_demand']['open_calls']] == ['Fresh']
    assert context['deadlines'] == {'submission': 'rolling'}


# ---------------------------------------------------------------------------
# The weekly re-check (no AI)
# ---------------------------------------------------------------------------

CFP_PAGES = {'https://ops.example/jdo': CFP_HOME, 'https://ops.example/jdo/special-issues': CFP_PAGE}


@pytest.mark.django_db
def test_check_confirms_calls_from_the_official_pages():
    venue = verified_venue(source_urls=['https://ops.example/jdo'])
    fetcher, config = make_fetcher(CFP_PAGES)
    assert fr.check_calls(venue, fetcher, config, now=NOW) == 2
    venue.refresh_from_db()
    assert [c['deadline'] for c in venue.open_calls] == ['2099-03-15', '2099-12-01']
    assert all(c['confirmed_at'] == NOW.isoformat() for c in venue.open_calls)
    assert venue.calls_checked_at == NOW and venue.calls_error == ''


@pytest.mark.django_db
def test_unreadable_pages_keep_old_calls_which_then_age_out():
    old = NOW - timedelta(days=12)
    venue = verified_venue(source_urls=['https://down.example/j'], calls_checked_at=old,
                           open_calls=[call('Earlier call', FUTURE, confirmed_days_ago=12)])
    fetcher, config = make_fetcher({})
    with pytest.raises(vd.DiscoveryFetchError):
        fr.check_calls(venue, fetcher, config, now=NOW)
    venue.refresh_from_db()
    assert venue.open_calls and 'could not be read' in venue.calls_error and venue.calls_attempted_at == NOW
    assert fr.visible_calls(venue, config_of(venue), NOW) == ([], 1)  # unconfirmed for 12 days: hidden


@pytest.mark.django_db
def test_calls_run_and_cadence():
    verified_venue(source_urls=['https://ops.example/jdo'])
    recent = verified_venue('Checked Yesterday', 'cy', source_urls=['https://cy.example/'],
                            calls_attempted_at=NOW - timedelta(days=1))
    make_venue('Editor Journal', 'ej')  # claimed: never crawled
    assert [v.name for v in fr.calls_due(NOW)] == ['Journal of Digital Operations']
    fetcher, _ = make_fetcher(CFP_PAGES)
    run, _ = vi.start_index_run(mode='calls', trigger='command')
    run = vi.run_index(run, rules_fetcher=fetcher)
    assert run.status == 'completed' and (run.calls_checked, run.calls_open, run.calls_failed) == (1, 2, 0)
    assert run.summary.startswith('Open calls re-confirmed for 1 venues: 2 open calls found')
    recent.refresh_from_db()
    assert recent.calls_attempted_at < NOW  # not due, not touched


@pytest.mark.django_db
def test_publishing_seeds_calls_confirmed_when_read():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    item.refresh_from_db()
    DiscoveredVenue.objects.filter(id=item.discovered_id).update(
        current_demand={'calls_for_papers': [call('Special issue', FUTURE)]})
    admin_client().post(f'/api/admin/venue-index/{item.id}/publish/')
    venue = Venue.objects.get()
    assert [c['title'] for c in venue.open_calls] == ['Special issue']
    assert venue.calls_checked_at and venue.open_calls[0]['confirmed_at']


# ---------------------------------------------------------------------------
# Quarterly rules re-read, and applying changes found on the pages
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_live_journals_are_re_read_after_90_days():
    venue = make_venue('Live', 'live')
    item = journal(venue=venue, rules_status='ready', rules_read_at=NOW - timedelta(days=30))
    assert not ir.rules_candidates('business-is').filter(id=item.id).exists()
    IndexedVenue.objects.filter(id=item.id).update(rules_read_at=NOW - timedelta(days=91))
    assert ir.rules_candidates('business-is').filter(id=item.id).exists()


@pytest.mark.django_db
def test_admin_applies_changes_found_on_a_live_journals_pages():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    client = admin_client()
    client.post(f'/api/admin/venue-index/{item.id}/publish/')
    item.refresh_from_db()
    assert client.post(f'/api/admin/venue-index/{item.id}/apply-changes/').status_code == 409  # nothing waiting

    later = NOW + timedelta(days=95)
    DiscoveredVenue.objects.filter(id=item.discovered_id).update(
        discovery_status='changed', change_summary='Word limit changed.', aims_scope='New scope.', last_checked_at=later)
    listed = client.get('/api/admin/venue-index/?filter=rules_changed').json()
    assert [i['title'] for i in listed['items']] == [item.title]
    assert listed['items'][0]['rules']['changes'] == 'Word limit changed.'

    response = client.post(f'/api/admin/venue-index/{item.id}/apply-changes/')
    assert response.status_code == 200 and response.json()['item']['rules']['changes'] == ''
    venue = Venue.objects.get()
    active = venue.agent_configs.get(active=True)
    assert active.version == 2 and active.aims_scope == 'New scope.' and venue.agent_configs.count() == 2
    assert venue.last_verified_at == later


@pytest.mark.django_db
def test_apply_changes_requires_platform_admin():
    from django.test import Client
    item = journal()
    assert Client().post(f'/api/admin/venue-index/{item.id}/apply-changes/').status_code == 401


# ---------------------------------------------------------------------------
# Schedules and the admin's view
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_schedules_include_daily_calls_and_weekly_rules(monkeypatch):
    from django_q.models import Schedule
    from review.index_schedule import set_index_schedule
    state = set_index_schedule(enabled=True, tz_name='UTC')
    assert state['next_calls_check'] and state['next_rules_read']
    kwargs = {s.name: s.kwargs for s in Schedule.objects.all()}
    assert "'calls'" in kwargs['flexee-venue-index-daily-calls'] and "'rules'" in kwargs['flexee-venue-index-weekly-rules']
    monkeypatch.setenv('VENUE_INDEX_RULES_SCHEDULE', 'off')
    assert set_index_schedule(enabled=True, tz_name='UTC')['next_rules_read'] is None
    set_index_schedule(enabled=False)
    assert not Schedule.objects.filter(func='review.tasks.run_venue_index_task').exists()


@pytest.mark.django_db
def test_scheduled_calls_and_rules_cost_nothing_when_nothing_is_due():
    from review.tasks import run_venue_index_task
    assert run_venue_index_task(mode='calls') is None
    assert run_venue_index_task(mode='rules') is None
    assert not VenueIndexRun.objects.exists()


@pytest.mark.django_db
def test_admin_sees_freshness():
    verified_venue(calls_checked_at=NOW, open_calls=[call('Fresh', FUTURE, 1), call('Stale', FUTURE, 20)])
    body = admin_client().get('/api/admin/venue-index/').json()
    f = body['freshness']
    assert (f['live_verified'], f['calls_shown'], f['calls_hidden'], f['median_age_days']) == (1, 1, 1, 0)
    assert f['calls_confirm_days'] == 10 and f['rules_refresh_days'] == 90
