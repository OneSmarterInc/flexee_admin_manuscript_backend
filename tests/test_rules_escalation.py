"""Build plan step 6: local model first, one local retry, then the cloud, decided by the validator."""
import pytest
from django.utils import timezone

from review.ai_usage import AIBudgetExceeded
from review.models import AIUsageEvent, IndexedVenue, RulesAttempt
from review.services import rules_escalation as esc
from review.services import venue_discovery as vd
from tests.test_index_rules import admin_client, env, journal, run_rules  # noqa: F401  (env: autouse fixture)
from tests.test_venue_discovery import JOURNAL_PAGES, journal_extraction, make_fetcher

NAME_ONLY = {'name': 'Journal of Applied AI in Organizations', 'venue_type': 'journal'}


class Scripted:
    """An extractor whose answer depends on the attempt (and on which provider is asked)."""

    def __init__(self, *answers, cloud=None):
        self.answers, self.cloud, self.calls = list(answers), cloud, []

    def __call__(self, pages, config):
        self.calls.append(config.ai_provider)
        if config.ai_provider == 'anthropic':
            return self.cloud() if callable(self.cloud) else self.cloud
        answer = self.answers.pop(0) if self.answers else NAME_ONLY
        return answer() if callable(answer) else answer


def stages(record):
    return [(a.stage, a.outcome) for a in RulesAttempt.objects.filter(record=record).order_by('attempt')]


@pytest.mark.django_db
def test_adequate_first_answer_stops_at_the_local_model():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run = run_rules(fetcher, extractor=Scripted(journal_extraction))
    item.refresh_from_db()
    assert item.rules_status == 'ready' and stages(item) == [('local', 'adequate')]
    assert (run.rules_retried, run.rules_escalated) == (0, 0)


@pytest.mark.django_db
def test_inadequate_answer_gets_one_local_retry():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    extractor = Scripted(NAME_ONLY, journal_extraction)
    run = run_rules(fetcher, extractor=extractor)
    item.refresh_from_db()
    assert stages(item) == [('local', 'inadequate'), ('local_retry', 'adequate')]
    first = RulesAttempt.objects.get(record=item, attempt=1)
    assert first.escalated and 'aims_scope' in first.missing_fields
    assert item.rules_status == 'ready' and run.rules_retried == 1 and run.rules_escalated == 0
    assert 'Local retries: 1' in run.summary


@pytest.mark.django_db
def test_cloud_is_off_without_a_key():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    extractor = Scripted(NAME_ONLY, NAME_ONLY, cloud=journal_extraction)
    run_rules(fetcher, extractor=extractor)
    assert extractor.calls == ['ollama', 'ollama'] and len(stages(item)) == 2


@pytest.mark.django_db
def test_escalates_to_the_cloud_when_local_twice_fails(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_ESCALATE', 'anthropic')
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    extractor = Scripted(NAME_ONLY, NAME_ONLY, cloud=journal_extraction)
    run = run_rules(fetcher, extractor=extractor)
    item.refresh_from_db()
    assert extractor.calls == ['ollama', 'ollama', 'anthropic']
    assert stages(item) == [('local', 'inadequate'), ('local_retry', 'inadequate'), ('cloud', 'adequate')]
    assert item.rules_status == 'ready' and run.rules_escalated == 1


@pytest.mark.django_db
def test_cloud_ceiling_per_run(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_ESCALATE', 'anthropic')
    monkeypatch.setenv('VENUE_INDEX_ESCALATIONS_PER_RUN', '1')
    journal('One', openalex_id='S1', homepage='https://one.example/')
    journal('Two', openalex_id='S2', homepage='https://two.example/')
    pages = {'https://one.example': '<html><body><p>Welcome to One.</p></body></html>',
             'https://two.example': '<html><body><p>Welcome to Two.</p></body></html>'}
    fetcher, _ = make_fetcher(pages)
    extractor = Scripted(cloud={'name': 'X', 'venue_type': 'journal'})
    run = run_rules(fetcher, extractor=extractor)
    assert extractor.calls.count('anthropic') == 1 and run.rules_escalated == 1


@pytest.mark.django_db
def test_budget_stop_is_recorded_and_the_run_continues(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_ESCALATE', 'anthropic')
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)

    def over_budget():
        raise AIBudgetExceeded('monthly AI budget reached')
    run = run_rules(fetcher, extractor=Scripted(NAME_ONLY, NAME_ONLY, cloud=over_budget))
    item.refresh_from_db()
    cloud = RulesAttempt.objects.get(record=item, stage='cloud')
    assert cloud.outcome == 'error' and 'budget' in cloud.reason
    assert run.status == 'completed' and item.rules_status in {'ready', 'incomplete'}


@pytest.mark.django_db
def test_local_model_offline_still_stops_the_run():
    journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)

    def offline(*a):
        raise vd.DiscoveryModelUnavailable('Could not reach Ollama at http://127.0.0.1:11434.')
    run = run_rules(fetcher, extractor=offline)
    assert run.rules_attempted == 0 and not RulesAttempt.objects.exists()


@pytest.mark.django_db
def test_retry_tells_the_model_what_failed_and_uses_its_own_operation(monkeypatch):
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    seen = []

    def fake_extract(pages, config, *, operation, feedback='', model=None):
        seen.append((operation, feedback, model))
        return NAME_ONLY if operation == 'index_rules_local' else journal_extraction()
    monkeypatch.setattr(vd, 'extract_with_ai', fake_extract)
    monkeypatch.setenv('VENUE_INDEX_RETRY_OLLAMA_MODEL', 'qwen2.5:7b-instruct')
    run_rules(fetcher, extractor=None)
    item.refresh_from_db()
    assert [s[0] for s in seen] == ['index_rules_local', 'index_rules_retry']
    assert seen[0][1] == '' and 'aims and scope' in seen[1][1] and seen[1][2] == 'qwen2.5:7b-instruct'
    assert item.rules_status == 'ready'


@pytest.mark.django_db
def test_unchanged_pages_that_were_read_well_are_not_read_again():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=Scripted(journal_extraction))
    IndexedVenue.objects.filter(id=item.id).update(rules_status='not_read')
    extractor = Scripted(journal_extraction)
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=extractor)
    assert extractor.calls == [] and RulesAttempt.objects.count() == 1


