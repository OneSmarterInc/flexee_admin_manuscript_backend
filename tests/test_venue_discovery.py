import json
from unittest.mock import patch

import httpx
import pytest
from django.db import IntegrityError
from django.test import Client

from review.auth import AUTHOR_COOKIE_NAME, COOKIE_NAME, issue_author_session, issue_session
from review.models import (
    AuditEvent, Author, DiscoveredVenue, EditorUser, Manuscript, Membership, Organization, Venue,
    VenueAgentConfig, VenueDiscoveryRun,
)
from review.services import venue_discovery as vd


# ---------------------------------------------------------------------------
# Fixtures and fakes (no real network, search or AI is ever used)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'discovery-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_ENABLED', 'true')
    monkeypatch.setenv('VENUE_SEARCH_API_KEY', 'test-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-anthropic-key')
    monkeypatch.setenv('VENUE_DISCOVERY_MODEL', 'claude-test-model')
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    # Every test hostname resolves to a public documentation address.
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})


JOURNAL_HOME = """<html><head><title>Journal of Applied AI in Organizations</title></head><body>
<nav>Home | About</nav>
<h1>Journal of Applied AI in Organizations</h1>
<p>Published by Meridian Academic Publishing.</p>
<p>Aims and scope: empirical research on applied AI in organizations.</p>
<a href="/jaaio/author-guidelines">Author guidelines</a>
<a href="https://other-site.example/elsewhere">Partner</a>
<script>var tracking = 1;</script></body></html>"""

JOURNAL_GUIDE = """<html><head><title>Author guidelines</title></head><body>
<h1>Instructions for authors</h1>
<p>We publish original research articles and review articles.</p>
<p>Manuscripts should not exceed 8,000 words, including references.</p>
<p>Submit your manuscript through our online system at any time.</p>
<p>A conflict of interest statement is required.</p></body></html>"""


def site_handler(pages):
    def handler(request):
        url = str(request.url).rstrip('/')
        if url.endswith('/robots.txt'):
            return httpx.Response(200, text='User-agent: *\nAllow: /\n', headers={'content-type': 'text/plain'})
        body = pages.get(url)
        if body is None:
            return httpx.Response(404, text='not found', headers={'content-type': 'text/html'})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body, headers={'content-type': 'text/html; charset=utf-8'})
    return handler


def make_fetcher(pages, **config_overrides):
    config = vd.DiscoveryConfig.from_env()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    client = httpx.Client(transport=httpx.MockTransport(site_handler(pages)), follow_redirects=False)
    return vd.SafeFetcher(config, client=client), config


JOURNAL_PAGES = {
    'https://www.meridian-academic.example/jaaio': JOURNAL_HOME,
    'https://www.meridian-academic.example/jaaio/author-guidelines': JOURNAL_GUIDE,
}


def journal_extraction(pages=None, config=None):
    return {
        'name': 'Journal of Applied AI in Organizations',
        'organization_name': 'Meridian Academic Publishing',
        'venue_type': 'journal',
        'acceptance_status': 'accepting',
        'website_url': 'https://www.meridian-academic.example/jaaio',
        'submission_url': 'https://www.meridian-academic.example/jaaio/author-guidelines',
        'submission_types': ['Original Research', 'Review Paper'],
        'description': 'Applied AI in organizations.',
        'aims_scope': 'Empirical research on applied AI in organizations.',
        'article_types': ['Research article', 'Review article'],
        'accepted_methods': [],
        'quality_threshold': 'Manuscripts should make a significant contribution.',
        'reviewer_criteria': ['Applied AI'],
        'policies': {'peer_review': 'Double-blind'},
        'disclosures': ['Conflict of interest statement'],
        'reporting_standards': [],
        'desk_rejection_rules': [],
        'structured_desk_rejection_rules': [
            {'field': 'word_count', 'operator': '>', 'value': 8000, 'message': 'Over the 8,000-word limit.'},
            {'field': 'word_count', 'operator': '>', 'value': 5000, 'message': 'Invented limit.'},
            {'field': 'disclosure', 'operator': 'empty', 'message': 'Subjective rule.'},
        ],
        'required_submission_items': [
            {'label': 'Conflict of interest statement', 'type': 'textarea', 'required': True},
            {'label': 'Cover letter', 'type': 'file'},
            {'label': 'Broken', 'type': 'video'},
        ],
        'retention_days': None,
        'deadlines': {}, 'submission_capacity': {}, 'current_demand': {},
        'config_notes': '',
        'source_evidence': [
            {'field': 'acceptance_status', 'claim': 'Online submission is open.',
             'url': 'https://www.meridian-academic.example/jaaio/author-guidelines', 'source_title': 'Author guidelines',
             'evidence_text': 'Submit your manuscript through our online system at any time.'},
            {'field': 'word_limit', 'claim': 'Maximum 8,000 words.',
             'url': 'https://www.meridian-academic.example/jaaio/author-guidelines', 'source_title': 'Author guidelines',
             'evidence_text': 'should not exceed 8,000 words'},
            {'field': 'accepted_types', 'claim': 'Fabricated quote.',
             'url': 'https://www.meridian-academic.example/jaaio/author-guidelines', 'source_title': 'Author guidelines',
             'evidence_text': 'We also accept poetry and film scripts of any length.'},
        ],
    }


class FakeProvider:
    def __init__(self, results_by_query=None, fail=False):
        self.results_by_query = results_by_query or {}
        self.fail = fail
        self.calls = 0

    def search(self, query, *, max_results=10):
        self.calls += 1
        if self.fail:
            raise vd.DiscoveryFetchError('provider timeout')
        return self.results_by_query.get(query, [])


def admin_client(email='root@example.com', superuser=True):
    user = EditorUser.objects.create(email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    token, _ = issue_session(user.email)
    client.cookies[COOKIE_NAME] = token
    return client, user


def stage_journal():
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    record, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                           extractor=journal_extraction)
    assert outcome == 'created'
    return record


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_candidate_model_persists_evidence_and_status():
    item = DiscoveredVenue.objects.create(name='X', normalized_name='x', venue_type='journal',
                                          source_evidence=[{'field': 'f', 'url': 'https://a.example'}])
    item.refresh_from_db()
    assert item.discovery_status == 'new' and item.acceptance_status == 'unclear'
    assert item.source_evidence[0]['field'] == 'f'


# ---------------------------------------------------------------------------
# Safe fetching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/admin', 'http://localhost:8000/', 'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.5/', 'http://192.168.1.1/', 'http://[::1]/', 'http://100.64.1.1/', 'file:///etc/passwd',
    'ftp://example.org/', 'http://user:pw@example.org/', 'http://intranet.local/',
])
def test_private_and_unsafe_urls_are_blocked(url):
    with pytest.raises(vd.DiscoveryFetchError):
        vd.validate_public_url(url)


