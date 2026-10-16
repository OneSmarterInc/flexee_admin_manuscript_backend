"""Review 5, R5-20 and the backend items from the Low list."""
import time
from datetime import timedelta
from pathlib import Path

import pytest

from review import backup_utils
from review.services import author_agents as aa
from tests.test_author_dashboard_actions import author_client, env, make_venue  # noqa: F401


# R5-20 --------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_20_reading_the_file_counts_against_the_budget(monkeypatch):
    """The deadline used to start at the chunk loop, so a slow read of a large PDF beforehand was free
    and the run could overshoot the worker's limit with nothing recorded."""
    from review.models import ManuscriptVersion, Manuscript, ReadinessAssessment
    m = Manuscript.objects.create(title='Probe', manuscript_filename='p.md', manuscript_type='research_article')
    version = ManuscriptVersion.objects.create(manuscript=m, number=1, filename='p.md', title=m.title,
                                               manuscript_type=m.manuscript_type)
    Manuscript.objects.filter(id=m.id).update(current_version=version)
    m.refresh_from_db()
    ReadinessAssessment.objects.create(manuscript=m, version=version, status='completed',
                                       engine_version='mechanical-v1',
                                       summary={'ready_for_matching': True, 'word_count': 500}, findings=[])

    monkeypatch.setattr(aa, '_semantic_budget_seconds', lambda: 3)
    monkeypatch.setattr(aa, 'ai_available', lambda: True)

    def slow_read(_manuscript):
        time.sleep(3.2)                      # the whole budget goes on reading the file
        return '\n\n'.join(['Words here. ' * 400] * 12)

    monkeypatch.setattr(aa, 'load_manuscript_text', slow_read)
    called = []
    monkeypatch.setattr(aa, '_agent_json',
                        lambda *a, **k: called.append(1) or ('m', {}))

    assessment = aa.run_semantic_readiness(m)
    assert called == [], 'the model was called although the budget was already spent'
    assert assessment.summary['budget_exhausted'] is True
    assert assessment.summary['chunks_not_attempted'] > 0


def test_r5_20_citation_checks_stop_when_the_budget_is_spent(monkeypatch):
    """Each Crossref lookup is a network call, and they ran after the deadline had already passed."""
    looked_up = []
    monkeypatch.setattr(aa, '_verify_citation_crossref',
                        lambda citation: looked_up.append(citation) or {'status': 'found'})
    monkeypatch.setattr(aa, '_extract_citations',
                        lambda text: (3, ['Smith 2020', 'Jones 2021', 'Raman 2022']))

    spent = aa._citation_checks('text', 'sha', deadline=time.monotonic() - 1)
    assert looked_up == []
    assert spent['checked'] == 0 and spent['skipped'] == 'out of time'

    aa._citation_checks('text', 'sha', deadline=time.monotonic() + 30)
    assert looked_up, 'with time left the checks still run'


# Backups ------------------------------------------------------------------------------------------

def test_the_leftovers_of_an_interrupted_backup_are_pruned(tmp_path):
    """'.flexee-backup-....partial' never matched startswith(PREFIX), so every failed run stayed on disk
    for good — a full database dump and a full media tarball each time."""
    root = Path(tmp_path)
    old = root / '.flexee-backup-20260101-000000.partial'
    old.mkdir()
    (old / 'db.sql').write_text('x' * 100)
    finished = root / 'flexee-backup-20260101-000000'
    finished.mkdir()
    stale = (time.time() - 86400 * 40)
    for path in (old, finished):
        import os
        os.utime(path, (stale, stale))

    removed = backup_utils.purge_old_backups(root, retention_days=30)
    assert not old.exists(), 'the interrupted backup was left on disk'
    assert not finished.exists()
    assert len(removed) == 2


def test_a_recent_partial_is_left_alone(tmp_path):
    root = Path(tmp_path)
    fresh = root / '.flexee-backup-20260601-000000.partial'
    fresh.mkdir()
    backup_utils.purge_old_backups(root, retention_days=30)
    assert fresh.exists(), 'a backup still being written was deleted'


# X2 pagination ------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_venue_list_is_paged(env):  # noqa: F811
    """It returned every visible venue in one response, built entirely in memory."""
    client, _author = author_client()
    for i in range(7):
        make_venue(f'Journal {i:02d}', f'j{i:02d}')
    body = client.get('/api/author/venues/?limit=3').json()
    assert len(body['venues']) == 3
    assert body['pagination']['total'] >= 7
    second = client.get('/api/author/venues/?limit=3&offset=3').json()
    assert [v['slug'] for v in second['venues']] != [v['slug'] for v in body['venues']]


@pytest.mark.django_db
def test_a_nonsense_page_size_does_not_break_the_list(env):  # noqa: F811
    client, _author = author_client()
    make_venue('Journal A', 'ja')
    assert client.get('/api/author/venues/?limit=abc&offset=-5').status_code == 200
    assert client.get('/api/author/venues/?limit=99999999999999999999').status_code == 200
