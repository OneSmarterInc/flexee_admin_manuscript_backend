"""Build plan step 8: the author-facing journal index (search, journal pages, tier and date everywhere)."""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from review.match_score import compute_match_score
from review.models import IndexedVenue, Venue, VenueAgentConfig, VenueIndexRun
from tests.test_author_dashboard_actions import make_manuscript, author_client, make_venue

NOW = timezone.now()


def listed(title, **extra):
    base = dict(title=title, normalized_title=title.lower(), field_profile='business-is', issn_l='0048-7333',
                issns=['0048-7333'], publisher='Meridian Academic', homepage_url='https://j.example',
                openalex_id=f'S{abs(hash(title)) % 10**8}', subfields=[{'id': '1404', 'name': 'Management Information Systems', 'share': 0.8}],
                metrics={'works_count': 900}, screening_status='clear', open_access=False, last_refreshed_at=NOW)
    base.update(extra)
    return IndexedVenue.objects.create(**base)


def search(**params):
    return Client().get('/api/journals/', params).json()


@pytest.mark.django_db
def test_search_lists_live_venues_first_then_listed_journals_with_their_tier():
    make_venue('Field Notes Journal', 'fnj')
    checked = make_venue('Journal of Applied AI', 'jaai')
    Venue.objects.filter(id=checked.id).update(trust_tier='verified_index', last_verified_at=NOW)
    listed('MIS Quarterly Review', metrics={'works_count': 5000})
    listed('Small IS Journal', metrics={'works_count': 600})
    body = search()
    assert [(i['name'], i['trust']['tier']) for i in body['items']] == [
        ('Field Notes Journal', 'claimed'), ('Journal of Applied AI', 'verified_index'),
        ('MIS Quarterly Review', 'listed'), ('Small IS Journal', 'listed')]
    assert body['items'][1]['trust']['last_verified_at'] and body['items'][2]['trust']['label'] == 'Listed only'
    assert body['counts'] == {'claimed': 1, 'verified_index': 1, 'listed': 2}
    assert body['items'][0]['matchable'] and not body['items'][2]['matchable']


@pytest.mark.django_db
def test_excluded_flagged_and_live_duplicates_never_appear():
    venue = make_venue('Rapid Acceptance Journal', 'raj')
    Venue.objects.filter(id=venue.id).update(excluded=True)
    listed('Excluded Journal', excluded=True, screening_status='excluded')
    listed('Flagged Journal', screening_status='flagged')            # waiting for a decision
    live = make_venue('Live Journal', 'live')
    listed('Live Journal', venue=live)                                # shown once, as the live venue
    names = [i['name'] for i in search()['items']]
    assert names == ['Live Journal']


@pytest.mark.django_db
def test_search_filters():
    make_venue('Field Notes Journal', 'fnj')
    listed('Information Systems Research', issn_l='1047-7047', open_access=True)
    listed('Strategy Review', publisher='Other Press', primary_subfield='Strategy and Management')
    assert [i['name'] for i in search(q='1047-7047')['items']] == ['Information Systems Research']
    assert [i['name'] for i in search(q='strategy')['items']] == ['Strategy Review']
    assert [i['name'] for i in search(q='other press')['items']] == ['Strategy Review']
    assert [i['name'] for i in search(tier='listed')['items']] == ['Information Systems Research', 'Strategy Review']
    assert [i['name'] for i in search(tier='claimed')['items']] == ['Field Notes Journal']
    assert [i['name'] for i in search(open_access='1')['items']] == ['Information Systems Research']


@pytest.mark.django_db
def test_pagination_spans_live_and_listed():
    for i in range(3):
        make_venue(f'Live {i}', f'live-{i}')
    for i in range(25):
        listed(f'Listed {i:02d}', metrics={'works_count': 1000 - i})
    first, second = search(page=1), search(page=2)
    assert first['pagination']['total'] == 28 and first['pagination']['pages'] == 2
    assert len(first['items']) == 20 and len(second['items']) == 8
    names = [i['name'] for i in first['items'] + second['items']]
    assert len(set(names)) == 28 and names[:3] == ['Live 0', 'Live 1', 'Live 2']


