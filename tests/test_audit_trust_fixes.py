"""12 October audit, trust promises shown to authors (items 4, 5 and 23).

4  A rule is kept only with its own quote from the official page; quote matching is strict and a
   year is never read as a word limit; phrases the system spots itself are not "rules found".
5  Journals that share a publisher-wide guidelines page stay separate records, and Publish never
   puts one journal onto another journal's reading or live venue.
23 A venue an org owner creates is "Listed only"; only an approved claim makes it editor-confirmed,
   and a confirmed venue cannot be renamed without Flexee.
"""
from datetime import timedelta

import pytest
from django.test import Client

from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import (DiscoveredVenue, EditorUser, IndexedVenue, Membership, Organization, Venue,
                           VenueClaim)
from review.services import index_rules as ir
from review.services import venue_discovery as vd
from tests.test_index_rules import admin_client, journal, run_rules
from tests.test_venue_discovery import JOURNAL_PAGES, journal_extraction, make_fetcher

GUIDE = 'https://www.meridian-academic.example/jaaio/author-guidelines'


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'trust-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    monkeypatch.setenv('VENUE_DISCOVERY_RETRY_DELAY_SECONDS', '0')
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})


# ---------------------------------------------------------------------------
# Item 4: quotes
# ---------------------------------------------------------------------------

PAGE = ('Founded in 2019, the journal publishes research on information systems. '
        'Manuscripts should not exceed 8,000 words, including references. '
        'Please cite no more than 60 references.')


def test_one_short_fragment_no_longer_verifies_a_quote():
    assert vd._quote_found('Manuscripts should not exceed 8,000 words', PAGE)
    # Before: any 12-character fragment was enough, so an invented second half passed.
    assert not vd._quote_found('should not exceed ... a strict limit of 5,000 words for reviews', PAGE)
    assert not vd._quote_found('2019', PAGE)        # too short to prove anything
    assert vd._quote_found('Manuscripts should not exceed ... including references', PAGE)


def test_a_year_is_not_a_word_limit():
    rules = [{'field': 'word_count', 'operator': '>', 'value': 2019, 'message': 'Year as a limit'},
             {'field': 'word_count', 'operator': '>', 'value': 8000, 'message': 'Real limit'},
             {'field': 'reference_count', 'operator': '>', 'value': 60, 'message': 'Real reference limit'}]
    page = vd.FetchedPage(url=GUIDE, title='Guide', text=PAGE)
    kept = vd._verified_structured_rules(rules, [page])
    assert sorted((r['field'], r['value']) for r in kept) == [('reference_count', 60), ('word_count', 8000)]


def test_index_reading_drops_rule_fields_without_their_own_quote():
    candidate = {
        'aims_scope': 'Anything at all.', 'article_types': ['Research article'], 'submission_types': ['research_article'],
        'required_submission_items': [{'key': 'coi', 'label': 'Conflict of interest', 'required': True}],
        'structured_desk_rejection_rules': [],
        'source_evidence': [{'field': 'word_limit', 'claim': 'x', 'url': GUIDE, 'excerpt': 'should not exceed 8,000 words'}],
    }
    kept = ir.keep_quoted_rules(candidate)
    assert kept['aims_scope'] == '' and kept['required_submission_items'] == [] and kept['article_types'] == []
    candidate['source_evidence'] += [
        {'field': 'aims_scope', 'claim': 'Scope', 'url': GUIDE, 'excerpt': 'publishes research on information systems'},
        {'field': 'required_submission_items', 'claim': 'COI', 'url': GUIDE, 'excerpt': 'A conflict of interest statement is required'},
        {'field': 'article_types', 'claim': 'Types', 'url': GUIDE, 'excerpt': 'original research articles'},
    ]
    kept = ir.keep_quoted_rules(candidate)
    assert kept['aims_scope'] and kept['required_submission_items'] and kept['article_types'] == ['Research article']


