"""Bug review 2 (14 October), Medium findings in the Venue Index (X5, X10, X11)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import RulesAttempt, VenueIndexRun
from review.services import index_screening as scr
from review.services import venue_index as vi
from review.services.index_report import screening_overturned
from tests.test_index_rules import env, journal, run_rules  # noqa: F401  (env: autouse fixture)
from tests.test_rules_escalation import Scripted, stages
from tests.test_venue_discovery import JOURNAL_PAGES, make_fetcher
from tests.test_venue_discovery import journal_extraction as unquoted_extraction


@pytest.mark.django_db
def test_attempt_called_adequate_must_still_have_the_required_fields_after_unquoted_rules_are_dropped():
    """extract_with_escalation judges adequacy (missing_fields: aims_scope, article_types present?) on the
    candidate BEFORE keep_quoted_rules drops fields without their own quote. journal_extraction has an
    aims_scope with no scope quote and a fabricated article-types quote: the local attempt is recorded
    'adequate' with no missing fields, no retry is made, and the stored rules have neither field."""
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=Scripted(unquoted_extraction))
    item.refresh_from_db()
    stored = item.discovered
    attempt = RulesAttempt.objects.get(record=item, attempt=1)
    if not stored.aims_scope:
        assert 'aims_scope' in attempt.missing_fields or len(stages(item)) > 1, (
            stages(item), attempt.missing_fields, stored.aims_scope, stored.article_types)


@pytest.mark.django_db
def test_screening_overturned_compares_with_what_screening_decided_not_with_any_negative_flag():
    """A journal with only a weak flag (publisher not stated, 1 point < threshold 3) is screened 'clear'.
    A person excluding it overturns the screening, but index_report counts any negative flag as 'flagged'."""
    item = journal(publisher='')
    scr.screen_record(item, set())
    item.refresh_from_db()
    assert item.screening_status == 'clear' and item.screening_flags  # clear, with one weak negative flag
    scr.decide(item, decision='exclude', user_email='a@x', criteria=['unverifiable_board'],
               evidence_urls=['https://example.org/board'])
    result = screening_overturned()
    assert result['clear_but_excluded'] == 1, result


@pytest.mark.django_db
def test_force_close_does_not_close_a_run_that_started_recently_after_waiting_in_the_queue():
    """force_close_runs / close_stale_runs use created_at. A manual run queued behind a 25-minute discovery
    run on the single worker (FLEXEE_Q_WORKERS=1) starts late; --force (or any start after an hour) then
    closes it while it is working, and a second run can start alongside it."""
    run = VenueIndexRun.objects.create(mode='enrich', trigger='manual', status='processing')
    now = timezone.now()
    VenueIndexRun.objects.filter(id=run.id).update(created_at=now - timedelta(minutes=40),
                                                   started_at=now - timedelta(minutes=10))
    assert vi.force_close_runs(now=now) == 0
    run.refresh_from_db()
    assert run.status == 'processing'