def test_hostname_resolving_to_private_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(vd, '_resolve', lambda host: {'10.1.2.3'})
    with pytest.raises(vd.DiscoveryFetchError):
        vd.validate_public_url('https://looks-public.example/')


@pytest.mark.django_db
def test_fetch_valid_official_page_strips_scripts_and_nav():
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    page = fetcher.fetch('https://www.meridian-academic.example/jaaio')
    assert page.title == 'Journal of Applied AI in Organizations'
    assert 'Aims and scope' in page.text
    assert 'tracking' not in page.text and 'Home | About' not in page.text
    assert vd.find_submission_links(page, 2) == ['https://www.meridian-academic.example/jaaio/author-guidelines']


def test_redirect_to_private_address_is_blocked():
    pages = {'https://pub.example/start': httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})}
    fetcher, _ = make_fetcher(pages)
    with pytest.raises(vd.DiscoveryFetchError, match='Blocked'):
        fetcher.fetch('https://pub.example/start')


def test_non_html_and_oversized_responses_are_rejected():
    pages = {
        'https://pub.example/file.pdf': httpx.Response(200, content=b'%PDF', headers={'content-type': 'application/pdf'}),
        'https://pub.example/huge': httpx.Response(200, text='x' * 50_000, headers={'content-type': 'text/html'}),
    }
    fetcher, _ = make_fetcher(pages, page_max_bytes=10_000)
    with pytest.raises(vd.DiscoveryFetchError, match='non-HTML'):
        fetcher.fetch('https://pub.example/file.pdf')
    with pytest.raises(vd.DiscoveryFetchError, match='larger'):
        fetcher.fetch('https://pub.example/huge')


def test_timeout_is_reported_as_fetch_error():
    def boom(request):
        if str(request.url).endswith('/robots.txt'):
            return httpx.Response(404)
        raise httpx.ReadTimeout('slow', request=request)
    config = vd.DiscoveryConfig.from_env()
    fetcher = vd.SafeFetcher(config, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(vd.DiscoveryFetchError, match='Timed out'):
        fetcher.fetch('https://slow.example/page')


def test_robots_disallow_is_respected():
    def handler(request):
        if str(request.url).endswith('/robots.txt'):
            return httpx.Response(200, text='User-agent: *\nDisallow: /\n', headers={'content-type': 'text/plain'})
        return httpx.Response(200, text='<p>hi</p>', headers={'content-type': 'text/html'})
    config = vd.DiscoveryConfig.from_env()
    fetcher = vd.SafeFetcher(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(vd.DiscoveryFetchError, match='robots'):
        fetcher.fetch('https://private-site.example/journal')


# ---------------------------------------------------------------------------
# Extraction and validation
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_valid_extraction_is_normalised_and_verified():
    record = stage_journal()
    assert record.acceptance_status == 'accepting'
    assert record.submission_types == ['research_article', 'review_article']
    assert record.article_types[:2] == ['Research article', 'Review article']
    # Only the objective 8,000-word rule that appears on the page survives.
    assert record.structured_desk_rejection_rules == [
        {'field': 'word_count', 'operator': '>', 'value': 8000, 'message': 'Over the 8,000-word limit.'}]
    # Requirements: invalid type dropped; "required" only when stated.
    items = {i['label']: i for i in record.required_submission_items}
    assert set(items) == {'Conflict of interest statement', 'Cover letter'}
    assert items['Conflict of interest statement']['required'] is True and items['Cover letter']['required'] is False
    # The fabricated quote was dropped; real quotes kept with timestamps.
    assert all('checked_at' in e for e in record.source_evidence)
    quotes = ' '.join(e['excerpt'] for e in record.source_evidence)
    assert 'poetry' not in quotes  # the fabricated quote was dropped
    assert any(e['field'] == 'acceptance_status' for e in record.source_evidence)
    assert record.confidence >= 80
    assert record.retention_days is None and record.current_demand == {}


def test_malformed_ai_json_is_rejected():
    with pytest.raises(vd.DiscoveryExtractionError):
        vd.parse_ai_json('this is not json')
    assert vd.parse_ai_json('```json\n{"name": "X"}\n```') == {'name': 'X'}


@pytest.mark.parametrize('label,expected', [
    ('Original Research', 'research_article'), ('Systematic Review', 'review_article'), ('Case Study', 'case_study'),
    ('Conference Paper', 'conference_paper'), ('Textbook Proposal', 'book'), ('Research monograph', 'book'),
    ('Practitioner perspective', 'practitioner_article'), ('Letters to the editor', 'other'),
])
def test_submission_type_mapping(label, expected):
    assert vd.normalize_submission_type(label) == expected


@pytest.mark.django_db
def test_accepting_without_official_evidence_becomes_unclear():
    raw = journal_extraction()
    raw['source_evidence'] = []
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    pages = vd.gather_pages('https://www.meridian-academic.example/jaaio', fetcher, config)
    assert vd.validate_extraction(raw, pages)['acceptance_status'] == 'unclear'


@pytest.mark.django_db
def test_explicitly_closed_venue_is_closed():
    closed = {'https://press.example/qlt': '<title>QLT</title><p>We are not accepting new submissions at this time.</p>'}
    raw = {'name': 'Quarterly of Learning Technologies', 'venue_type': 'journal', 'acceptance_status': 'closed',
           'website_url': 'https://press.example/qlt',
           'source_evidence': [{'field': 'acceptance_status', 'claim': 'Submissions suspended.',
                                'url': 'https://press.example/qlt',
                                'evidence_text': 'We are not accepting new submissions at this time.'}]}
    fetcher, config = make_fetcher(closed)
    pages = vd.gather_pages('https://press.example/qlt', fetcher, config)
    assert vd.validate_extraction(raw, pages)['acceptance_status'] == 'closed'


@pytest.mark.django_db
def test_third_party_blog_is_never_high_confidence_accepting():
    blog = {'https://someblog.example/post': '<title>Top journals</title><p>The Annals of X now accepts submissions!</p>'}
    raw = {'name': 'Annals of X', 'venue_type': 'journal', 'acceptance_status': 'accepting',
           'website_url': 'https://annals-x.example',
           'source_evidence': [{'field': 'acceptance_status', 'claim': 'Accepting.', 'url': 'https://someblog.example/post',
                                'evidence_text': 'The Annals of X now accepts submissions!'}]}
    fetcher, config = make_fetcher(blog)
    candidate = vd.validate_extraction(raw, vd.gather_pages('https://someblog.example/post', fetcher, config))
    assert candidate['acceptance_status'] == 'unclear'
    assert candidate['confidence'] <= 30


# ---------------------------------------------------------------------------
# Deduplication and re-checks
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_same_venue_found_twice_updates_one_record():
    stage_journal()
    candidate = vd.validate_extraction(journal_extraction(), [])  # same identity, different (empty) pages
    candidate['submission_url'] = 'https://Meridian-Academic.example/jaaio/author-guidelines/?utm_source=x'
    record, outcome = vd.upsert_candidate(candidate, 'other-fingerprint')
    assert outcome == 'updated'
    assert DiscoveredVenue.objects.count() == 1


@pytest.mark.django_db
def test_unchanged_content_skips_the_ai():
    stage_journal()
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    def must_not_run(*args):
        raise AssertionError('AI should not be called for unchanged pages')
    _record, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                            extractor=must_not_run)
    assert outcome == 'unchanged'


@pytest.mark.django_db
def test_changed_sources_never_overwrite_the_live_config():
    client, _ = admin_client()
    record = stage_journal()
    client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    live = VenueAgentConfig.objects.get()

    changed_guide = JOURNAL_GUIDE.replace('8,000', '7,500')
    fetcher, config = make_fetcher({**JOURNAL_PAGES,
                                    'https://www.meridian-academic.example/jaaio/author-guidelines': changed_guide})
    raw = journal_extraction()
    raw['structured_desk_rejection_rules'] = [{'field': 'word_count', 'operator': '>', 'value': 7500, 'message': 'Over 7,500.'}]
    raw['source_evidence'][1]['evidence_text'] = 'should not exceed 7,500 words'
    updated, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                            extractor=lambda *a: raw)
    assert outcome == 'changed' and updated.discovery_status == 'changed'
    assert 'live Venue Agent was not changed' in updated.change_summary
    live.refresh_from_db()
    assert live.structured_desk_rejection_rules[0]['value'] == 8000
    assert AuditEvent.objects.filter(action='venue_discovery.candidate_changed').exists()


