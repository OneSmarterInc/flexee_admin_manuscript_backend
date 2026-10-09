"""Every existing manuscript becomes version 1, sharing its current file (no copy). Existing readiness
assessments, venue matches and venue submissions are attached to that version (instruction 2.4)."""
from django.db import migrations


def forwards(apps, schema_editor):
    Manuscript = apps.get_model('review', 'Manuscript')
    ManuscriptVersion = apps.get_model('review', 'ManuscriptVersion')
    ReadinessAssessment = apps.get_model('review', 'ReadinessAssessment')
    VenueMatch = apps.get_model('review', 'VenueMatch')
    VenueSubmission = apps.get_model('review', 'VenueSubmission')

    for manuscript in Manuscript.objects.filter(current_version__isnull=True).iterator(chunk_size=200):
        purged = manuscript.content_purged_at
        version = ManuscriptVersion.objects.create(
            manuscript_id=manuscript.id, number=1, source='migration',
            file='' if purged else (manuscript.manuscript_file.name or ''),
            filename=manuscript.manuscript_filename or '', bytes=0 if purged else (manuscript.manuscript_bytes or 0),
            sha256=manuscript.manuscript_sha256 or '', title=manuscript.title or '',
            abstract=manuscript.abstract or '', keywords=manuscript.keywords or [],
            manuscript_type=manuscript.manuscript_type or '', parsed_profile=manuscript.parsed_profile or {},
            content_purged_at=purged,
        )
        Manuscript.objects.filter(id=manuscript.id).update(current_version_id=version.id)
        ReadinessAssessment.objects.filter(manuscript_id=manuscript.id, version__isnull=True).update(version_id=version.id)
        VenueMatch.objects.filter(manuscript_id=manuscript.id, version__isnull=True).update(version_id=version.id)
        VenueSubmission.objects.filter(manuscript_id=manuscript.id, version__isnull=True).update(version_id=version.id)


def backwards(apps, schema_editor):
    Manuscript = apps.get_model('review', 'Manuscript')
    Manuscript.objects.update(current_version=None)


class Migration(migrations.Migration):
    dependencies = [('review', '0037_manuscript_versions')]
    operations = [migrations.RunPython(forwards, backwards)]