@pytest.mark.django_db
def test_live_journal_page_shows_rules_tier_date_and_only_live_calls():
    venue = make_venue('Journal of Applied AI', 'jaai')
    Venue.objects.filter(id=venue.id).update(
        trust_tier='verified_index', last_verified_at=NOW, source_urls=['https://jaai.example/guide'],
        calls_checked_at=NOW, open_calls=[
            {'title': 'Fresh call', 'deadline': '2099-03-15', 'confirmed_at': NOW.isoformat()},
            {'title': 'Stale call', 'deadline': '2099-03-15', 'confirmed_at': (NOW - timedelta(days=30)).isoformat()}])
    VenueAgentConfig.objects.filter(venue=venue).update(
        structured_desk_rejection_rules=[{'message': 'Over the 8,000-word limit.'}],
        required_submission_items=[{'label': 'Cover letter'}])
    listed('Journal of Applied AI', venue=venue, open_access=True, doaj_listed=True, apc_usd=900)
    page = Client().get('/api/journals/v/jaai/').json()['journal']
    assert page['trust']['tier'] == 'verified_index' and page['trust']['source_urls'] == ['https://jaai.example/guide']
    assert page['rules']['limits'] == ['Over the 8,000-word limit.'] and page['rules']['required_items'] == ['Cover letter']
    assert [c['title'] for c in page['open_calls']] == ['Fresh call']
    assert page['catalogue']['doaj_listed'] and page['catalogue']['apc_usd'] == 900


@pytest.mark.django_db
def test_listed_journal_page_has_catalogue_facts_only():
    record = listed('MIS Quarterly Review', apc_usd=0, open_access=True)
    page = Client().get(f'/api/journals/i/{record.id}/').json()['journal']
    assert page['trust']['tier'] == 'listed' and page['rules'] is None and not page['matchable']
    assert page['catalogue']['issn'] == '0048-7333' and page['catalogue']['subjects'] == ['Management Information Systems']
    hidden = listed('Flagged', screening_status='flagged')
    assert Client().get(f'/api/journals/i/{hidden.id}/').status_code == 404
    assert Client().get('/api/journals/v/does-not-exist/').status_code == 404


@pytest.mark.django_db
def test_listed_page_points_to_the_live_page_once_published():
    venue = make_venue('Went Live', 'went-live')
    record = listed('Went Live', venue=venue)
    body = Client().get(f'/api/journals/i/{record.id}/').json()
    assert body['redirect'] == '/journals/v/went-live'


# ---------------------------------------------------------------------------
# Match results: topic similarity counts for scope
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_topic_similarity_lifts_scope_for_close_meaning_without_shared_words():
    venue = make_venue('Organisational Computing', 'oc')
    VenueAgentConfig.objects.filter(venue=venue).update(aims_scope='Enterprise software adoption in firms.')
    _client, author = author_client()
    m = make_manuscript(author)
    config = venue.agent_configs.get()
    plain = compute_match_score(m, config)
    close = compute_match_score(m, config, topic_similarity=0.70)
    far = compute_match_score(m, config, topic_similarity=0.35)
    assert close['breakdown']['scope'] == 40 and close['score'] > plain['score']
    assert far['breakdown']['scope'] == plain['breakdown']['scope']  # never lowers the word-based score


@pytest.mark.django_db
def test_force_closes_a_run_left_by_a_restart(capsys):
    VenueIndexRun.objects.create(mode='rules', status='processing', trigger='manual')
    call_command('import_venue_index', '--calls-only', '--force')
    out = capsys.readouterr().out
    assert 'Closed 1 run left over from a restart' in out
    assert VenueIndexRun.objects.filter(status='processing').count() == 0
