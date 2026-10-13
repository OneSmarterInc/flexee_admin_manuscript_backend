"""Run a test against the database as it was before a migration (audit item 16 made version links
required, so tests of the older version backfill run on the schema they were written for)."""
from contextlib import contextmanager

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE_REQUIRED_VERSIONS = [('review', '0042_smtp_password_encrypted')]


@contextmanager
def schema_at(target):
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    old_apps = MigrationExecutor(connection).loader.project_state(target).apps
    try:
        yield old_apps
    finally:
        latest = MigrationExecutor(connection)
        latest.loader.build_graph()
        latest.migrate(latest.loader.graph.leaf_nodes())


def make_old_manuscript(apps):
    """A manuscript and a readiness row with no version, as before the version backfill."""
    Author = apps.get_model('review', 'Author')
    Manuscript = apps.get_model('review', 'Manuscript')
    ReadinessAssessment = apps.get_model('review', 'ReadinessAssessment')
    author = Author.objects.create(email='old@example.com', name='Old', password_hash='x')
    m = Manuscript.objects.create(author_account=author, title='Old text', author_name='Old',
                                  author_email='old@example.com', manuscript_filename='old.md',
                                  manuscript_file='manuscripts/old.md', manuscript_bytes=3)
    ReadinessAssessment.objects.create(manuscript=m, status='completed', summary={})
    return m


def remove_old_manuscript(apps, manuscript_id):
    apps.get_model('review', 'ReadinessAssessment').objects.filter(manuscript_id=manuscript_id).delete()
    apps.get_model('review', 'Manuscript').objects.filter(id=manuscript_id).update(current_version=None)
    apps.get_model('review', 'ManuscriptVersion').objects.filter(manuscript_id=manuscript_id).delete()
    apps.get_model('review', 'Manuscript').objects.filter(id=manuscript_id).delete()