def test_page_signals_alone_are_not_rules_found():
    item = {'confidence': 90, 'article_types': ['Research article'],
            'source_evidence': [{'field': 'accepted_types', 'claim': 'The official page mentions research articles.',
                                 'url': GUIDE, 'excerpt': 'We publish original research articles', 'origin': 'page_signal'}]}
    ok, why = ir.rules_found(item, 40)
    assert not ok and 'quote' in why


# ---------------------------------------------------------------------------
# Item 5: one publisher page, two journals
# ---------------------------------------------------------------------------

def _shared_page_journals():
    """Two journals whose DOAJ entry points at the same publisher-wide guidelines page."""
    first = journal('Journal A of Information Systems', openalex_id='SA', homepage='',
                    doaj={'guidelines_url': GUIDE})
    second = journal('Journal B of Information Systems', openalex_id='SB', homepage='',
                     doaj={'guidelines_url': GUIDE})
    return first, second


@pytest.mark.django_db
def test_journals_on_one_publisher_page_keep_separate_records():
    first, second = _shared_page_journals()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.rules_status == 'ready' and second.rules_status == 'ready'
    assert first.discovered_id != second.discovered_id
    assert first.discovered.name == first.title and second.discovered.name == second.title


@pytest.mark.django_db
def test_publishing_both_journals_gives_two_venues():
    first, second = _shared_page_journals()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    client = admin_client()
    assert client.post(f'/api/admin/venue-index/{first.id}/publish/').status_code == 200
    assert client.post(f'/api/admin/venue-index/{second.id}/publish/').status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.venue_id and second.venue_id and first.venue_id != second.venue_id
    assert set(Venue.objects.values_list('name', flat=True)) == {first.title, second.title}


@pytest.mark.django_db
def test_publish_refuses_a_reading_already_merged_with_another_journal():
    """Records merged before this fix: Publish must not link journal B to journal A's venue."""
    first, second = _shared_page_journals()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    first.refresh_from_db()
    IndexedVenue.objects.filter(id=second.id).update(discovered=first.discovered)  # the old merged state
    client = admin_client()
    response = client.post(f'/api/admin/venue-index/{second.id}/publish/')
    assert response.status_code == 409 and response.json()['code'] == 'shared_reading'
    assert not Venue.objects.exists()


@pytest.mark.django_db
def test_publish_refuses_a_reading_live_as_another_journal():
    first, second = _shared_page_journals()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    client = admin_client()
    assert client.post(f'/api/admin/venue-index/{first.id}/publish/').status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    # Point B at A's already-published reading through a second record (the other merge path).
    stray = DiscoveredVenue.objects.get(id=first.discovered_id)
    IndexedVenue.objects.filter(id=first.id).update(discovered=second.discovered)
    IndexedVenue.objects.filter(id=second.id).update(discovered=stray)
    response = client.post(f'/api/admin/venue-index/{second.id}/publish/')
    assert response.status_code == 409 and response.json()['code'] == 'other_venue'
    assert Venue.objects.count() == 1


@pytest.mark.django_db
def test_a_reread_of_a_merged_journal_gets_its_own_record():
    first, second = _shared_page_journals()
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    first.refresh_from_db()
    IndexedVenue.objects.filter(id=second.id).update(discovered=first.discovered, rules_status='failed',
                                                     rules_read_at=first.rules_read_at - timedelta(days=40))
    second.refresh_from_db()
    ir.read_rules_for(second, fetcher, vd.DiscoveryConfig.from_env(), extractor=journal_extraction)
    second.refresh_from_db()
    assert second.discovered_id != first.discovered_id and second.discovered.name == second.title


# ---------------------------------------------------------------------------
# Item 23: who may say "Editor-confirmed"
# ---------------------------------------------------------------------------

def _owner(email='owner@x.org'):
    org = Organization.objects.create(name='Owner org', organization_type='journal')
    user = EditorUser.objects.create(email=email, password_hash=hash_password('pw'))
    Membership.objects.create(user=user, organization=org, role='owner')
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, org, user


