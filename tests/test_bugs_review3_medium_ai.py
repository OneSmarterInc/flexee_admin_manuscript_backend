"""Medium findings of review 3 in AI and background work (Y2, Y3, Y4). Each test failed before its fix."""
import json
from concurrent.futures import Future
from datetime import timedelta

import pytest

from review.models import IndexedVenue, VenueDiscoveryRun, VenueIndexRun
from review.services import index_screening as scr
from review.services import review_engine as re_
from tests.test_index_screening import env, record  # noqa: F401  (env is autouse there)


# ---------------------------------------------------------------------------
# Y2: chunk sizing reserves a fixed 1000 tokens for the prompt wrapper; a book with failed structural
#     checks has a bigger wrapper, so every chunk is "too large" and the review job fails
# ---------------------------------------------------------------------------

def _uneven_book():
    parts = []
    for n in range(1, 13):
        parts.append(f'Chapter {n} Title {n}')
        per = 3200 if n % 2 else 1200   # unbalanced chapters: the balance check fails with a long detail
        parts.append(('word ' * 40 + '\n\n') * (per // 40))
    return '\n'.join(parts)


def test_chunked_review_of_an_unbalanced_book_fits_the_shared_worker(monkeypatch):
    import redis as redis_mod
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('AI_PROVIDER', 'shared_qwen')
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('SHARED_QWEN_POLL_INTERVAL', '0')
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', '8192')

    class FakeRedis:
        def __init__(self):
            self.results = {}

        def ping(self):
            return True

        def rpush(self, queue, raw):
            job = json.loads(raw)
            items = [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''} for i in re_.BOOK_JUDGMENT]
            self.results['ai_result:' + job['job_id']] = json.dumps(
                {'ok': True, 'result': {'model': 'qwen-shared', 'content': json.dumps({'items': items})}})

        def get(self, key):
            return self.results.get(key)

        def delete(self, key):
            self.results.pop(key, None)
    fake = FakeRedis()
    monkeypatch.setattr(redis_mod, 'from_url', lambda *a, **k: fake)
    import review.ai_usage as au
    monkeypatch.setattr(au, 'reserve_ai_call', lambda **kw: None)   # no DB in this test
    monkeypatch.setattr(au, 'complete_ai_call', lambda *a, **kw: None)
    import review.services.ai_provider as ap
    monkeypatch.setattr(ap, 'reserve_ai_call', lambda **kw: None)
    monkeypatch.setattr(ap, 'complete_ai_call', lambda *a, **kw: None)
    monkeypatch.setattr(ap, 'fail_ai_call', lambda *a, **kw: None)
    disclosure = 'I used AI for copy-editing.'
    text = _uneven_book()
    measured = re_.structural_checks(text, 'book')
    assert not all(c['passed'] for c in measured['checks'])
    # Raises RuntimeError("Manuscript review prompt is too large ...") for every chunk on current code.
    _m, _items, _decision, _s, _l, meta = re_.judge_with_local_model(
        f"{text}\n\nAI-Use Disclosure (submitted with the manuscript):\n{disclosure}",
        'Flexee Systems Analysis and Design Simulation', re_.BOOK_JUDGMENT, 'book', disclosure, measured)
    assert meta['mode'] == 'chunked'


# ---------------------------------------------------------------------------
# Y3: page-evidence pass re-screens the copy it loaded before the page fetch: a reviewer's "keep"
#     made meanwhile is undone (back to 'flagged'), so the journal never gets its rules read
# ---------------------------------------------------------------------------

class _SyncExecutor:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *args):
        f = Future()
        f.set_result(fn(*args))
        return f


@pytest.mark.django_db
def test_page_evidence_pass_does_not_undo_a_keep_made_while_pages_were_read(monkeypatch):
    import concurrent.futures
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _SyncExecutor)
    item = record('Checksum Journal', homepage_url='https://cj.example/',
                  issn_checks={'valid_checksums': False, 'has_issn': True})
    scr.screen_record(item, set())
    item.refresh_from_db()
    assert item.screening_status == 'flagged'

    def evidence_while_reviewer_keeps(rec, fetcher):
        fresh = IndexedVenue.objects.get(id=rec.id)
        scr.decide(fresh, decision='keep', user_email='admin@x')
        return [], ''
    monkeypatch.setattr(scr, 'page_evidence', evidence_while_reviewer_keeps)
    run = VenueIndexRun.objects.create(mode='screen')
    scr.gather_page_evidence(run, 'business-is', lambda: True, fetcher=object())
    item.refresh_from_db()
    assert item.screening_status == 'kept', item.screening_status


# ---------------------------------------------------------------------------
# Y4: X1 fix is only checked between categories: one category's agent loop (up to 5 turns, 600 s
#     each) keeps making paid calls past the run time limit, into the worker's 30-minute kill
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_claude_agent_stops_paid_turns_once_the_run_time_limit_is_used(monkeypatch):
    from review.services import venue_discovery as vd
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal')
    monkeypatch.setattr(vd, 'max_run_minutes', lambda: 5)
    run = VenueDiscoveryRun.objects.create(status='queued')
    calls = []

    def slow_turn(settings, messages):
        calls.append(1)
        run.started_at -= timedelta(minutes=4)   # each agent turn takes 4 minutes
        return {'content': [{'type': 'text', 'text': 'working'}], 'stop_reason': 'pause_turn',
                'usage': {'input_tokens': 1000, 'output_tokens': 100}}
    config = vd.DiscoveryConfig.from_env()
    config.enabled, config.mode, config.max_rechecks_per_run = True, 'claude_agent', 0
    vd.run_discovery(run, config=config, create_message=slow_turn)
    assert len(calls) <= 2, f'{len(calls)} paid agent turns, {4 * len(calls)} minutes against a 5-minute limit'
