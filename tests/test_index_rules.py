"""Build plan step 4: read the first field's journal rules from official pages; an admin approves each."""
import json
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from review.auth import COOKIE_NAME, issue_session
from review.models import DiscoveredVenue, EditorUser, IndexedVenue, Venue, VenueIndexRun
from review.services import index_rules as ir
from review.services import index_screening as scr
from review.services import venue_discovery as vd
from review.services import venue_index as vi
from tests.test_venue_discovery import JOURNAL_PAGES, journal_extraction, make_fetcher

HOME = 'https://www.meridian-academic.example/jaaio'


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'rules-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    monkeypatch.setenv('VENUE_DISCOVERY_RETRY_DELAY_SECONDS', '0')
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})


def journal(title='Journal of Applied AI in Organizations', *, subfield='1404', homepage=HOME, **extra):
    base = dict(title=title, normalized_title=vd.normalize_name(title), field_profile='business-is',
                issn_l='0048-7333', issns=['0048-7333'], publisher='Meridian Academic Publishing',
                homepage_url=homepage, openalex_id=f'S{abs(hash(title)) % 10**8}',
                subfields=[{'id': subfield, 'name': 'Subject', 'share': 0.8}], metrics={'works_count': 900},
                screening_status='clear', enriched_at=timezone.now())
    base.update(extra)
    return IndexedVenue.objects.create(**base)


def admin_client():
    user = EditorUser.objects.create(email='root@example.com', password_hash='x', platform_superuser=True)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


def run_rules(fetcher, extractor=journal_extraction, **kw):
    run, created = vi.start_index_run(mode='rules', trigger='command')
    assert created
    return vi.run_index(run, rules_fetcher=fetcher, rules_extractor=extractor, **kw)


# ---------------------------------------------------------------------------
# Which journals are read
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_only_first_field_clear_unpublished_journals_are_candidates():
    keep = journal('IS Journal', openalex_id='S1')
    journal('Strategy Journal', openalex_id='S2', subfield='1408')              # other field
    journal('Flagged IS Journal', openalex_id='S3', screening_status='flagged') # waiting for a decision
    journal('Excluded IS Journal', openalex_id='S4', excluded=True, screening_status='excluded')
    journal('Kept IS Journal', openalex_id='S5', screening_status='kept')
    journal('Read IS Journal', openalex_id='S6', rules_status='ready')
    journal('No Site IS Journal', openalex_id='S7', homepage='')
    names = set(ir.rules_candidates('business-is').values_list('title', flat=True))
    assert names == {keep.title, 'Kept IS Journal'}


@pytest.mark.django_db
def test_failed_reads_are_retried_after_30_days():
    item = journal(rules_status='failed', rules_read_at=timezone.now())
    assert not ir.rules_candidates('business-is').exists()
    IndexedVenue.objects.filter(id=item.id).update(rules_read_at=timezone.now() - timezone.timedelta(days=31))
    assert ir.rules_candidates('business-is').exists()


# ---------------------------------------------------------------------------
# Reading: only quoted rules survive; nothing is published
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_reading_rules_makes_them_ready_but_publishes_nothing():
    item = journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run = run_rules(fetcher)
    item.refresh_from_db()
    assert run.status == 'completed' and (run.rules_attempted, run.rules_ready, run.rules_failed) == (1, 1, 0)
    assert item.rules_status == 'ready' and item.discovered and item.discovered.origin == 'index'
    assert not Venue.objects.exists() and item.venue is None  # waits for an admin
    summary = ir.rules_summary(item.discovered)
    assert summary['limits'] == ['Over the 8,000-word limit.']      # the invented 5,000 limit was dropped
    quotes = [e['quote'] for e in summary['evidence']]
    assert quotes and all(quotes)                                      # every shown rule carries its quote
    assert 'should not exceed 8,000 words' in quotes
    assert 'We also accept poetry and film scripts of any length.' not in quotes  # fabricated quote dropped


@pytest.mark.django_db
def test_page_without_rules_is_incomplete_with_a_reason():
    item = journal()
    # A homepage that states no rules at all (no article types, no limits, no submission route).
    fetcher, _config = make_fetcher({HOME: '<html><head><title>Journal</title></head><body><p>Welcome. '
                                           'News and events from our editorial office.</p></body></html>'})
    empty = lambda *a: {'name': 'Journal of Applied AI in Organizations', 'venue_type': 'journal'}  # noqa: E731
    run_rules(fetcher, extractor=empty)
    item.refresh_from_db()
    assert item.rules_status == 'incomplete' and 'quote' in item.rules_error


@pytest.mark.django_db
def test_rules_quoted_by_the_page_scanner_count_even_if_the_ai_returns_nothing():
    item = journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=lambda *a: {'name': 'Journal of Applied AI in Organizations', 'venue_type': 'journal'})
    item.refresh_from_db()
    summary = ir.rules_summary(item.discovered)
    assert item.rules_status == 'ready' and summary['article_types'] and all(e['quote'] for e in summary['evidence'])