# ---------------------------------------------------------------------------
# One-click Add
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_add_creates_active_venue_and_config_with_all_fields():
    client, _ = admin_client()
    record = stage_journal()
    response = client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    assert response.status_code == 201, response.content
    body = response.json()
    assert body['already_added'] is False

    venue = Venue.objects.get()
    config = VenueAgentConfig.objects.get()
    record.refresh_from_db()
    assert venue.active and config.active and config.version == 1
    assert venue.name == 'Journal of Applied AI in Organizations' and venue.venue_type == 'journal'
    assert venue.organization.name == 'Meridian Academic Publishing' and venue.organization.organization_type == 'journal'
    assert config.aims_scope == record.aims_scope
    assert config.article_types == record.article_types
    assert config.policies == {'peer_review': 'Double-blind'}
    assert config.structured_desk_rejection_rules == record.structured_desk_rejection_rules
    assert config.required_submission_items == record.required_submission_items
    assert config.disclosures == ['Conflict of interest statement']
    assert 'Discovered automatically on' in config.config_notes
    assert record.discovery_status == 'added' and record.added_venue == venue and record.added_venue_config == config
    actions = set(AuditEvent.objects.values_list('action', flat=True))
    assert {'venue.created_from_discovery', 'venue_config.created_from_discovery',
            'venue_discovery.candidate_added'} <= actions


@pytest.mark.django_db
def test_add_twice_is_idempotent():
    client, _ = admin_client()
    record = stage_journal()
    first = client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    second = client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()['already_added'] is True
    assert second.json()['venue']['id'] == first.json()['venue']['id']
    assert Venue.objects.count() == 1 and VenueAgentConfig.objects.count() == 1 and Organization.objects.count() == 1


@pytest.mark.django_db
def test_add_rolls_back_everything_when_config_creation_fails():
    client, _ = admin_client()
    record = stage_journal()
    with patch('review.discovery_api.VenueAgentConfig.objects.create', side_effect=IntegrityError('boom')):
        with pytest.raises(IntegrityError):
            client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    record.refresh_from_db()
    assert Venue.objects.count() == 0 and Organization.objects.count() == 0
    assert record.discovery_status == 'new' and record.added_venue_id is None


@pytest.mark.django_db
def test_invalid_staged_config_is_rejected_without_side_effects():
    client, _ = admin_client()
    record = stage_journal()
    record.retention_days = 99999
    record.save()
    response = client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    assert response.status_code == 422
    assert Venue.objects.count() == 0 and Organization.objects.count() == 0


@pytest.mark.django_db
def test_closed_and_ignored_candidates_cannot_be_added():
    client, _ = admin_client()
    record = stage_journal()
    record.acceptance_status = 'closed'
    record.save()
    assert client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').status_code == 409
    record.acceptance_status = 'accepting'
    record.save()
    client.post(f'/api/admin/venue-discovery/{record.id}/ignore/')
    assert client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').status_code == 409
    assert client.post(f'/api/admin/venue-discovery/{record.id}/restore/').status_code == 200
    assert client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').status_code == 201


@pytest.mark.django_db
def test_existing_organization_is_reused_on_exact_name_only():
    existing = Organization.objects.create(name='meridian academic publishing', organization_type='publisher')
    Organization.objects.create(name='Meridian Academic', organization_type='publisher')
    client, _ = admin_client()
    record = stage_journal()
    client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    assert Venue.objects.get().organization_id == existing.id


@pytest.mark.django_db
def test_book_publisher_scenario_matches_book_manuscripts():
    publisher = {'https://press.example/proposals': (
        '<title>Submitting a book proposal</title><p>We welcome proposals for research monographs, textbooks and '
        'professional books.</p><p>Please include a book overview, target audience, table of contents, competing '
        'titles, author biography and a sample chapter.</p>')}
    raw = {'name': 'Northfield Academic Press', 'organization_name': 'Northfield Academic Press',
           'venue_type': 'publisher', 'acceptance_status': 'accepting', 'website_url': 'https://press.example',
           'submission_url': 'https://press.example/proposals',
           'submission_types': ['Research monograph', 'Textbook', 'Professional book'],
           'policies': {'book_submission_stage': 'proposal', 'accepted_book_types': ['Textbook', 'Professional book']},
           'required_submission_items': [{'label': 'Table of contents', 'type': 'file', 'required': True},
                                         {'label': 'Sample chapter', 'type': 'file', 'required': True}],
           'source_evidence': [{'field': 'acceptance_status', 'claim': 'Invites proposals.',
                                'url': 'https://press.example/proposals', 'evidence_text': 'We welcome proposals'}]}
    fetcher, config = make_fetcher(publisher)
    record, _ = vd.process_candidate('https://press.example/proposals', fetcher, config, extractor=lambda *a: raw)
    assert record.submission_types == ['book'] and record.article_types[0] == 'Book manuscript'

    client, _ = admin_client()
    assert client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/').status_code == 201
    from review.author_api import _normalise_label
    config_obj = VenueAgentConfig.objects.get()
    accepted = {_normalise_label(x) for x in config_obj.article_types}
    assert _normalise_label(dict(Manuscript.TYPE_CHOICES)['book']) in accepted
    assert config_obj.policies['book_submission_stage'] == 'proposal'


