"""Build plan step 2: the venue index spine (OpenAlex, Crossref, DOAJ, ISSN checks)."""
import json
from datetime import timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from review.auth import COOKIE_NAME, issue_session
from review.models import EditorUser, IndexedVenue, Organization, Venue, VenueIndexRun
from review.services import venue_index as vi


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'index-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_INDEX_POLITE_DELAY', '0')
    monkeypatch.setenv('VENUE_INDEX_MIN_WORKS', '30')


# ---------------------------------------------------------------------------
# Fixtures shaped like the real API responses
# ---------------------------------------------------------------------------

def topic(subfield_id, name, count):
    return {'id': f'https://openalex.org/T{subfield_id}0', 'display_name': f'{name} topic', 'count': count,
            'subfield': {'id': f'https://openalex.org/subfields/{subfield_id}', 'display_name': name},
            'field': {'id': 'https://openalex.org/fields/14', 'display_name': 'Business'},
            'domain': {'id': 'https://openalex.org/domains/2', 'display_name': 'Social Sciences'}}


def source(sid, title, *, issn_l, issns=None, topics=None, works=500, oa=False, doaj=False, last_year=2026,
           publisher='Meridian Academic', type_='journal'):
    return {
        'id': f'https://openalex.org/{sid}', 'issn_l': issn_l, 'issn': issns or [issn_l], 'display_name': title,
        'host_organization_name': publisher, 'country_code': 'gb', 'type': type_, 'is_oa': oa, 'is_in_doaj': doaj,
        'is_core': True, 'is_ojs': False, 'homepage_url': f'https://{sid.lower()}.example', 'apc_usd': 1500 if oa else None,
        'works_count': works, 'cited_by_count': works * 10,
        'summary_stats': {'2yr_mean_citedness': 2.5, 'h_index': 40, 'i10_index': 100},
        'first_publication_year': 1995, 'last_publication_year': last_year,
        'topics': topics if topics is not None else [topic('1404', 'Management Information Systems', 300),
                                                     topic('1408', 'Strategy and Management', 100)],
    }


MIS = source('S1', 'Journal of Information Systems Practice', issn_l='0048-7333', issns=['0048-7333', '1873-7625'])
OPS = source('S2', 'Operations and Supply Chain Review', issn_l='1932-6203', oa=True, doaj=True,
             topics=[topic('1803', 'Management Science and Operations Research', 200)])
MEDICINE = source('S3', 'Clinical Cardiology Letters', issn_l='0000-0019',
                  topics=[topic('2705', 'Cardiology', 900), topic('1404', 'Management Information Systems', 5)])
TINY = source('S4', 'Tiny New Journal', issn_l='0000-0027', works=5)
DEAD = source('S5', 'Ceased Management Quarterly', issn_l='0000-0035', last_year=2015)


def crossref_payload(issns, years=((2019, 40), (1996, 3), (2001, 0))):
    return {'status': 'ok', 'message': {
        'title': 'x', 'publisher': 'Meridian Academic', 'ISSN': issns,
        'counts': {'current-dois': 50, 'backfile-dois': 500, 'total-dois': 550},
        'breakdowns': {'dois-by-issued-year': [list(p) for p in years]},
    }}


DOAJ_PAYLOAD = {'total': 1, 'results': [{'id': 'doaj-abc', 'last_updated': '2026-09-17T10:51:00Z', 'bibjson': {
    'title': 'Operations and Supply Chain Review', 'eissn': '1932-6203',
    'publisher': {'name': 'Meridian Academic', 'country': 'GB'},
    'ref': {'aims_scope': 'https://oscr.example/scope', 'journal': 'https://oscr.example/',
            'author_instructions': 'https://oscr.example/guidelines'},
    'editorial': {'review_process': ['Double anonymous peer review'], 'review_url': 'https://oscr.example/review',
                  'board_url': 'https://oscr.example/board'},
    'apc': {'has_apc': True, 'max': [{'price': 1500, 'currency': 'USD'}]},
    'publication_time_weeks': 12, 'oa_start': 2010, 'plagiarism': {'detection': True},
}}]}


