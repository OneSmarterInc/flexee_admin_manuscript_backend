"""Manuscript versions (8 October instructions, 2.4).

The Manuscript row is the working copy every existing pipeline reads (readiness, matching, packets).
A ManuscriptVersion is a snapshot with its own file. The current version mirrors the working copy;
once a version has been submitted somewhere it is frozen, and a revision becomes a new version
instead of overwriting the text an earlier brief, match or submission was built on.
"""
import os

from django.db import transaction
from django.db.models import Max

from .models import Manuscript, ManuscriptVersion, VenueMatch, VenueSubmission

EDITABLE_SUBMISSION_STATUSES = {'draft', 'packet_ready'}


def max_versions():
    try:
        return max(2, int(os.getenv('MANUSCRIPT_MAX_VERSIONS', '20')))
    except ValueError:
        return 20


def _snapshot(manuscript):
    return {
        'file': manuscript.manuscript_file.name if manuscript.manuscript_file else '',
        'filename': manuscript.manuscript_filename or '',
        'bytes': manuscript.manuscript_bytes or 0,
        'sha256': manuscript.manuscript_sha256 or '',
        'title': manuscript.title or '',
        'abstract': manuscript.abstract or '',
        'keywords': list(manuscript.keywords or []),
        'manuscript_type': manuscript.manuscript_type or '',
        'parsed_profile': dict(manuscript.parsed_profile or {}),
    }


def create_initial_version(manuscript, *, source='upload'):
    """Version 1 for a new manuscript. Shares the working copy's stored file (no copy)."""
    version = ManuscriptVersion.objects.create(manuscript=manuscript, number=1, source=source, **_snapshot(manuscript))
    Manuscript.objects.filter(id=manuscript.id).update(current_version=version)
    manuscript.current_version = version
    return version


def sync_current_version(manuscript):
    """Copy the working copy into its current version (after an edit allowed before submission)."""
    if not manuscript.current_version_id:
        return create_initial_version(manuscript)
    ManuscriptVersion.objects.filter(id=manuscript.current_version_id).update(**_snapshot(manuscript))
    return manuscript.current_version


def locked_submissions(version_id):
    """Submissions that went beyond a draft packet with this version: it can no longer change."""
    return VenueSubmission.objects.filter(version_id=version_id).exclude(status__in=EDITABLE_SUBMISSION_STATUSES)


def version_locked(manuscript):
    if not manuscript.current_version_id:
        return manuscript.venue_submissions.exclude(status__in=EDITABLE_SUBMISSION_STATUSES).exists()
    return locked_submissions(manuscript.current_version_id).exists()


def file_in_use(name, *, exclude_version_id=None):
    """True if any version (other than the given one) or working copy still points at this stored file."""
    if not name:
        return False
    versions = ManuscriptVersion.objects.filter(file=name)
    if exclude_version_id:
        versions = versions.exclude(id=exclude_version_id)
    return versions.exists() or Manuscript.objects.filter(manuscript_file=name).exists()


def create_new_version(manuscript, *, upload, content_bytes, sha256, change_note, revised_after=None, fields=None):
    """Make version N+1 from a new upload and point the working copy at it. Nothing earlier changes.

    Must run inside a transaction holding a lock on the manuscript row.
    Returns (version, reset_draft_submissions).
    """
    fields = fields or {}
    if manuscript.current_version_id:
        sync_current_version(manuscript)  # freeze the outgoing version's final state (parsed profile, title…)
    else:
        create_initial_version(manuscript, source='migration')
    number = (manuscript.versions.aggregate(top=Max('number'))['top'] or 0) + 1
    version = ManuscriptVersion(
        manuscript=manuscript, number=number, source='upload', change_note=change_note[:5000],
        revised_after=revised_after, filename=upload.name, bytes=content_bytes, sha256=sha256,
        title=fields.get('title') or manuscript.title,
        abstract=fields.get('abstract', manuscript.abstract) or '',
        keywords=fields.get('keywords', manuscript.keywords) or [],
        manuscript_type=fields.get('manuscript_type') or manuscript.manuscript_type,
    )
    version.file.save(upload.name, upload, save=False)
    version.save()

    manuscript.manuscript_file = version.file.name
    manuscript.manuscript_filename = version.filename
    manuscript.manuscript_bytes = version.bytes
    manuscript.manuscript_sha256 = version.sha256
    manuscript.title = version.title
    manuscript.abstract = version.abstract
    manuscript.keywords = version.keywords
    manuscript.manuscript_type = version.manuscript_type
    manuscript.parsed_profile = {}
    manuscript.shortlisted_at = None
    manuscript.current_version = version
    manuscript.save()

    # Draft packets were never sent: they move to the new version and are rebuilt from it.
    from .models import EvidenceFinding
    reset = 0
    for submission in manuscript.venue_submissions.filter(status__in=EDITABLE_SUBMISSION_STATUSES):
        EvidenceFinding.objects.filter(venue_submission=submission).delete()
        submission.version = version
        submission.status = 'draft'
        submission.editorial_brief = {}
        submission.packet = {**{k: v for k, v in (submission.packet or {}).items()
                                if k in {'author_name', 'author_email', 'coauthors', 'disclosure',
                                         'transferred_from_submission_id'}},
                             'manuscript_filename': version.filename, 'title': version.title,
                             'manuscript_type': version.manuscript_type}
        submission.save(update_fields=['version', 'status', 'editorial_brief', 'packet', 'updated_at'])
        reset += 1
    return version, reset


def version_payload(version, *, current_id=None):
    submissions = list(version.venue_submissions.select_related('venue').order_by('created_at'))
    return {
        'id': str(version.id),
        'number': version.number,
        'created_at': version.created_at.isoformat(),
        'filename': '' if version.content_purged_at else version.filename,
        'bytes': 0 if version.content_purged_at else version.bytes,
        'title': version.title,
        'manuscript_type': version.manuscript_type,
        'change_note': version.change_note,
        'source': version.source,
        'is_current': str(version.id) == str(current_id),
        'content_purged': bool(version.content_purged_at),
        'revised_after_submission_id': str(version.revised_after_id) if version.revised_after_id else None,
        'submissions': [{'id': str(s.id), 'venue': s.venue.name, 'status': s.status} for s in submissions],
        'readiness_count': version.readiness_assessments.count(),
        'match_count': VenueMatch.objects.filter(version=version).count(),
    }


def purge_version_file(version, now):
    """Remove one version's file once nothing retained needs it (retention)."""
    if version.content_purged_at:
        return False
    name = version.file.name if version.file else ''
    storage = version.file.storage if version.file else None
    with transaction.atomic():
        ManuscriptVersion.objects.filter(id=version.id).update(file='', bytes=0, abstract='', parsed_profile={},
                                                               content_purged_at=now)
    if name and storage is not None and not file_in_use(name):
        storage.delete(name)
    return True