# ---------------------------------------------------------------------------
# Author integration
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_only_added_venues_reach_authors():
    record = stage_journal()
    public = Client().get('/api/author/venues/').json()['venues']
    assert public == []  # staged candidates are invisible to authors

    client, _ = admin_client()
    client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    names = [v['name'] for v in Client().get('/api/author/venues/').json()['venues']]
    assert names == ['Journal of Applied AI in Organizations']


@pytest.mark.django_db
def test_added_journal_is_matched_for_a_research_article(tmp_path, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from review.models import ReadinessAssessment
    settings.MEDIA_ROOT = str(tmp_path)
    client, _ = admin_client()
    record = stage_journal()
    client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')

    author = Author.objects.create(email='a@example.com', name='A', email_verified=True)
    author_client = Client()
    token, _ = issue_author_session(author.id)
    author_client.cookies[AUTHOR_COOKIE_NAME] = token
    created = author_client.post('/api/author/manuscripts/', {
        'title': 'Paper', 'author': 'A', 'email': 'a@example.com', 'manuscript_type': 'research_article',
        'disclosure': 'None.', 'attestation': 'true',
        'manuscript': SimpleUploadedFile('p.md', b'# Paper\n\n## Abstract\nText.\n', content_type='text/markdown'),
    })
    manuscript_id = created.json()['manuscript']['id']
    ReadinessAssessment.objects.create(manuscript_id=manuscript_id, status='completed',
                                       summary={'ready_for_matching': True, 'word_count': 500})
    response = author_client.post(f'/api/author/manuscripts/{manuscript_id}/matches/run/')
    assert response.status_code in (200, 201, 202), response.content
    matches = author_client.get(f'/api/author/manuscripts/{manuscript_id}/matches/').json()['matches']
    match = next(m for m in matches if m['venue']['name'] == 'Journal of Applied AI in Organizations')
    assert match['eligibility'] == 'eligible'


# ---------------------------------------------------------------------------
# Permissions and API
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_permissions():
    record = stage_journal()
    url = f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/'
    assert Client().post(url).status_code == 401
    assert Client().get('/api/admin/venue-discovery/').status_code == 401

    author = Author.objects.create(email='a@example.com', name='A', email_verified=True)
    author_client = Client()
    token, _ = issue_author_session(author.id)
    author_client.cookies[AUTHOR_COOKIE_NAME] = token
    assert author_client.post(url).status_code == 401

    owner_client, owner = admin_client('owner@example.com', superuser=False)
    Membership.objects.create(user=owner, organization=Organization.objects.create(name='Org'), role='owner')
    assert owner_client.post(url).status_code == 403
    assert owner_client.get('/api/admin/venue-discovery/').status_code == 403
    assert owner_client.post('/api/admin/venue-discovery/run/').status_code == 403
    assert Venue.objects.count() == 0


@pytest.mark.django_db
def test_list_filters_counts_and_detail():
    client, _ = admin_client()
    record = stage_journal()
    DiscoveredVenue.objects.create(name='Closed J', normalized_name='closed j', venue_type='journal',
                                   acceptance_status='closed')
    body = client.get('/api/admin/venue-discovery/?status=new&acceptance=accepting').json()
    assert [i['name'] for i in body['items']] == ['Journal of Applied AI in Organizations']
    # Tab counts follow the status filter; the hidden ones are reported separately.
    assert body['counts']['new'] == 1 and body['counts_all_statuses']['new'] == 2
    assert body['hidden_by_status_filter'] == 1
    assert client.get('/api/admin/venue-discovery/?status=new').json()['hidden_by_status_filter'] == 0
    assert body['settings']['search_configured'] is True
    item = body['items'][0]
    assert item['confidence_label'] == 'high' and item['primary_source_url'].startswith('https://')
    detail = client.get(f'/api/admin/venue-discovery/{record.id}/').json()['item']
    assert detail['aims_scope'] and detail['source_evidence']
    assert 'test-key' not in json.dumps(body) and 'test-anthropic-key' not in json.dumps(body)
    assert body['settings']['mode'] == 'claude_agent' and body['settings']['missing_key'] == 'ANTHROPIC_API_KEY'


@pytest.mark.django_db
def test_run_now_enqueues_the_scheduled_task_once(monkeypatch):
    client, _ = admin_client()
    queued = []
    monkeypatch.setattr('django_q.tasks.async_task', lambda *args, **kw: queued.append(args))
    first = client.post('/api/admin/venue-discovery/run/')
    second = client.post('/api/admin/venue-discovery/run/')
    assert first.status_code == 202 and second.json()['already_running'] is True
    assert queued == [('review.tasks.run_venue_discovery_task', first.json()['run']['id'])]


@pytest.mark.django_db
def test_run_now_when_disabled_makes_no_network_call(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_ENABLED', 'false')
    client, _ = admin_client()
    response = client.post('/api/admin/venue-discovery/run/')
    assert response.status_code == 409 and response.json()['code'] == 'discovery_disabled'
    assert VenueDiscoveryRun.objects.count() == 0


# ---------------------------------------------------------------------------
# Daily run
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_run_processes_candidates_and_survives_one_failure():
    query = vd.query_bank()[0][1]
    provider = FakeProvider({query: [
        vd.SearchResult(url='https://www.meridian-academic.example/jaaio'),
        vd.SearchResult(url='https://broken.example/journal'),
        vd.SearchResult(url='https://en.wikipedia.org/wiki/Some_journal'),
        vd.SearchResult(url='http://127.0.0.1/evil'),
    ]})
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, config=config, provider=provider, fetcher=fetcher, extractor=journal_extraction)
    run.refresh_from_db()
    assert run.status == 'completed'
    assert run.candidates_created == 1
    assert DiscoveredVenue.objects.count() == 1
    stages = [e['url'] for e in run.errors]
    assert 'https://broken.example/journal' in stages and 'http://127.0.0.1/evil' in stages
    assert not any('wikipedia' in u for u in stages)  # third-party sources are skipped, not fetched


@pytest.mark.django_db
def test_run_without_api_key_fails_clearly(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_MODE', 'search_api')
    monkeypatch.setenv('VENUE_SEARCH_API_KEY', '')
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run)
    run.refresh_from_db()
    assert run.status == 'failed' and 'VENUE_SEARCH_API_KEY' in run.summary


@pytest.mark.django_db
def test_disabled_run_makes_no_calls(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_ENABLED', 'false')
    provider = FakeProvider()
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, provider=provider)
    run.refresh_from_db()
    assert run.status == 'failed' and 'disabled' in run.summary and provider.calls == 0


@pytest.mark.django_db
def test_search_provider_errors_do_not_crash_the_run():
    fetcher, config = make_fetcher({})
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, config=config, provider=FakeProvider(fail=True), fetcher=fetcher)
    run.refresh_from_db()
    assert run.status == 'completed' and run.errors and run.errors[0]['stage'] == 'search'


