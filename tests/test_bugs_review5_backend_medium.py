"""Review 5, R5-6, R5-10 and R5-12."""
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import IndexedVenue, VenueClaim, VenueSubmission
from review.services import author_agents as aa
from review.tasks import sweep_retention_task
from tests.test_author_dashboard_actions import (  # noqa: F401
    author_client, env, make_manuscript, make_venue)


# R5-6 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_6_packets_a_venue_still_has_open_do_not_block_the_sweep(settings, tmp_path, monkeypatch):
    """A packet at 'under_review' is skipped by the loop, but it was still taken as a candidate. Ordered
    oldest-first, enough of them fill the whole batch and nothing else is ever purged."""
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv('RETENTION_SWEEP_BATCH_SIZE', '2')
    _client, author = author_client()

    def copy(status, days_ago, slug):
        manuscript = make_manuscript(author)
        venue = make_venue(f'Journal {slug}', slug)
        row = VenueSubmission.objects.create(manuscript=manuscript, venue=venue,
                                             venue_config=venue.agent_configs.first(),
                                             version=manuscript.current_version, status=status, packet={})
        VenueSubmission.objects.filter(id=row.id).update(
            retention_expires_at=timezone.now() - timedelta(days=days_ago))
        return row

    # Two open packets expired longest ago, so oldest-first puts them at the head of the batch of two.
    blockers = [copy('under_review', 30, 'held-a'), copy('revision_requested', 29, 'held-b')]
    due = copy('rejected', 1, 'due')

    sweep_retention_task()
    assert VenueSubmission.objects.get(id=due.id).retention_purged_at is not None, \
        'the two open packets filled the batch and the expired copy was never reached'
    for held in blockers:
        assert VenueSubmission.objects.get(id=held.id).retention_purged_at is None


# R5-10 --------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_10_the_size_cutoff_keeps_a_journal_an_editor_has_claimed(monkeypatch):
    """The out-of-scope removal was taught to keep claimed journals; the size-cutoff removal was not, and
    VenueClaim.indexed cascades, so the editor's claim went with the record."""
    from tests.test_venue_index import FakeHttp, MIS, run_full, source
    monkeypatch.setenv('VENUE_INDEX_POLITE_DELAY', '0')
    monkeypatch.setenv('VENUE_INDEX_READ_PAGES', 'false')
    small = source('S9', 'Small Claimed Journal', issn_l='0000-0099')
    run_full(FakeHttp([[MIS, small]]))
    record = IndexedVenue.objects.get(openalex_id='S9')
    IndexedVenue.objects.filter(id=record.id).update(metrics={'works_count': 1})
    claim = VenueClaim.objects.create(indexed=record, journal_name=record.title, name='Dr Editor',
                                      email='editor@small.example', role_title='Editor-in-chief',
                                      status='pending_review', email_verified_at=timezone.now())

    monkeypatch.setenv('VENUE_INDEX_MAX_RECORDS', '1')
    run_full(FakeHttp([[MIS]]))
    assert VenueClaim.objects.filter(id=claim.id, status='pending_review').exists(), \
        "the editor's claim was deleted with the index record"


# R5-12 --------------------------------------------------------------------------------------------

def test_r5_12_the_excerpt_shrinks_for_cjk_text():
    """Four characters per token holds for English and overstates CJK about fourfold. Our own estimator
    counts characters too, so nothing raises: the model drops the tail and the check reads as passed."""
    latin = 'Methods and results. ' * 4000
    chinese = '研究方法与结果。' * 4000
    assert aa._chars_per_token(latin) == pytest.approx(4.0)
    assert aa._chars_per_token(chinese) < 2.0
    latin_prompt = aa._anonymization_prompt(latin)
    chinese_prompt = aa._anonymization_prompt(chinese)
    assert len(chinese_prompt) < len(latin_prompt), (len(chinese_prompt), len(latin_prompt))


def test_r5_12_the_end_of_the_manuscript_is_read_too():
    """Author biographies, funding notes and acknowledgements sit at the end, and the opening-only
    excerpt never looked at them."""
    body = 'Neutral methods text. ' * 6000
    text = 'Opening paragraph. ' + body + ' Acknowledgements: thanks to Dr Priya Raman, Wright State.'
    prompt = aa._anonymization_prompt(text)
    assert 'Opening paragraph.' in prompt
    assert 'Priya Raman' in prompt, 'the end of the manuscript was never shown to the check'


def test_r5_12_a_short_manuscript_is_sent_whole():
    text = 'A short paper by Priya Raman at Wright State. Methods and results follow.'
    prompt = aa._anonymization_prompt(text)
    assert text in prompt
    assert 'omitted' not in prompt
