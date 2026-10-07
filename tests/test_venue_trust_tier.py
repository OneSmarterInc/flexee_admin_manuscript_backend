"""Build plan step 1: trust tier, verified date, source links and exclusion fields on venues."""
import json
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.test import Client
from django.utils import timezone

from review.auth import COOKIE_NAME, issue_session
from review.models import DiscoveredVenue, EditorUser, Membership, Organization, Venue, VenueAgentConfig
from review.services import venue_discovery as vd
from tests.test_author_dashboard_actions import author_client, make_manuscript, make_venue
from tests.test_venue_discovery import JOURNAL_GUIDE, JOURNAL_PAGES, journal_extraction, make_fetcher, stage_journal


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'trust-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    monkeypatch.setenv('VENUE_DISCOVERY_RETRY_DELAY_SECONDS', '0')
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})  # test hosts resolve to a public address


def admin_client(email='root@example.com', superuser=True):
    user = EditorUser.objects.create(email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, user


def patch(client, url, data):
    return client.patch(url, data=json.dumps(data), content_type='application/json')


# ---------------------------------------------------------------------------
# Payloads: every venue carries its tier and date, so tiers never look the same
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_match_results_carry_tier_date_and_sources():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    indexed = make_venue('Journal of Applied AI', 'jaai')
    checked = timezone.now() - timedelta(days=42)
    Venue.objects.filter(id=indexed.id).update(trust_tier='verified_index', last_verified_at=checked,
                                               source_urls=['https://jaai.example/guidelines'])
    m = make_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')

    matches = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    trust = {item['venue']['name']: item['venue']['trust'] for item in matches}
    assert trust['Field Notes Journal']['tier'] == 'claimed'
    assert trust['Journal of Applied AI'] == {
        'tier': 'verified_index', 'label': 'Verified from official pages',
        'last_verified_at': checked.isoformat(), 'source_urls': ['https://jaai.example/guidelines'],
    }


@pytest.mark.django_db
def test_authors_never_see_exclusion_details():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    venue = client.get('/api/author/venues/').json()['venues'][0]
    assert 'excluded' not in venue and 'exclusion_reason' not in venue
    assert venue['trust']['tier'] == 'claimed'


# ---------------------------------------------------------------------------
# Exclusions: the venue simply does not appear
# ---------------------------------------------------------------------------

def exclude(venue):
    Venue.objects.filter(id=venue.id).update(excluded=True, exclusion_reason={
        'criteria': ['guaranteed_acceptance'], 'evidence_urls': ['https://x.example/fees'],
        'decided_at': timezone.now().isoformat(), 'decided_by': 'root@example.com', 'note': '',
    })


@pytest.mark.django_db
def test_excluded_venue_is_hidden_everywhere_an_author_looks():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    bad = make_venue('Rapid Acceptance Journal', 'raj')
    m = make_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    assert len(client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']) == 2

    exclude(bad)

    names = [v['name'] for v in client.get('/api/author/venues/').json()['venues']]
    assert names == ['Field Notes Journal']
    matches = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert [x['venue']['name'] for x in matches] == ['Field Notes Journal']
    assert client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]['match_count'] == 1
    rerun = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()['matches']
    assert [x['venue']['name'] for x in rerun] == ['Field Notes Journal']
    response = client.post(f'/api/author/manuscripts/{m.id}/submissions/', data=json.dumps({'venue_id': str(bad.id)}),
                           content_type='application/json')
    assert response.status_code == 404


@pytest.mark.django_db
def test_listed_venue_is_visible_but_never_matched_on_rules():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    listed = make_venue('Spine Only Review', 'sor')
    Venue.objects.filter(id=listed.id).update(trust_tier='listed')
    m = make_manuscript(author)

    names = {v['name'] for v in client.get('/api/author/venues/').json()['venues']}
    assert names == {'Field Notes Journal', 'Spine Only Review'}
    matches = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()['matches']
    assert [x['venue']['name'] for x in matches] == ['Field Notes Journal']


@pytest.mark.django_db
def test_admin_sees_exclusion_details():
    client, _ = admin_client()
    venue = make_venue('Rapid Acceptance Journal', 'raj')
    exclude(venue)
    listed = client.get('/api/admin/venues/').json()['venues'][0]
    assert listed['excluded'] is True and listed['exclusion_reason']['criteria'] == ['guaranteed_acceptance']
    detail = client.get(f'/api/admin/venues/{venue.id}/').json()['venue']
    assert detail['excluded'] is True and detail['trust']['tier'] == 'claimed'


# ---------------------------------------------------------------------------
# Who can change the tier, and what moves the date
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_only_platform_admin_can_change_tier():
    venue = make_venue('Field Notes Journal', 'fnj')
    owner_client, owner = admin_client('owner@example.com', superuser=False)
    Membership.objects.create(user=owner, organization=venue.organization, role='owner')
    assert patch(owner_client, f'/api/admin/venues/{venue.id}/', {'trust_tier': 'listed'}).status_code == 403

    root, _ = admin_client()
    assert patch(root, f'/api/admin/venues/{venue.id}/', {'trust_tier': 'bogus'}).status_code == 400
    response = patch(root, f'/api/admin/venues/{venue.id}/', {'trust_tier': 'verified_index'})
    assert response.status_code == 200 and response.json()['venue']['trust']['tier'] == 'verified_index'
    venue.refresh_from_db()
    assert venue.trust_tier == 'verified_index'


@pytest.mark.django_db
def test_editor_saving_rules_confirms_a_claimed_venue_only():
    root, _ = admin_client()
    claimed = make_venue('Field Notes Journal', 'fnj')
    indexed = make_venue('Journal of Applied AI', 'jaai')
    old = timezone.now() - timedelta(days=90)
    Venue.objects.filter(id=indexed.id).update(trust_tier='verified_index', last_verified_at=old)

    for venue in (claimed, indexed):
        response = root.post(f'/api/admin/venues/{venue.id}/config/', data=json.dumps({
            'aims_scope': 'AI in operations.', 'article_types': ['Research article'],
        }), content_type='application/json')
        assert response.status_code in (200, 201)

    claimed.refresh_from_db()
    indexed.refresh_from_db()
    assert claimed.last_verified_at and timezone.now() - claimed.last_verified_at < timedelta(minutes=1)
    assert indexed.last_verified_at == old  # an admin edit is not a re-read of the official pages


# ---------------------------------------------------------------------------
# Discovery: added venues are verified_index; re-reads re-confirm them
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_venue_added_from_discovery_is_verified_index():
    record = stage_journal()
    root, _ = admin_client()
    response = root.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    assert response.status_code == 201
    venue = Venue.objects.get(id=response.json()['venue']['id'])
    record.refresh_from_db()
    assert venue.trust_tier == 'verified_index'
    assert venue.last_verified_at == record.last_checked_at
    assert venue.source_urls == record.source_urls and venue.source_urls


@pytest.mark.django_db
def test_unchanged_recheck_reconfirms_the_live_venue():
    record = stage_journal()
    root, _ = admin_client()
    venue_id = root.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').json()['venue']['id']
    old = timezone.now() - timedelta(days=30)
    Venue.objects.filter(id=venue_id).update(last_verified_at=old)

    fetcher, config = make_fetcher(JOURNAL_PAGES)
    _, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                      extractor=journal_extraction)
    assert outcome == 'unchanged'
    assert Venue.objects.get(id=venue_id).last_verified_at > old