@pytest.mark.django_db
def test_schedule_command_is_idempotent():
    from django.core.management import call_command
    from django_q.models import Schedule
    call_command('ensure_venue_discovery_schedule')
    call_command('ensure_venue_discovery_schedule', hour=3)
    schedules = Schedule.objects.filter(func='review.tasks.run_venue_discovery_task')
    assert schedules.count() == 1 and schedules.first().schedule_type == Schedule.DAILY


# ---------------------------------------------------------------------------
# Claude agent mode (Anthropic web_search + web_fetch; all responses faked)
# ---------------------------------------------------------------------------

GUIDE_URL = 'https://www.meridian-academic.example/jaaio/author-guidelines'
GUIDE_TEXT = ('Instructions for authors. We publish original research articles and review articles. '
              'Manuscripts should not exceed 8,000 words, including references. '
              'Submit your manuscript through our online system at any time. '
              'A conflict of interest statement is required.')


def agent_answer(venues):
    return json.dumps({'venues': venues})


def fetch_block(url, text, title='Author guidelines'):
    return {'type': 'web_fetch_tool_result', 'tool_use_id': 'srvtoolu_f1',
            'content': {'type': 'web_fetch_result', 'url': url,
                        'content': {'type': 'document', 'title': title,
                                    'source': {'type': 'text', 'media_type': 'text/plain', 'data': text}},
                        'retrieved_at': '2026-10-01T02:00:00Z'}}


def agent_response(final_text, *, fetched=None, stop_reason='end_turn', searches=2):
    content = [
        {'type': 'text', 'text': 'Searching for journals.'},
        {'type': 'server_tool_use', 'id': 'srvtoolu_s1', 'name': 'web_search', 'input': {'query': 'journal submit'}},
        {'type': 'web_search_tool_result', 'tool_use_id': 'srvtoolu_s1',
         'content': [{'type': 'web_search_result', 'url': GUIDE_URL, 'title': 'Guidelines', 'encrypted_content': 'x'}]},
        {'type': 'server_tool_use', 'id': 'srvtoolu_f1', 'name': 'web_fetch', 'input': {'url': GUIDE_URL}},
    ]
    for url, text in (fetched or {}).items():
        content.append(fetch_block(url, text))
    if final_text:
        content.append({'type': 'text', 'text': final_text})
    return {'content': content, 'stop_reason': stop_reason,
            'usage': {'input_tokens': 12000, 'output_tokens': 900,
                      'server_tool_use': {'web_search_requests': searches, 'web_fetch_requests': len(fetched or {})}}}


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, settings, messages):
        self.calls.append({'settings': settings, 'messages': [dict(m) for m in messages]})
        return self.responses.pop(0) if self.responses else agent_response(agent_answer([]))


def journal_venue(**overrides):
    venue = journal_extraction()
    venue['submission_url'] = GUIDE_URL
    venue.update(overrides)
    return venue


