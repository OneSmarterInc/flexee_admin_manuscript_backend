"""Security audit, A02: quoted manuscript text must go when the manuscript goes.

_persist_readiness_evidence writes EvidenceFinding rows with venue_submission=None and up to 1500
characters of the manuscript in `excerpt`. The retention sweep deleted readiness assessments, venue
matches and embeddings, and the submission-scoped evidence rows — but never the manuscript-scoped
ones. So after the author had been told their manuscript was deleted, verbatim passages of it stayed
in the database for ever.

Fails on main at a8af296.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import (
    Author,
    EvidenceFinding,
    Manuscript,
    Organization,
    Venue,
    VenueSubmission,
)
from review.tasks import sweep_retention_task
from tests.test_retention_policy import make_manuscript, retention_env  # noqa: F401


SECRET = 'The unpublished result is that the catalyst yields 94 percent under mild conditions.'


def _expired_manuscript():
    author = Author.objects.create(email='evidence@example.com', name='Evidence Author',
                                   password_hash='x')
    manuscript = make_manuscript(author)
    org = Organization.objects.create(name='Evidence Org')
    venue = Venue.objects.create(organization=org, name='Evidence Journal',
                                 slug='evidence-journal', venue_type='journal')
    VenueSubmission.objects.create(
        manuscript=manuscript, venue=venue, status='rejected',
        submitted_at=timezone.now() - timedelta(days=120),
        retention_expires_at=timezone.now() - timedelta(days=1),
    )
    return manuscript


@pytest.mark.django_db
def test_the_purge_removes_manuscript_text_quoted_in_evidence_findings():
    manuscript = _expired_manuscript()
    # A readiness run quotes the manuscript: no venue submission, so the sweep's own delete misses it.
    EvidenceFinding.objects.create(
        manuscript=manuscript, venue_submission=None, finding_type='readiness.claim',
        claim='The manuscript states its result without a confidence interval.',
        source_type='manuscript', source_locator='page 4', excerpt=SECRET,
    )

    sweep_retention_task()

    manuscript.refresh_from_db()
    assert manuscript.content_purged_at is not None, 'the manuscript was not purged at all'
    remaining = EvidenceFinding.objects.filter(manuscript_id=manuscript.id)
    assert not remaining.exists(), 'quoted manuscript text outlived the manuscript'
    assert not EvidenceFinding.objects.filter(excerpt__icontains='94 percent').exists()


@pytest.mark.django_db
def test_evidence_is_kept_while_the_manuscript_is_still_live():
    """The fix must not delete evidence from a manuscript that is still inside its window."""
    author = Author.objects.create(email='live-evidence@example.com', name='Live Author',
                                   password_hash='x')
    manuscript = make_manuscript(author)
    org = Organization.objects.create(name='Live Evidence Org')
    venue = Venue.objects.create(organization=org, name='Live Journal', slug='live-journal',
                                 venue_type='journal')
    VenueSubmission.objects.create(
        manuscript=manuscript, venue=venue, status='under_review',
        submitted_at=timezone.now() - timedelta(days=2),
        retention_expires_at=timezone.now() + timedelta(days=200),
    )
    EvidenceFinding.objects.create(
        manuscript=manuscript, venue_submission=None, finding_type='readiness.claim',
        claim='Still useful.', source_type='manuscript', excerpt=SECRET,
    )

    sweep_retention_task()

    manuscript.refresh_from_db()
    assert manuscript.content_purged_at is None
    assert EvidenceFinding.objects.filter(manuscript_id=manuscript.id).count() == 1