@pytest.mark.django_db
def test_changed_pages_do_not_reconfirm_the_live_venue():
    record = stage_journal()
    root, _ = admin_client()
    venue_id = root.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').json()['venue']['id']
    old = timezone.now() - timedelta(days=30)
    Venue.objects.filter(id=venue_id).update(last_verified_at=old)

    # The word limit on the official guidelines page changed (same shape as the discovery tests).
    changed_guide = JOURNAL_GUIDE.replace('8,000', '7,500')
    fetcher, config = make_fetcher({**JOURNAL_PAGES,
                                    'https://www.meridian-academic.example/jaaio/author-guidelines': changed_guide})
    raw = journal_extraction()
    raw['structured_desk_rejection_rules'] = [{'field': 'word_count', 'operator': '>', 'value': 7500, 'message': 'Over 7,500.'}]
    raw['source_evidence'][1]['evidence_text'] = 'should not exceed 7,500 words'
    _, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                      extractor=lambda *a: raw)
    assert outcome == 'changed'
    assert Venue.objects.get(id=venue_id).last_verified_at == old


# ---------------------------------------------------------------------------
# Migration backfill for existing venues
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_backfill_labels_existing_venues():
    import importlib
    backfill = importlib.import_module('review.migrations.0024_venue_trust_tier').backfill_trust

    org = Organization.objects.create(name='Flexee Publishing')
    editor_venue = Venue.objects.create(organization=org, name='Editor J', slug='ej', venue_type='journal')
    config = VenueAgentConfig.objects.create(venue=editor_venue, version=1, active=True)
    found_venue = Venue.objects.create(organization=org, name='Found J', slug='fj', venue_type='journal')
    checked = timezone.now() - timedelta(days=10)
    DiscoveredVenue.objects.create(name='Found J', normalized_name='found j', venue_type='journal',
                                   added_venue=found_venue, discovery_status='added', last_checked_at=checked,
                                   source_urls=['https://fj.example/guide'])
    Venue.objects.update(trust_tier='listed', last_verified_at=None, source_urls=[])

    backfill(django_apps, None)

    editor_venue.refresh_from_db()
    found_venue.refresh_from_db()
    assert editor_venue.trust_tier == 'claimed' and editor_venue.last_verified_at == config.effective_at
    assert found_venue.trust_tier == 'verified_index' and found_venue.last_verified_at == checked
    assert found_venue.source_urls == ['https://fj.example/guide']