@pytest.mark.django_db
def test_claude_agent_run_creates_verified_candidate(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal')
    claude = FakeClaude([agent_response(agent_answer([journal_venue()]), fetched={GUIDE_URL: GUIDE_TEXT})])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    run.refresh_from_db()
    assert run.status == 'completed', run.summary
    assert run.candidates_created == 1 and run.queries_run == 2 and run.official_pages_checked == 1
    record = DiscoveredVenue.objects.get()
    assert record.acceptance_status == 'accepting' and record.confidence >= 70
    assert record.structured_desk_rejection_rules[0]['value'] == 8000
    assert all(e['url'] == GUIDE_URL for e in record.source_evidence)

    # The agent got the web tools with limits and third-party domains blocked.
    tools = {t['name']: t for t in vd_agent_tools()}
    assert tools['web_search']['type'] == 'web_search_20250305' and tools['web_search']['max_uses'] >= 1
    assert 'wikipedia.org' in tools['web_fetch']['blocked_domains']
    assert claude.calls[0]['settings']['model'] == 'claude-test-model'

    from review.models import AIUsageEvent
    usage = AIUsageEvent.objects.get(operation='venue_discovery_agent')
    assert usage.status == 'completed' and usage.input_tokens == 12000 and usage.output_tokens == 900


def vd_agent_tools():
    from review.services.venue_discovery_agent import agent_settings, tools_for
    return tools_for(agent_settings())


@pytest.mark.django_db
def test_claude_agent_claims_without_fetched_pages_are_not_trusted(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal')
    claude = FakeClaude([agent_response(agent_answer([journal_venue()]), fetched={})])  # nothing fetched
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    record = DiscoveredVenue.objects.get()
    assert record.acceptance_status == 'unclear'
    assert record.source_evidence == [] and record.structured_desk_rejection_rules == []
    assert record.confidence <= 25


@pytest.mark.django_db
def test_claude_agent_pause_turn_is_continued(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal')
    first = agent_response('', fetched={GUIDE_URL: GUIDE_TEXT}, stop_reason='pause_turn')
    second = {'content': [{'type': 'text', 'text': agent_answer([journal_venue()])}], 'stop_reason': 'end_turn',
              'usage': {'input_tokens': 500, 'output_tokens': 300}}
    claude = FakeClaude([first, second])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    assert len(claude.calls) == 2
    assert claude.calls[1]['messages'][-1]['role'] == 'assistant'  # paused content sent back unchanged
    assert DiscoveredVenue.objects.get().acceptance_status == 'accepting'


@pytest.mark.django_db
def test_claude_agent_bad_json_is_recorded_and_run_completes(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal,publisher')
    claude = FakeClaude([agent_response('I could not find anything useful.'),
                         agent_response(agent_answer([]))])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    run.refresh_from_db()
    assert run.status == 'completed' and any(e['stage'] == 'agent' for e in run.errors)
    assert len(claude.calls) == 2


@pytest.mark.django_db
def test_claude_agent_without_anthropic_key_fails_clearly(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', '')
    claude = FakeClaude([])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    run.refresh_from_db()
    assert run.status == 'failed' and 'ANTHROPIC_API_KEY' in run.summary and claude.calls == []


@pytest.mark.django_db
def test_claude_agent_stops_when_ai_budget_is_reached(monkeypatch):
    from review.ai_usage import AIBudgetExceeded
    def blocked(**kwargs):
        raise AIBudgetExceeded('Daily AI cost ceiling would be exceeded.')
    monkeypatch.setattr('review.ai_usage.reserve_ai_call', blocked)
    claude = FakeClaude([])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude)
    run.refresh_from_db()
    assert run.status == 'completed' and claude.calls == []
    assert run.errors[0]['stage'] == 'budget'


@pytest.mark.django_db
def test_claude_agent_recheck_flags_changes_and_missing_venues(monkeypatch):
    from datetime import timedelta
    from django.utils import timezone
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal')
    claude = FakeClaude([agent_response(agent_answer([journal_venue()]), fetched={GUIDE_URL: GUIDE_TEXT})])
    vd.run_discovery(VenueDiscoveryRun.objects.create(), create_message=claude)
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'none')  # second run: rechecks only
    client, _ = admin_client()
    record = DiscoveredVenue.objects.get()
    client.post(f'/api/admin/venue-discovery/{record.id}/add-to-venue-agent/')
    other = DiscoveredVenue.objects.create(name='Quiet Journal', normalized_name='quiet journal', venue_type='journal',
                                           website_url='https://quiet.example/journal')
    DiscoveredVenue.objects.update(last_checked_at=timezone.now() - timedelta(days=2))

    changed_text = GUIDE_TEXT.replace('8,000', '7,500')
    changed = journal_venue(structured_desk_rejection_rules=[
        {'field': 'word_count', 'operator': '>', 'value': 7500, 'message': 'Over 7,500.'}])
    changed['source_evidence'][1]['evidence_text'] = 'should not exceed 7,500 words'
    claude2 = FakeClaude([agent_response(agent_answer([changed]), fetched={GUIDE_URL: changed_text})])
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, create_message=claude2)

    prompt = claude2.calls[0]['messages'][0]['content']
    assert GUIDE_URL in prompt and 'https://quiet.example/journal' in prompt
    record.refresh_from_db(); other.refresh_from_db()
    assert record.discovery_status == 'changed'
    assert VenueAgentConfig.objects.get().structured_desk_rejection_rules[0]['value'] == 8000  # live untouched
    assert 'did not return this venue' in other.last_error


# ---------------------------------------------------------------------------
# Free mode: SearXNG + DOAJ search, local Ollama extraction (all faked)
# ---------------------------------------------------------------------------

class FakeHttp:
    """Stands in for httpx.get / httpx.post inside the discovery module."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response(url, kwargs) if callable(response) else response
        return httpx.Response(404, json={})


def _req(method, url):
    return httpx.Request(method, url)


def searxng_json(urls):
    return httpx.Response(200, json={'results': [{'url': u, 'title': 'T', 'content': 'snippet'} for u in urls]},
                          request=_req('GET', 'http://127.0.0.1:8888/search'))


DOAJ_RESULT = {'results': [{'bibjson': {
    'title': 'Journal of Applied AI in Organizations',
    'publisher': {'name': 'Meridian Academic Publishing'},
    'ref': {'journal': 'https://www.meridian-academic.example/jaaio',
            'author_instructions': 'https://www.meridian-academic.example/jaaio/author-guidelines'},
    'subject': [{'term': 'Information technology'}, {'term': 'Management'}],
    'editorial': {'review_process': ['Double blind peer review']},
    'apc': {'has_apc': False},
}}]}


def ollama_reply(content, model='qwen2.5:0.5b-instruct'):
    return httpx.Response(200, json={'model': model, 'message': {'role': 'assistant', 'content': content},
                                     'prompt_eval_count': 2100, 'eval_count': 300},
                          request=_req('POST', 'http://127.0.0.1:11434/api/chat'))


def free_env(monkeypatch, provider='searxng,doaj'):
    monkeypatch.setenv('VENUE_DISCOVERY_MODE', 'search_api')
    monkeypatch.setenv('VENUE_SEARCH_PROVIDER', provider)
    monkeypatch.setenv('VENUE_SEARXNG_URL', 'http://127.0.0.1:8888')
    monkeypatch.setenv('VENUE_DISCOVERY_AI_PROVIDER', 'ollama')
    monkeypatch.setenv('VENUE_DISCOVERY_OLLAMA_MODEL', 'qwen2.5:0.5b-instruct')
    monkeypatch.setenv('VENUE_DISCOVERY_FOCUS', 'information systems, management')


def test_searxng_provider_parses_results_and_explains_json_errors(monkeypatch):
    fake = FakeHttp({'http://127.0.0.1:8888/search': searxng_json(['https://a.example/journal'])})
    monkeypatch.setattr(vd.httpx, 'get', fake)
    provider = vd.SearxngSearchProvider('http://127.0.0.1:8888/')
    assert [r.url for r in provider.search('journal submit', max_results=5)] == ['https://a.example/journal']
    assert fake.calls[0]['params']['format'] == 'json'

    monkeypatch.setattr(vd.httpx, 'get', FakeHttp({'http://127.0.0.1:8888': httpx.Response(403)}))
    with pytest.raises(vd.DiscoveryFetchError, match='json'):
        provider.search('x')


def test_doaj_provider_returns_author_instructions_with_hints(monkeypatch):
    fake = FakeHttp({'https://doaj.org/api/search/journals/': httpx.Response(200, json=DOAJ_RESULT)})
    monkeypatch.setattr(vd.httpx, 'get', fake)
    free_env(monkeypatch)
    provider = vd.DoajSearchProvider()
    assert provider.queries(vd.DiscoveryConfig.from_env()) == [('journal', 'information systems'), ('journal', 'management')]
    [result] = provider.search('information systems', max_results=3)
    assert result.url.endswith('/author-guidelines')
    assert result.hints['name'] == 'Journal of Applied AI in Organizations'
    assert result.hints['organization_name'] == 'Meridian Academic Publishing'
    assert result.hints['policies'] == {'listed_in_doaj': True, 'peer_review': 'Double blind peer review',
                                        'article_processing_charge': 'no'}
    assert 'subject.term' in fake.calls[0]['url'] or 'subject.term' in str(fake.calls[0])


def test_provider_list_parsing(monkeypatch):
    free_env(monkeypatch)
    names = [p.name for p in vd.get_search_providers(vd.DiscoveryConfig.from_env())]
    assert names == ['searxng', 'doaj']
    monkeypatch.setenv('VENUE_SEARXNG_URL', '')
    with pytest.raises(vd.DiscoveryConfigError, match='VENUE_SEARXNG_URL'):
        vd.get_search_providers(vd.DiscoveryConfig.from_env())
    monkeypatch.setenv('VENUE_SEARCH_PROVIDER', 'bing')
    with pytest.raises(vd.DiscoveryConfigError, match='Unknown'):
        vd.get_search_providers(vd.DiscoveryConfig.from_env())


@pytest.mark.django_db
def test_local_ollama_extraction_uses_discovery_model_and_logs_free_usage(monkeypatch):
    free_env(monkeypatch)
    monkeypatch.setenv('OLLAMA_MODEL', 'qwen2.5:0.5b-instruct')
    monkeypatch.setenv('VENUE_DISCOVERY_OLLAMA_MODEL', 'qwen2.5:7b-instruct')
    fake = FakeHttp({'http://127.0.0.1:11434/api/chat': ollama_reply(json.dumps({'name': 'X'}), 'qwen2.5:7b-instruct')})
    monkeypatch.setattr(vd.httpx, 'post', fake)
    page = vd.FetchedPage(url='https://a.example/j', title='J', text='word ' * 50_000)
    raw = vd.extract_with_ai([page], vd.DiscoveryConfig.from_env())
    assert raw == {'name': 'X'}
    body = fake.calls[0]['json']
    assert body['model'] == 'qwen2.5:7b-instruct' and body['format'] == 'json'
    # The prompt is trimmed to fit the configured local context window.
    local = vd.local_ai_settings()
    assert len(body['messages'][0]['content']) // 4 < local['num_ctx'] - local['num_predict']
    from review.models import AIUsageEvent
    usage = AIUsageEvent.objects.get(operation='venue_discovery_extraction')
    assert usage.provider == 'ollama' and usage.actual_cost_usd == 0


def test_local_ollama_missing_model_gives_a_clear_message(monkeypatch):
    free_env(monkeypatch)
    monkeypatch.setattr(vd.httpx, 'post', FakeHttp({'http://127.0.0.1:11434': httpx.Response(404, json={})}))
    with pytest.raises(vd.DiscoveryModelUnavailable, match='ollama pull'):
        vd.extract_with_ai([vd.FetchedPage(url='https://a.example', title='', text='x')], vd.DiscoveryConfig.from_env())


@pytest.mark.django_db
def test_free_run_end_to_end_with_searxng_doaj_and_ollama(monkeypatch):
    free_env(monkeypatch)
    publisher_pages = {
        'https://press.example/proposals': (
            '<title>Book proposals</title><p>We welcome proposals for textbooks and professional books.</p>'),
    }
    monkeypatch.setattr(vd.httpx, 'get', FakeHttp({
        'http://127.0.0.1:8888/search': searxng_json(['https://press.example/proposals',
                                                      'https://en.wikipedia.org/wiki/Some_press']),
        'https://doaj.org/api/search/journals/': httpx.Response(200, json=DOAJ_RESULT),
    }))
    good = json.dumps({
        'name': 'Northfield Press', 'organization_name': 'Northfield Press', 'venue_type': 'publisher',
        'acceptance_status': 'accepting', 'website_url': 'https://press.example',
        'submission_url': 'https://press.example/proposals', 'submission_types': ['Textbook'],
        'source_evidence': [{'field': 'acceptance_status', 'claim': 'Open to proposals.',
                             'url': 'https://press.example/proposals', 'evidence_text': 'We welcome proposals'}]})
    def ollama(url, kwargs):
        text = kwargs['json']['messages'][0]['content']
        return ollama_reply(good if 'press.example' in text else 'not json at all')  # 0.5B fails on the journal
    monkeypatch.setattr(vd.httpx, 'post', FakeHttp({'http://127.0.0.1:11434/api/chat': ollama}))

    fetcher, config = make_fetcher({**JOURNAL_PAGES, **publisher_pages})
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, config=config, fetcher=fetcher)
    run.refresh_from_db()
    assert run.status == 'completed', run.summary
    assert run.candidates_created == 2 and run.queries_run >= 3

    press = DiscoveredVenue.objects.get(name='Northfield Press')
    assert press.acceptance_status == 'accepting' and press.submission_types == ['book']
    # The journal came from DOAJ and the model failed; directory data fills in the identity and the
    # official page's own "Submit your manuscript" sentence proves it is accepting.
    journal = DiscoveredVenue.objects.get(name='Journal of Applied AI in Organizations')
    assert journal.acceptance_status == 'accepting' and journal.organization_name == 'Meridian Academic Publishing'
    assert journal.policies['listed_in_doaj'] is True
    assert {'research_article', 'review_article'} <= set(journal.submission_types)


@pytest.mark.django_db
def test_settings_payload_for_free_mode(monkeypatch):
    free_env(monkeypatch)
    client, _ = admin_client()
    settings = client.get('/api/admin/venue-discovery/').json()['settings']
    assert settings['mode'] == 'search_api' and settings['search_configured'] is True
    assert 'searxng + doaj' in settings['search_provider'] and 'qwen2.5:0.5b-instruct' in settings['search_provider']
    monkeypatch.setenv('VENUE_SEARXNG_URL', '')
    settings = client.get('/api/admin/venue-discovery/').json()['settings']
    assert settings['search_configured'] is False and settings['missing_key'] == 'VENUE_SEARXNG_URL'


@pytest.mark.django_db
def test_ollama_down_does_not_create_thin_records(monkeypatch):
    free_env(monkeypatch, provider='doaj')
    monkeypatch.setattr(vd.httpx, 'get', FakeHttp({'https://doaj.org/api/search/journals/': httpx.Response(200, json=DOAJ_RESULT)}))
    def down(url, **kwargs):
        raise httpx.ConnectError('refused', request=_req('POST', url))
    monkeypatch.setattr(vd.httpx, 'post', down)
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    run = VenueDiscoveryRun.objects.create()
    vd.run_discovery(run, config=config, fetcher=fetcher)
    run.refresh_from_db()
    assert DiscoveredVenue.objects.count() == 0
    assert any('Could not reach Ollama' in e['message'] for e in run.errors)



# ---------------------------------------------------------------------------
# Stability: a weaker re-reading never replaces a better verified result
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_weaker_reading_keeps_the_previous_verified_result():
    record = stage_journal()
    before = (record.acceptance_status, record.confidence, record.aims_scope)
    weak = journal_extraction()
    weak['source_evidence'] = []          # the model missed the quotes this time
    weak['aims_scope'] = 'something else'
    fetcher, config = make_fetcher({**JOURNAL_PAGES,
                                    'https://www.meridian-academic.example/jaaio': JOURNAL_HOME + '<p>new footer</p>'})
    updated, outcome = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                            extractor=lambda *a: weak)
    updated.refresh_from_db()
    assert outcome == 'updated'
    assert (updated.acceptance_status, updated.confidence, updated.aims_scope) == before
    assert updated.last_error == vd.INCONCLUSIVE_NOTE
    assert updated.last_checked_at > record.last_checked_at


@pytest.mark.django_db
def test_verified_closed_status_still_replaces_accepting():
    record = stage_journal()
    closed_guide = '<title>Author guidelines</title><p>We are not accepting new submissions at this time.</p>'
    raw = journal_extraction()
    raw['acceptance_status'] = 'closed'
    raw['source_evidence'] = [{'field': 'acceptance_status', 'claim': 'Submissions suspended.',
                               'url': 'https://www.meridian-academic.example/jaaio/author-guidelines',
                               'evidence_text': 'We are not accepting new submissions at this time.'}]
    fetcher, config = make_fetcher({**JOURNAL_PAGES,
                                    'https://www.meridian-academic.example/jaaio/author-guidelines': closed_guide})
    updated, _ = vd.process_candidate('https://www.meridian-academic.example/jaaio', fetcher, config,
                                      extractor=lambda *a: raw)
    assert updated.acceptance_status == 'closed' and updated.last_error == ''


@pytest.mark.django_db
def test_list_marks_venues_checked_by_the_latest_run():
    from datetime import timedelta
    from django.utils import timezone
    client, _ = admin_client()
    old = stage_journal()
    DiscoveredVenue.objects.filter(id=old.id).update(last_checked_at=timezone.now() - timedelta(hours=2))
    fresh = DiscoveredVenue.objects.create(name='Fresh J', normalized_name='fresh j', venue_type='journal',
                                           acceptance_status='accepting')
    VenueDiscoveryRun.objects.create(status='completed', started_at=timezone.now() - timedelta(minutes=5),
                                     completed_at=timezone.now())
    items = {i['name']: i for i in client.get('/api/admin/venue-discovery/?status=new&acceptance=accepting').json()['items']}
    assert items['Fresh J']['checked_in_last_run'] is True
    assert items['Journal of Applied AI in Organizations']['checked_in_last_run'] is False



# ---------------------------------------------------------------------------
# Sources variety, OpenAlex, phrase signals, sorting
# ---------------------------------------------------------------------------

def test_registrable_domain_handles_academic_country_domains():
    assert vd.registrable_domain('journal.unpad.ac.id') == 'unpad.ac.id'
    assert vd.registrable_domain('ejournal.undip.ac.id') == 'undip.ac.id'
    assert vd.registrable_domain('www.tandfonline.com') == 'tandfonline.com'
    assert vd.registrable_domain('journals.example.co.uk') == 'example.co.uk'
    assert vd.country_of('https://journal.unpad.ac.id/x') == 'ID'
    assert vd.country_of('https://www.tandfonline.com/x') == ''
    assert vd.country_of('https://www.tandfonline.com/x', 'gb') == 'GB'


def test_openalex_provider_returns_homepages_with_hints(monkeypatch):
    free_env(monkeypatch, provider='openalex')
    payload = {'results': [
        {'display_name': 'MIS Quarterly', 'host_organization_name': 'MIS Research Center',
         'homepage_url': 'https://misq.umn.edu', 'country_code': 'US', 'is_oa': False},
        {'display_name': 'No Homepage Journal', 'homepage_url': None},
    ]}
    fake = FakeHttp({'https://api.openalex.org/sources': httpx.Response(200, json=payload)})
    monkeypatch.setattr(vd.httpx, 'get', fake)
    provider = vd.get_search_providers(vd.DiscoveryConfig.from_env())[0]
    assert provider.name == 'openalex'
    assert provider.queries(vd.DiscoveryConfig.from_env()) == [('journal', 'information systems'), ('journal', 'management')]
    [result] = provider.search('information systems', max_results=5)
    assert result.url == 'https://misq.umn.edu'
    assert result.hints['organization_name'] == 'MIS Research Center' and result.hints['country'] == 'US'
    params = fake.calls[0]['params']
    assert params['filter'] == 'type:journal' and params['sort'] == 'cited_by_count:desc' and params['page'] >= 1


def test_varied_entries_limit_country_and_site_and_mix_sources(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_MAX_PER_COUNTRY', '2')
    config = vd.DiscoveryConfig.from_env()
    config.max_candidates_per_run = 6
    doaj = [(f'https://journal{i}.univ{i}.ac.id/j', {'country': 'ID'}) for i in range(5)]
    openalex = [('https://misq.umn.edu', {'country': 'US'}), ('https://www.tandfonline.com/a', {'country': 'GB'}),
                ('https://www.tandfonline.com/b', {'country': 'GB'}), ('https://www.emerald.com/j', {'country': 'GB'})]
    picked = [url for url, _ in vd.pick_varied_entries([doaj, openalex], config)]
    assert sum('.ac.id' in u for u in picked) == 2                      # max 2 per country
    assert sum('tandfonline.com' in u for u in picked) == 1             # max 1 per website
    assert picked[0].endswith('ac.id/j') and picked[1] == 'https://misq.umn.edu'  # sources interleaved


def test_page_rotation_changes_by_day(monkeypatch):
    from datetime import date
    pages = set()
    for day in range(1, 6):
        monkeypatch.setattr(vd.timezone, 'localdate', lambda d=day: date(2026, 10, d))
        pages.add(vd.results_page_for_today(5))
    assert pages == {1, 2, 3, 4, 5}


OJS_HOME = """<html><head><title>Jurnal Sistem Informasi</title></head><body>
<div class="pkp_block block_make_submission"><a href="/index.php/jsi/about/submissions">Make a Submission</a></div>
<p>The journal publishes original research articles and case studies in information systems.</p></body></html>"""


@pytest.mark.django_db
def test_page_phrases_prove_accepting_when_the_model_fails():
    fetcher, config = make_fetcher({'https://jsi.univ.ac.id/index.php/jsi': OJS_HOME})
    def broken_model(*args):
        raise vd.DiscoveryExtractionError('The AI did not return JSON.')
    record, _ = vd.process_candidate('https://jsi.univ.ac.id/index.php/jsi', fetcher, config, extractor=broken_model,
                                     hints={'name': 'Jurnal Sistem Informasi', 'website_url': 'https://jsi.univ.ac.id',
                                            'organization_name': 'Universitas Example', 'venue_type': 'journal'})
    assert record.acceptance_status == 'accepting'
    assert set(record.submission_types) == {'research_article', 'case_study'}
    status = [e for e in record.source_evidence if e['field'] == 'acceptance_status']
    assert status and 'Make a Submission' in status[0]['excerpt']
    assert record.confidence >= 50  # medium from one page; a fetched submissions page raises it


@pytest.mark.django_db
def test_page_phrases_detect_closed():
    page = '<title>QLT</title><p>Submissions are temporarily closed while we clear our backlog.</p><p>Make a Submission</p>'
    fetcher, config = make_fetcher({'https://press.example/qlt': page})
    record, _ = vd.process_candidate('https://press.example/qlt', fetcher, config,
                                     extractor=lambda *a: {'name': 'QLT', 'website_url': 'https://press.example/qlt'})
    assert record.acceptance_status == 'closed'


@pytest.mark.django_db
def test_list_sorting_newest_first_by_default():
    from datetime import timedelta
    from django.utils import timezone
    client, _ = admin_client()
    now = timezone.now()
    for name, conf, mins in [('Old high', 95, 300), ('New low', 30, 1), ('Middle', 60, 60)]:
        DiscoveredVenue.objects.create(name=name, normalized_name=name.lower(), venue_type='journal',
                                       acceptance_status='accepting', confidence=conf,
                                       first_discovered_at=now - timedelta(minutes=mins))
    names = lambda q: [i['name'] for i in client.get('/api/admin/venue-discovery/?status=new' + q).json()['items']]
    assert names('') == ['New low', 'Middle', 'Old high']
    assert names('&sort=confidence') == ['Old high', 'Middle', 'New low']