@pytest.mark.django_db
def test_unreadable_site_is_failed_and_others_continue():
    bad = journal('Down Journal', openalex_id='S1', homepage='https://down.example/', metrics={'works_count': 5000})
    good = journal('Journal of Applied AI in Organizations', openalex_id='S2')
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run = run_rules(fetcher)
    bad.refresh_from_db(); good.refresh_from_db()
    assert bad.rules_status == 'failed' and 'could not be read' in bad.rules_error
    assert good.rules_status == 'ready' and run.rules_failed == 1 and run.rules_ready == 1


@pytest.mark.django_db
def test_ai_offline_stops_the_run_with_a_clear_reason():
    journal('One', openalex_id='S1')
    journal('Two', openalex_id='S2')
    fetcher, _config = make_fetcher(JOURNAL_PAGES)

    def offline(*a):
        raise vd.DiscoveryModelUnavailable('Could not reach Ollama at http://127.0.0.1:11434. Start Ollama.')
    run = run_rules(fetcher, extractor=offline)
    assert run.rules_attempted == 0 and any('Ollama' in e['detail'] for e in run.errors)
    assert IndexedVenue.objects.filter(rules_status='not_read').count() == 2  # nothing marked as failed


@pytest.mark.django_db
def test_per_run_ceiling(monkeypatch):
    for i in range(5):
        journal(f'IS Journal {i}', openalex_id=f'S{i}', homepage=f'https://j{i}.example/')
    fetcher, _config = make_fetcher({})
    run = run_rules(fetcher, rules_limit=2)
    assert run.rules_attempted == 2


@pytest.mark.django_db
def test_rules_reading_uses_local_ollama_by_default(monkeypatch):
    journal()
    seen = {}

    def spy(pages, config):
        seen['provider'] = config.ai_provider
        return journal_extraction()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=spy)
    assert seen['provider'] == 'ollama'


# ---------------------------------------------------------------------------
# Approval: Publish creates the live venue, labelled "Checked from official pages"
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_publish_creates_a_verified_index_venue_and_links_it():
    item = journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    client = admin_client()
    detail = client.get(f'/api/admin/venue-index/{item.id}/').json()['item']
    assert detail['rules']['status'] == 'ready' and detail['rules_read']['article_types'] == ['Research article', 'Review article']

    response = client.post(f'/api/admin/venue-index/{item.id}/publish/')
    assert response.status_code == 200
    body = response.json()['item']
    venue = Venue.objects.get()
    assert body['venue']['id'] == str(venue.id) and body['trust']['tier'] == 'verified_index'
    assert venue.trust_tier == 'verified_index' and venue.agent_configs.get().structured_desk_rejection_rules
    assert client.post(f'/api/admin/venue-index/{item.id}/publish/').status_code == 200  # idempotent
    assert Venue.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('state,message', [
    ({'rules_status': 'not_read'}, 'not been read'),
    ({'excluded': True, 'screening_status': 'excluded', 'rules_status': 'ready'}, 'excluded'),
    ({'screening_status': 'flagged', 'rules_status': 'ready'}, 'exclusion review'),
])
def test_publish_is_refused_when_not_ready(state, message):
    discovered = DiscoveredVenue.objects.create(name='J', normalized_name='j', venue_type='journal', origin='index')
    item = journal(discovered=discovered, **state)
    response = admin_client().post(f'/api/admin/venue-index/{item.id}/publish/')
    assert response.status_code == 409 and message in response.json()['detail']


@pytest.mark.django_db
def test_index_reads_do_not_appear_in_venue_discovery():
    journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    client = admin_client()
    listed = client.get('/api/admin/venue-discovery/?status=new&acceptance=').json()
    assert listed['pagination']['total'] == 0


@pytest.mark.django_db
def test_admin_filters_and_run_mode():
    journal('Ready', openalex_id='S1', rules_status='ready')
    journal('Missing', openalex_id='S2', rules_status='incomplete', rules_error='No quote')
    client = admin_client()
    body = client.get('/api/admin/venue-index/?filter=rules_ready').json()
    assert [i['title'] for i in body['items']] == ['Ready'] and body['filter_counts']['rules_missing'] == 1
    assert body['rules_field']['label'] == 'Information Systems and MIS'
    with mock.patch('django_q.tasks.async_task') as queued:
        response = client.post('/api/admin/venue-index/run/', data=json.dumps({'mode': 'rules'}),
                               content_type='application/json')
    assert response.json()['run']['mode'] == 'rules' and queued.called


