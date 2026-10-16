"""Review 5, R5-3 and R5-5: the purge that never retried, and the review with no time limit."""
import time
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import Manuscript, SubmissionPlan, VenueSubmission
from review.services import review_engine as re_
from review.tasks import sweep_retention_task
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_bugs_high import _venue_of
from tests.test_submission_plan import activate, generate, setup_plan


def _expire(submission, days=1):
    VenueSubmission.objects.filter(id=submission.id).update(
        retention_expires_at=timezone.now() - timedelta(days=days))


# R5-3 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_3_the_purge_is_retried_once_the_plan_is_over(settings, tmp_path):
    """Review 4's P1 skips the manuscript purge while a plan is live. The first loop only reaches a
    manuscript through a submission it is purging on that run, so once every copy had gone the manuscript
    was never looked at again and the author's file stayed for good."""
    settings.MEDIA_ROOT = tmp_path
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    venue = _venue_of(vs, plan['steps'][0])
    packet = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    VenueSubmission.objects.filter(id=packet.id).update(status='rejected')
    _expire(packet)

    sweep_retention_task()                       # the plan is live, so the manuscript is kept
    m.refresh_from_db()
    assert m.content_purged_at is None
    assert VenueSubmission.objects.get(id=packet.id).retention_purged_at is not None

    SubmissionPlan.objects.filter(id=plan['id']).update(status='abandoned')
    sweep_retention_task()                       # now nothing holds it: the copy must go
    m.refresh_from_db()
    assert m.content_purged_at is not None, 'the deferred purge was never retried'
    assert not m.manuscript_file


@pytest.mark.django_db
def test_r5_3_a_draft_plan_nobody_started_stops_holding_the_file(settings, tmp_path, monkeypatch):
    """A plan generated and never activated would otherwise keep the manuscript for ever."""
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv('RETENTION_DRAFT_PLAN_GRACE_DAYS', '30')
    client, author = author_client()
    m, vs = setup_plan(client, author)
    draft = generate(client, m)                  # generated, never activated
    assert SubmissionPlan.objects.get(id=draft['id']).status == 'draft'
    venue = vs['ready_strong']
    packet = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    VenueSubmission.objects.filter(id=packet.id).update(status='rejected')
    _expire(packet)

    sweep_retention_task()
    m.refresh_from_db()
    assert m.content_purged_at is None, 'a fresh draft plan still holds the file'

    SubmissionPlan.objects.filter(id=draft['id']).update(
        updated_at=timezone.now() - timedelta(days=31))
    sweep_retention_task()
    m.refresh_from_db()
    assert m.content_purged_at is not None, 'an abandoned draft plan held the file past the grace period'


# R5-5 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_5_the_chunked_review_stops_inside_its_budget(monkeypatch):
    """A book runs to a hundred chunks or more. Without a budget, chunks x the model timeout reaches far
    past the worker's 30-minute limit, the worker kills the task, and nothing records the failure."""
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setattr(re_, '_review_budget_seconds', lambda: 6)
    text = '\n\n'.join(['Words here. ' * 300] * 120)
    seen = []

    def slow(prompt, **kw):
        seen.append(kw.get('timeout'))
        time.sleep(1)
        raise TimeoutError('the model did not answer')

    monkeypatch.setattr(re_, 'ai_chat_json', slow)
    measured = {'total_words': 20000, 'chapters': [], 'figures': 0, 'checks': []}

    started = time.monotonic()
    with pytest.raises(RuntimeError):            # no chunk was usable, so the review fails as it should
        re_.judge_with_local_model(text, '', re_.ARTICLE_JUDGMENT, 'article', '', measured)
    elapsed = time.monotonic() - started

    assert elapsed < 12, f'ran {elapsed:.0f}s against a 6s budget'
    assert seen and max(t for t in seen if t) <= re_.REVIEW_CHUNK_TIMEOUT


@pytest.mark.django_db
def test_r5_5_a_review_keeps_the_chunks_it_managed_before_the_budget_ran_out(monkeypatch):
    """Whatever was judged before the deadline still counts, rather than being thrown away."""
    import json
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setattr(re_, '_review_budget_seconds', lambda: 6)
    good = json.dumps({'items': [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''}
                                 for i in re_.ARTICLE_JUDGMENT]})
    answers = {'n': 0}

    def two_then_slow(prompt, **kw):
        answers['n'] += 1
        if answers['n'] <= 2:
            return 'qwen', good
        time.sleep(1)
        raise TimeoutError('the model did not answer')

    monkeypatch.setattr(re_, 'ai_chat_json', two_then_slow)
    measured = {'total_words': 20000, 'chapters': [], 'figures': 0, 'checks': []}
    text = '\n\n'.join(['Words here. ' * 300] * 120)

    _m, _items, decision, _s, _l, meta = re_.judge_with_local_model(
        text, '', re_.ARTICLE_JUDGMENT, 'article', '', measured)
    assert meta['mode'] == 'chunked'
    assert decision == re_.DECISION_PASS