def _superuser():
    user = EditorUser.objects.create(email='root@x.org', password_hash='x', platform_superuser=True)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


@pytest.mark.django_db
def test_a_venue_an_owner_creates_is_listed_only():
    client, org, _user = _owner()
    response = client.post('/api/admin/venues/', {'name': 'Nature', 'venue_type': 'journal',
                                                   'organization_id': str(org.id)}, content_type='application/json')
    assert response.status_code == 201
    assert Venue.objects.get(name='Nature').trust_tier == Venue.TIER_LISTED


@pytest.mark.django_db
def test_saving_rules_does_not_make_an_own_venue_editor_confirmed():
    client, org, _user = _owner()
    venue = Venue.objects.create(name='Nature', slug='nature', venue_type='journal', organization=org,
                                 trust_tier=Venue.TIER_LISTED)
    response = client.post(f'/api/admin/venues/{venue.id}/config/', {'aims_scope': 'Everything.'},
                           content_type='application/json')
    assert response.status_code in (200, 201)
    venue.refresh_from_db()
    assert venue.trust_tier == Venue.TIER_LISTED


@pytest.mark.django_db
def test_an_approved_claim_still_becomes_editor_confirmed():
    client, org, user = _owner()
    venue = Venue.objects.create(name='Real Journal', slug='real-journal', venue_type='journal', organization=org,
                                 trust_tier=Venue.TIER_LISTED)
    VenueClaim.objects.create(venue=venue, journal_name=venue.name, name='Ed', email=user.email,
                              role_title='Editor', status='approved', editor_user=user)
    response = client.post(f'/api/admin/venues/{venue.id}/config/', {'aims_scope': 'Our scope.'},
                           content_type='application/json')
    assert response.status_code in (200, 201)
    venue.refresh_from_db()
    assert venue.trust_tier == Venue.TIER_CLAIMED and venue.last_verified_at


@pytest.mark.django_db
def test_a_superuser_created_venue_keeps_the_old_default():
    client = _superuser()
    response = client.post('/api/admin/venues/', {'name': 'Field Notes Journal', 'venue_type': 'journal'},
                           content_type='application/json')
    assert response.status_code == 201
    assert Venue.objects.get(name='Field Notes Journal').trust_tier == Venue.TIER_CLAIMED


@pytest.mark.django_db
@pytest.mark.parametrize('tier', [Venue.TIER_CLAIMED, Venue.TIER_VERIFIED_INDEX])
def test_an_owner_cannot_rename_a_confirmed_venue(tier):
    client, org, _user = _owner()
    venue = Venue.objects.create(name='Real Journal', slug='real-journal', venue_type='journal', organization=org,
                                 trust_tier=tier)
    response = client.patch(f'/api/admin/venues/{venue.id}/', {'name': 'Nature'}, content_type='application/json')
    assert response.status_code == 403 and response.json()['code'] == 'rename_needs_approval'
    venue.refresh_from_db()
    assert venue.name == 'Real Journal'
    # Other edits, and saving the same name, still work.
    assert client.patch(f'/api/admin/venues/{venue.id}/', {'name': 'Real Journal', 'description': 'New text'},
                        content_type='application/json').status_code == 200


@pytest.mark.django_db
def test_an_owner_can_rename_a_listed_venue_and_flexee_can_rename_any():
    client, org, _user = _owner()
    listed = Venue.objects.create(name='Draft name', slug='draft-name', venue_type='journal', organization=org,
                                  trust_tier=Venue.TIER_LISTED)
    assert client.patch(f'/api/admin/venues/{listed.id}/', {'name': 'Better name'},
                        content_type='application/json').status_code == 200
    claimed = Venue.objects.create(name='Real Journal', slug='real-journal', venue_type='journal', organization=org,
                                   trust_tier=Venue.TIER_CLAIMED)
    assert _superuser().patch(f'/api/admin/venues/{claimed.id}/', {'name': 'Real Journal (new title)'},
                              content_type='application/json').status_code == 200