class FakeHttp:
    """Answers get_json like IndexHttp, from canned pages. Records every call."""

    def __init__(self, catalogue_pages, *, crossref=None, doaj=None, filter_status=200, search_pages=None,
                 fail=None):
        self.catalogue_pages = catalogue_pages  # list of result lists, chained by cursor
        self.search_pages = search_pages or {}
        self.crossref = crossref if crossref is not None else {}
        self.doaj = doaj if doaj is not None else {}
        self.filter_status = filter_status
        self.fail = fail or set()
        self.calls = []

    def _page(self, pages, cursor):
        index = 0 if cursor == '*' else int(cursor)
        results = pages[index] if index < len(pages) else []
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return 200, {'meta': {'count': sum(len(p) for p in pages), 'next_cursor': next_cursor}, 'results': results}

    def get_json(self, source, url, params=None):
        params = params or {}
        self.calls.append((source, url, dict(params)))
        if source in self.fail:
            raise vi.IndexSourceError(f'{source} returned HTTP 503')
        if source == 'openalex':
            if 'search' in params:
                return self._page(self.search_pages.get(params['search'], []), params['cursor'])
            if self.filter_status != 200:
                return self.filter_status, None
            return self._page(self.catalogue_pages, params['cursor'])
        if source == 'crossref':
            issn = url.rsplit('/', 1)[-1]
            if issn in self.crossref:
                return 200, self.crossref[issn]
            return 404, None
        if source == 'doaj':
            for issn, payload in self.doaj.items():
                if issn in url:
                    return 200, payload
            return 200, {'total': 0, 'results': []}
        raise AssertionError(url)


def run_full(http, mode='full'):
    run, created = vi.start_index_run(mode=mode, trigger='command')
    assert created
    return vi.run_index(run, http=http)