# ---------------------------------------------------------------------------
# The number that decides everything: escalation rate per field
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_escalation_rate_per_field():
    import uuid
    records = [journal(f'J{i}', openalex_id=f'S{i}') for i in range(5)]

    def chain(record, *steps):
        c = uuid.uuid4()
        for n, (stage, outcome, missing) in enumerate(steps, 1):
            RulesAttempt.objects.create(chain=c, record=record, attempt=n, stage=stage, provider='ollama',
                                        outcome=outcome, missing_fields=missing)
    chain(records[0], ('local', 'adequate', []))
    chain(records[1], ('local', 'adequate', []))
    chain(records[2], ('local', 'inadequate', ['article_types']), ('local_retry', 'adequate', []))
    chain(records[3], ('local', 'inadequate', ['quotes', 'aims_scope']), ('local_retry', 'inadequate', ['aims_scope']),
          ('cloud', 'adequate', []))
    chain(records[4], ('local', 'inadequate', ['aims_scope']), ('local_retry', 'inadequate', ['aims_scope']))
    AIUsageEvent.objects.create(provider='anthropic', operation='index_rules_cloud', status='completed',
                                actual_cost_usd='0.012')

    stats = esc.escalation_stats()
    assert stats['reads'] == 5 and stats['escalation_rate'] == 0.6
    assert stats['resolved'] == {'local': 2, 'local_retry': 1, 'cloud': 1, 'unresolved': 1}
    by_field = {f['field']: f for f in stats['fields']}
    assert by_field['aims_scope']['rate'] == 0.4 and by_field['aims_scope']['move_to_cloud']
    assert by_field['article_types']['rate'] == 0.2 and not by_field['article_types']['move_to_cloud']
    assert stats['cloud_calls'] == 1 and stats['cloud_cost_usd'] == pytest.approx(0.012)


@pytest.mark.django_db
def test_admin_sees_attempts_and_stats():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=Scripted(NAME_ONLY, journal_extraction))
    client = admin_client()
    listed = client.get('/api/admin/venue-index/').json()
    assert listed['escalation']['reads'] == 1 and listed['escalation']['resolved']['local_retry'] == 1
    assert listed['last_run']['rules_retried'] == 1
    detail = client.get(f'/api/admin/venue-index/{item.id}/').json()['item']
    assert [a['stage'] for a in detail['rules_attempts']] == ['local', 'local_retry']


def test_required_fields_can_be_configured(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_REQUIRED_FIELDS', 'article_types, nonsense')
    assert esc.required_fields() == ('article_types',)
    assert esc.missing_fields({'source_evidence': [1], 'confidence': 80, 'article_types': []}, 40) == ['article_types']


@pytest.mark.django_db
def test_local_only_by_default_even_with_an_anthropic_key(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    extractor = Scripted(NAME_ONLY, NAME_ONLY, cloud=journal_extraction)
    run_rules(fetcher, extractor=extractor)
    assert extractor.calls == ['ollama', 'ollama'] and 'anthropic' not in {a.provider for a in RulesAttempt.objects.all()}
    monkeypatch.setenv('VENUE_INDEX_ESCALATE', 'auto')
    assert esc.Escalation.from_env().cloud_enabled  # opt-in still works


@pytest.mark.django_db
def test_failing_retry_model_does_not_stop_the_run(monkeypatch):
    first = journal('One', openalex_id='S1')
    second = journal('Two', openalex_id='S2', homepage='https://two.example/')
    fetcher, _ = make_fetcher({**JOURNAL_PAGES, 'https://two.example': '<html><body><p>Two.</p></body></html>'})
    models = []

    def fake_extract(pages, config, *, operation, feedback='', model=None):
        models.append((operation, model))
        if model == 'qwen2.5:7b-instruct':
            raise vd.DiscoveryModelUnavailable('Could not reach Ollama at http://127.0.0.1:11434.')
        return NAME_ONLY
    monkeypatch.setattr(vd, 'extract_with_ai', fake_extract)
    monkeypatch.setenv('VENUE_DISCOVERY_OLLAMA_MODEL', 'qwen3:1.7b')
    monkeypatch.setenv('VENUE_INDEX_RETRY_OLLAMA_MODEL', 'qwen2.5:7b-instruct')
    run = run_rules(fetcher, extractor=None)
    assert run.status == 'completed' and run.rules_attempted == 2  # both journals read
    retry = RulesAttempt.objects.get(record=first, stage='local_retry')
    assert retry.outcome == 'error' and 'Local retry failed' in retry.reason
    # After the 7B failed once, the second journal's retry used the main model.
    assert RulesAttempt.objects.get(record=second, stage='local_retry').model == 'qwen3:1.7b'
    first.refresh_from_db()
    assert first.rules_status in {'ready', 'incomplete'}