@pytest.mark.django_db
def test_command_rules_only(monkeypatch, capsys):
    journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    real = vi.run_index
    monkeypatch.setattr('review.management.commands.import_venue_index.run_index',
                        lambda run, **kw: real(run, rules_fetcher=fetcher, rules_extractor=journal_extraction, **kw))
    call_command('import_venue_index', '--rules-only', '--limit', '5')
    out = capsys.readouterr().out
    assert 'rules ready for approval' in out and '1 journals have rules ready' in out
    assert VenueIndexRun.objects.get().summary.startswith('Rules read for 1 journals: 1 ready')


@pytest.mark.django_db
def test_excluding_a_published_journal_hides_the_live_venue():
    item = journal()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    admin_client().post(f'/api/admin/venue-index/{item.id}/publish/')
    item.refresh_from_db()
    scr.decide(item, decision='exclude', user_email='r@x', criteria=['invented_metrics'],
               evidence_urls=['https://bad.example'])
    assert Venue.objects.get().excluded


# ---------------------------------------------------------------------------
# Sites that refuse automated reading (HTTP 401/403/429, robots.txt)
# ---------------------------------------------------------------------------

def forbidden(url):
    import httpx
    return {url: httpx.Response(403, text='forbidden', headers={'content-type': 'text/html'})}


@pytest.mark.django_db
def test_blocked_site_gets_its_own_status_and_waits_90_days():
    item = journal('ACM Journal', homepage='https://cacm.example.org')
    fetcher, _config = make_fetcher(forbidden('https://cacm.example.org'))
    run = run_rules(fetcher)
    item.refresh_from_db()
    assert item.rules_status == 'blocked' and 'cacm.example.org blocks automated reading' in item.rules_error
    assert 'HTTP 403' in item.rules_error and run.rules_failed == 1
    IndexedVenue.objects.filter(id=item.id).update(rules_read_at=timezone.now() - timezone.timedelta(days=31))
    assert not ir.rules_candidates('business-is').exists()       # a plain failure would be due again
    IndexedVenue.objects.filter(id=item.id).update(rules_read_at=timezone.now() - timezone.timedelta(days=91))
    assert ir.rules_candidates('business-is').exists()


@pytest.mark.django_db
def test_other_journals_on_a_blocked_site_are_skipped_without_a_request():
    first = journal('Big Journal A', openalex_id='S1', homepage='https://pubs.example.org/a', metrics={'works_count': 9000})
    second = journal('Big Journal B', openalex_id='S2', homepage='https://pubs.example.org/b', metrics={'works_count': 8000})
    good = journal('Journal of Applied AI in Organizations', openalex_id='S3')
    requested = []
    pages = {**forbidden('https://pubs.example.org/a'), **forbidden('https://pubs.example.org/b'), **JOURNAL_PAGES}
    fetcher, _config = make_fetcher(pages)
    real_get = fetcher.client.send

    def spy(request, **kw):
        requested.append(str(request.url))
        return real_get(request, **kw)
    fetcher.client.send = spy
    run = run_rules(fetcher, rules_limit=2)
    for r in (first, second, good):
        r.refresh_from_db()
    assert first.rules_status == 'blocked' and second.rules_status == 'blocked'
    assert 'earlier in this run' in second.rules_error
    assert not any('/b' in u and 'robots' not in u for u in requested)  # never asked for B
    assert good.rules_status == 'ready'                                  # the skip did not use up the ceiling
    assert run.rules_attempted == 2


@pytest.mark.django_db
def test_blocked_guidelines_page_falls_back_to_the_homepage_on_another_site():
    item = journal(doaj={'guidelines_url': 'https://submit.blocked.example/guide'})
    fetcher, _config = make_fetcher({**forbidden('https://submit.blocked.example/guide'), **JOURNAL_PAGES})
    run_rules(fetcher)
    item.refresh_from_db()
    assert item.rules_status == 'ready'


@pytest.mark.django_db
def test_journals_with_a_doaj_guidelines_page_are_read_first():
    journal('Huge Publisher Journal', openalex_id='S1', homepage='https://huge.example/', metrics={'works_count': 50000})
    journal('DOAJ Journal', openalex_id='S2', homepage='https://small.example/', metrics={'works_count': 600},
            doaj={'guidelines_url': 'https://small.example/guide'})
    order = list(ir.reading_order(ir.rules_candidates('business-is')).values_list('title', flat=True))
    assert order == ['DOAJ Journal', 'Huge Publisher Journal']


@pytest.mark.django_db
def test_blocked_shows_in_the_missing_rules_filter():
    journal('Blocked', openalex_id='S1', rules_status='blocked', rules_error='x blocks automated reading')
    body = admin_client().get('/api/admin/venue-index/?filter=rules_missing').json()
    assert [i['title'] for i in body['items']] == ['Blocked'] and body['items'][0]['rules']['status'] == 'blocked'