def admin_client(superuser=True, email='root@example.com'):
    user = EditorUser.objects.create(email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


# ---------------------------------------------------------------------------
# ISSN and scope rules
# ---------------------------------------------------------------------------

def test_issn_checksum():
    assert vi.issn_checksum_ok('0048-7333') and vi.issn_checksum_ok('1932-6203') and vi.issn_checksum_ok('2434-561X')
    assert not vi.issn_checksum_ok('0048-7334')
    assert vi.normalize_issn('00487333') == '0048-7333' and vi.normalize_issn('nonsense') == ''


def test_scope_keeps_target_journals_and_drops_the_rest():
    config = vi.IndexConfig()
    keep, details = vi.assess_scope(MIS, config, this_year=2026)
    assert keep and details['primary_subfield'] == 'Management Information Systems' and details['share'] == 1.0
    assert vi.assess_scope(MEDICINE, config, this_year=2026) == (False, mock.ANY)  # one stray IS paper is not enough
    assert vi.assess_scope(MEDICINE, config, this_year=2026)[1]['reason'] == 'mostly outside the target fields'
    assert vi.assess_scope(TINY, config, this_year=2026)[1]['reason'] == 'too few works'
    assert vi.assess_scope(DEAD, config, this_year=2026)[1]['reason'] == 'no longer publishing'
    mixed = source('S9', 'Mixed', issn_l='0000-0043', topics=[topic('2705', 'Cardiology', 60),
                                                             topic('1404', 'Management Information Systems', 40)])
    assert vi.assess_scope(mixed, config, this_year=2026)[0]  # 40% in scope clears the 25% bar


# ---------------------------------------------------------------------------
# A full run
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_full_run_builds_listed_records_and_enriches_them():
    http = FakeHttp([[MIS, MEDICINE], [OPS, TINY, DEAD]],
                    crossref={'0048-7333': crossref_payload(['0048-7333', '1873-7625'])},
                    doaj={'1932-6203': DOAJ_PAYLOAD})
    run = run_full(http)

    assert run.status == 'completed' and run.catalogue_method == 'subfield_filter' and run.catalogue_complete
    assert (run.records_seen, run.out_of_scope, run.created_count, run.enriched_count) == (5, 3, 2, 2)
    assert IndexedVenue.objects.count() == 2

    mis = IndexedVenue.objects.get(openalex_id='S1')
    assert mis.trust_tier == 'listed' and mis.issns == ['0048-7333', '1873-7625']
    assert mis.crossref['registered'] and mis.crossref['total_dois'] == 550
    assert mis.crossref['first_year'] == 1996  # earliest year with DOIs; a zero-count year does not count
    assert mis.issn_checks == {'valid_checksums': True, 'has_issn': True, 'crossref_agrees': True}
    assert mis.doaj == {}  # not open access, so DOAJ was not asked

    ops = IndexedVenue.objects.get(openalex_id='S2')
    assert not ops.crossref['registered']  # Crossref 404
    assert ops.doaj_listed and ops.doaj['guidelines_url'] == 'https://oscr.example/guidelines'
    assert ops.doaj['review_process'] == ['Double anonymous peer review'] and ops.doaj['publication_time_weeks'] == 12
    assert ops.source_ids == {'doaj': 'doaj-abc'}
    doaj_calls = [c for c in http.calls if c[0] == 'doaj']
    assert len(doaj_calls) == 1

    first_call = http.calls[0][2]
    assert first_call['filter'].startswith('type:journal,topics.subfield.id:') and '1404' in first_call['filter']


@pytest.mark.django_db
def test_rerun_updates_flags_missing_and_restores():
    http = FakeHttp([[MIS, OPS]], crossref={}, doaj={})
    run_full(http)
    changed = {**MIS, 'display_name': 'Journal of Information Systems Practice (New Series)'}
    second = run_full(FakeHttp([[changed]]))
    assert second.updated_count == 1 and second.created_count == 0 and second.flagged_missing == 1
    assert IndexedVenue.objects.count() == 2  # kept, never deleted
    assert IndexedVenue.objects.get(openalex_id='S1').title.endswith('(New Series)')
    assert IndexedVenue.objects.get(openalex_id='S2').missing_since is not None

    run_full(FakeHttp([[MIS, OPS]]))
    assert IndexedVenue.objects.get(openalex_id='S2').missing_since is None


@pytest.mark.django_db
def test_doaj_answer_outranks_openalex_copy():
    run_full(FakeHttp([[OPS]], doaj={}))  # OpenAlex says in DOAJ; DOAJ itself has no record
    assert IndexedVenue.objects.get().doaj_listed is False
    run_full(FakeHttp([[OPS]], doaj={}))  # a later catalogue refresh does not overwrite DOAJ's answer
    assert IndexedVenue.objects.get().doaj_listed is False


# ---------------------------------------------------------------------------
# Fallbacks, caps and failures
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('status', [400, 403])
def test_rejected_subfield_filter_falls_back_to_keyword_search(status):
    run_full(FakeHttp([[MIS]]))  # an earlier complete pass
    http = FakeHttp([], filter_status=status, search_pages={'management': [[OPS, MEDICINE]], 'education': [[OPS]]})
    run = run_full(http)
    assert run.status == 'completed' and run.catalogue_method == 'keyword_search'
    assert run.created_count == 1 and run.out_of_scope == 1  # OPS once, despite two searches; MEDICINE dropped
    assert not run.catalogue_complete and run.flagged_missing == 0  # a search can never prove a journal left
    assert IndexedVenue.objects.get(openalex_id='S1').missing_since is None


@pytest.mark.django_db
def test_filter_that_matches_nothing_falls_back_too():
    run = run_full(FakeHttp([], search_pages={'business': [[MIS]]}))
    assert run.catalogue_method == 'keyword_search' and run.created_count == 1


@pytest.mark.django_db
def test_record_cap_stops_early_without_flagging(monkeypatch):
    run_full(FakeHttp([[MIS, OPS]]))
    monkeypatch.setenv('VENUE_INDEX_MAX_RECORDS', '1')
    run = run_full(FakeHttp([[OPS, MIS]]))
    assert not run.catalogue_complete and run.flagged_missing == 0


@pytest.mark.django_db
def test_enrichment_failures_are_recorded_and_retried_later():
    http = FakeHttp([[MIS, OPS]], fail={'crossref'})
    run = run_full(http)
    assert run.status == 'completed' and run.enriched_count == 0 and run.pending_after == 2
    assert all(r.enriched_at is None and 'crossref' in r.last_error for r in IndexedVenue.objects.all())
    assert any('crossref' in e['detail'] for e in run.errors)

    retry = run_full(FakeHttp([], crossref={'0048-7333': crossref_payload(['0048-7333'])}), mode='enrich')
    assert retry.enriched_count == 2 and retry.pending_after == 0 and retry.pages_fetched == 0


@pytest.mark.django_db
def test_five_failures_in_a_row_stop_enrichment():
    many = [source(f'S{i}', f'Management Journal {i}', issn_l='0048-7333') for i in range(10, 18)]
    run = run_full(FakeHttp([many], fail={'crossref'}))
    assert len([e for e in run.errors if e['source'] == 'enrichment']) == 6  # five failures + the stop notice
    assert 'stopping enrichment' in run.errors[-1]['detail']


@pytest.mark.django_db
def test_catalogue_outage_fails_the_run_cleanly():
    run_full(FakeHttp([[MIS]]))
    run = run_full(FakeHttp([], fail={'openalex'}))
    assert run.status == 'completed' and not run.catalogue_complete and run.flagged_missing == 0
    assert any(e['source'] == 'openalex' for e in run.errors)


@pytest.mark.django_db
def test_time_limit_leaves_work_for_the_next_run(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_TIME_LIMIT_MINUTES', '1')
    ticks = iter([0, 0] + [10_000] * 1000)  # deadline set, page 1 fetched, then time is up
    run, _ = vi.start_index_run(mode='full', trigger='command')
    run = vi.run_index(run, http=FakeHttp([[MIS], [OPS]]), clock=lambda: next(ticks))
    assert run.status == 'completed' and not run.catalogue_complete
    assert IndexedVenue.objects.count() == 1 and run.enriched_count == 0 and run.pending_after == 1


# ---------------------------------------------------------------------------
# Links to live venues
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_records_link_to_live_venues_and_take_their_tier():
    org = Organization.objects.create(name='Meridian')
    venue = Venue.objects.create(organization=org, name='Journal of Information Systems Practice', slug='jisp',
                                 venue_type='journal', trust_tier='verified_index')
    run = run_full(FakeHttp([[MIS, OPS]]))
    assert run.linked_count == 1
    assert IndexedVenue.objects.get(openalex_id='S1').venue_id == venue.id
    assert IndexedVenue.objects.get(openalex_id='S1').trust_tier == 'verified_index'
    assert IndexedVenue.objects.get(openalex_id='S2').trust_tier == 'listed'


# ---------------------------------------------------------------------------
# Runs, task and schedule
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_only_one_run_at_a_time_and_stale_runs_close():
    first, created = vi.start_index_run(trigger='manual')
    again, created_again = vi.start_index_run(trigger='manual')
    assert created and not created_again and again.id == first.id
    VenueIndexRun.objects.filter(id=first.id).update(created_at=timezone.now() - timedelta(hours=2))
    _, created_new = vi.start_index_run(trigger='manual')
    assert created_new and VenueIndexRun.objects.get(id=first.id).status == 'failed'


@pytest.mark.django_db
def test_daily_catch_up_does_nothing_when_nothing_is_due():
    from review.tasks import run_venue_index_task
    assert run_venue_index_task(mode='enrich') is None
    assert VenueIndexRun.objects.count() == 0


@pytest.mark.django_db
def test_schedule_installs_monthly_and_daily_and_removes_both():
    from django_q.models import Schedule
    from review.index_schedule import set_index_schedule, first_of_next_month
    state = set_index_schedule(enabled=True, tz_name='Asia/Kolkata')
    assert state['enabled'] and Schedule.objects.filter(func='review.tasks.run_venue_index_task').count() == 2
    monthly = Schedule.objects.get(name='flexee-venue-index-monthly')
    assert monthly.schedule_type == Schedule.MONTHLY and "'full'" in monthly.kwargs
    assert monthly.next_run.astimezone(timezone.get_default_timezone()).tzinfo is not None
    set_index_schedule(enabled=False)
    assert Schedule.objects.filter(func='review.tasks.run_venue_index_task').count() == 0
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dec = datetime(2026, 12, 15, 10, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
    assert first_of_next_month(3, 30, 'Asia/Kolkata', now=dec).date().isoformat() == '2027-01-01'


@pytest.mark.django_db
def test_management_command_imports(monkeypatch, capsys):
    monkeypatch.setattr(vi, 'IndexHttp', lambda config: FakeHttp([[MIS]], crossref={'0048-7333': crossref_payload(['0048-7333'])}))
    call_command('import_venue_index')
    out = capsys.readouterr().out
    assert 'Completed' in out and 'Index now holds 1 journals' in out


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_admin_api_is_platform_admin_only():
    assert Client().get('/api/admin/venue-index/').status_code in (401, 403)
    assert admin_client(superuser=False).get('/api/admin/venue-index/').status_code == 403


@pytest.mark.django_db
def test_admin_list_counts_filters_search_and_detail():
    run_full(FakeHttp([[MIS, OPS]], crossref={'0048-7333': crossref_payload(['0048-7333'])}, doaj={'1932-6203': DOAJ_PAYLOAD}))
    client = admin_client()
    body = client.get('/api/admin/venue-index/').json()
    assert body['counts']['total'] == 2 and body['counts']['crossref'] == 1 and body['counts']['doaj'] == 1
    assert body['filter_counts']['open_access'] == 1 and body['pagination']['total'] == 2
    assert body['items'][0]['trust']['tier'] == 'listed'
    assert body['last_run']['status'] == 'completed' and body['schedule']['enabled'] is False
    assert {f['name'] for f in body['counts']['by_field']} == {'Management Information Systems',
                                                               'Management Science and Operations Research'}

    assert [i['title'] for i in client.get('/api/admin/venue-index/?filter=doaj').json()['items']] == \
        ['Operations and Supply Chain Review']
    assert client.get('/api/admin/venue-index/?q=1873-').json()['pagination']['total'] == 0  # issn_l search only
    assert client.get('/api/admin/venue-index/?q=supply').json()['pagination']['total'] == 1
    paged = client.get('/api/admin/venue-index/?page_size=1&page=9').json()
    assert paged['pagination'] == {'page': 2, 'page_size': 1, 'total': 2, 'pages': 2}

    record = IndexedVenue.objects.get(openalex_id='S2')
    detail = client.get(f'/api/admin/venue-index/{record.id}/').json()['item']
    assert detail['openalex_url'] == 'https://openalex.org/S2' and detail['doaj']['board_url'] == 'https://oscr.example/board'


@pytest.mark.django_db
def test_admin_run_now_queues_once_and_schedule_toggles():
    client = admin_client()
    with mock.patch('django_q.tasks.async_task') as queued:
        first = client.post('/api/admin/venue-index/run/', data=json.dumps({'mode': 'enrich'}), content_type='application/json')
        second = client.post('/api/admin/venue-index/run/', data='{}', content_type='application/json')
    assert first.status_code == 202 and first.json()['run']['mode'] == 'enrich' and not first.json()['already_running']
    assert second.json()['already_running'] and queued.call_count == 1

    on = client.post('/api/admin/venue-index/schedule/', data=json.dumps({'enabled': True}), content_type='application/json')
    assert on.status_code == 200 and on.json()['schedule']['enabled'] and on.json()['schedule']['next_daily_checks']
    off = client.post('/api/admin/venue-index/schedule/', data=json.dumps({'enabled': False}), content_type='application/json')
    assert off.json()['schedule']['enabled'] is False


# ---------------------------------------------------------------------------
# The real HTTP layer: polite retries
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def test_http_retries_rate_limits_and_server_errors():
    answers = iter([_Resp(429), _Resp(503), _Resp(200, {'ok': True})])
    sent = []
    http = vi.IndexHttp(vi.IndexConfig(), get=lambda url, **kw: sent.append(kw) or next(answers), sleep=lambda s: None)
    assert http.get_json('crossref', 'https://api.crossref.org/journals/0048-7333') == (200, {'ok': True})
    assert len(sent) == 3 and sent[0]['headers']['User-Agent'].startswith('FlexeeVenueIndex/1.0')


def test_http_404_is_an_answer_and_repeated_outage_is_an_error():
    import httpx
    http = vi.IndexHttp(vi.IndexConfig(), get=lambda url, **kw: _Resp(404), sleep=lambda s: None)
    assert http.get_json('crossref', 'https://x') == (404, None)

    def down(url, **kw):
        raise httpx.ConnectError('down')
    http = vi.IndexHttp(vi.IndexConfig(), get=down, sleep=lambda s: None)
    with pytest.raises(vi.IndexSourceError, match='could not reach doaj'):
        http.get_json('doaj', 'https://x')


def test_http_sends_openalex_key_and_contact_when_set(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'k-123')
    monkeypatch.setenv('VENUE_INDEX_CONTACT_EMAIL', 'editor@flexee.org')
    config = vi.IndexConfig()
    catalogue = vi.OpenAlexCatalogue(http=None, config=config)
    params = catalogue._params({'filter': 'type:journal'})
    assert params['api_key'] == 'k-123' and params['mailto'] == 'editor@flexee.org' and params['per_page'] == 100
    assert config.user_agent == 'FlexeeVenueIndex/1.0 (mailto:editor@flexee.org)'
