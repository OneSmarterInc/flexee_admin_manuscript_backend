"""Audit item 16: every readiness check, match and submission belongs to a manuscript version; a
version that rows point at cannot be deleted on its own; a whole manuscript can still be deleted."""
import pytest
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError, ProtectedError

from review.models import (Manuscript, ManuscriptVersion, ReadinessAssessment, SubmissionPlanStep, VenueMatch,
                           VenueSubmission)
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401


@pytest.mark.django_db
def test_a_version_with_rows_cannot_be_deleted_on_its_own():
    _client, author = author_client()
    m = make_manuscript(author)
    ReadinessAssessment.objects.create(manuscript=m, status='completed', summary={})
    with pytest.raises((RestrictedError, ProtectedError)):
        ManuscriptVersion.objects.filter(id=m.current_version_id).delete()
    assert ManuscriptVersion.objects.filter(id=m.current_version_id).exists()


@pytest.mark.django_db
def test_a_whole_manuscript_can_still_be_deleted():
    _client, author = author_client()
    m = make_manuscript(author)
    ReadinessAssessment.objects.create(manuscript=m, status='completed', summary={})
    VenueMatch.objects.create(manuscript=m, venue=make_venue('Field Notes Journal', 'fnj'))
    Manuscript.objects.get(id=m.id).delete()
    assert not ManuscriptVersion.objects.filter(manuscript_id=m.id).exists()


@pytest.mark.django_db
def test_a_row_without_a_version_gets_the_current_one_or_version_1():
    _client, author = author_client()
    m = make_manuscript(author)
    match = VenueMatch.objects.create(manuscript=m, venue=make_venue('Field Notes Journal', 'fnj'))
    assert match.version_id == m.current_version_id
    bare = Manuscript.objects.create(author_account=author, title='No version yet', author_name='P',
                                     author_email=author.email, manuscript_filename='x.md')
    submission = VenueSubmission.objects.create(manuscript=bare, venue=make_venue('Other Journal', 'oj'))
    bare.refresh_from_db()
    assert submission.version.number == 1 and bare.current_version_id == submission.version_id


@pytest.mark.django_db
def test_the_database_refuses_a_missing_version():
    _client, author = author_client()
    m = make_manuscript(author)
    with pytest.raises(IntegrityError):
        with transaction.atomic():   # bulk_create skips save(), so nothing fills the version in
            ReadinessAssessment.objects.bulk_create([ReadinessAssessment(manuscript=m, status='completed', summary={})])


def test_a_sent_version_is_restricted_in_plan_steps():
    field = SubmissionPlanStep._meta.get_field('submitted_version')
    assert field.remote_field.on_delete.__name__ in {'RESTRICT', 'PROTECT'}
