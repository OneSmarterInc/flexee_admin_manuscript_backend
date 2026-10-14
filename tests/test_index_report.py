"""The three Venue Index numbers for the 8 October report (section 4)."""
import uuid
from io import StringIO

import pytest
from django.core.management import call_command

from review.models import IndexReviewDecision, RulesAttempt
from review.services.index_report import index_report
from tests.test_index_rules import env, journal  # noqa: F401  (env: autouse fixture)

NEGATIVE = [{'code': 'fee_no_peer_review', 'kind': 'negative', 'weight': 2}]
POSITIVE = [{'code': 'doaj', 'kind': 'positive', 'weight': 0}]


def _chain(record, *steps):
    c = uuid.uuid4()
    for n, (stage, outcome, missing) in enumerate(steps, 1):
        RulesAttempt.objects.create(chain=c, record=record, attempt=n, stage=stage, provider='ollama',
                                    outcome=outcome, missing_fields=missing)


def _seed():
    records = [journal(f'J{i}', openalex_id=f'S{i}') for i in range(5)]
    _chain(records[0], ('local', 'adequate', []))
    _chain(records[1], ('local', 'adequate', []))
    _chain(records[2], ('local', 'inadequate', ['article_types']), ('local_retry', 'adequate', []))
    _chain(records[3], ('local', 'inadequate', ['aims_scope']), ('local_retry', 'error', []),
           ('cloud', 'adequate', []))
    _chain(records[4], ('local', 'inadequate', ['aims_scope']), ('local_retry', 'inadequate', ['aims_scope']))
    # Screening: flagged and kept (overturned), flagged and excluded, clear and kept, clear and excluded
    # (overturned). A later decision on the same record does not count again.
    decide = IndexReviewDecision.objects.create
    decide(record=records[0], decision='keep', flags_snapshot=NEGATIVE, decided_by='a@x')
    decide(record=records[1], decision='exclude', flags_snapshot=NEGATIVE, decided_by='a@x')
    decide(record=records[2], decision='keep', flags_snapshot=POSITIVE, decided_by='a@x')
    decide(record=records[3], decision='exclude', flags_snapshot=[], decided_by='a@x')
    decide(record=records[0], decision='exclude', flags_snapshot=NEGATIVE, decided_by='b@x')
    decide(record=records[4], decision='restore', flags_snapshot=NEGATIVE, decided_by='a@x')


@pytest.mark.django_db
def test_the_three_numbers():
    _seed()
    report = index_report()
    e, s, v = report['escalation'], report['screening'], report['validation']
    assert e['reads'] == 5 and e['rate'] == 0.6 and e['over_threshold'] is True
    assert {f['field']: f['rate'] for f in e['fields']} == {'aims_scope': 0.4, 'article_types': 0.2}
    assert s == {'decided': 4, 'flagged_but_kept': 1, 'clear_but_excluded': 1, 'overturned': 2, 'share': 0.5}
    assert v['attempts'] == 9 and v['failed_validation'] == 4 and v['errors'] == 1
    assert v['failed_validation_share'] == round(4 / 9, 3)
    assert v['by_stage']['local'] == {'attempts': 5, 'inadequate': 3, 'error': 0, 'failed_validation_share': 0.6}
    assert v['reads'] == 5 and v['reads_unresolved'] == 1 and v['reads_unresolved_share'] == 0.2


@pytest.mark.django_db
def test_the_command_prints_the_three_numbers():
    _seed()
    out = StringIO()
    call_command('index_report', stdout=out)
    text = out.getvalue()
    assert '1. Escalation rate: 60.0% of 5 rule reads (ABOVE the threshold of 20.0%)' in text
    assert 'Aims and scope: missing on first try 2 (40.0%)  <- move to cloud' in text
    assert '2. Screening overturned by a person: 50.0% of 4 records decided' in text
    assert '3. Rule-read attempts failing validation: 44.4% (4 of 9 attempts; 1 more were errors)' in text


@pytest.mark.django_db
def test_an_empty_window_says_what_to_run():
    out = StringIO()
    call_command('index_report', '--days', '7', stdout=out)
    assert 'No rule reads or screening decisions in this window yet' in out.getvalue()
    out = StringIO()
    call_command('index_report', '--json', stdout=out)
    assert '"escalation"' in out.getvalue()
