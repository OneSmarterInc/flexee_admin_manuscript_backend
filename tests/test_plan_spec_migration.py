"""Migration 0040 maps existing plans onto the spec's states without losing what the author saw."""
import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [('review', '0039_submission_plan')]
AFTER = [('review', '0040_submission_plan_spec')]


@pytest.mark.django_db(transaction=True)
def test_existing_plans_and_steps_are_mapped():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    Author = old.get_model('review', 'Author')
    Manuscript = old.get_model('review', 'Manuscript')
    Version = old.get_model('review', 'ManuscriptVersion')
    Org = old.get_model('review', 'Organization')
    Venue = old.get_model('review', 'Venue')
    Plan = old.get_model('review', 'SubmissionPlan')
    Position = old.get_model('review', 'PlanPosition')

    author = Author.objects.create(email='m@example.com', name='M', password_hash='x')
    m = Manuscript.objects.create(author_account=author, title='T', author_name='M', author_email='m@example.com')
    v1 = Version.objects.create(manuscript=m, number=1)
    v2 = Version.objects.create(manuscript=m, number=2)
    org = Org.objects.create(name='O')
    venues = [Venue.objects.create(organization=org, name=f'V{i}', slug=f'v{i}-{uuid.uuid4().hex[:6]}')
              for i in range(3)]
    paused = Plan.objects.create(manuscript=m, built_on_version=v1, status='active')
    Position.objects.create(plan=paused, order=1, venue=venues[0], state='awaiting_author', reason='First: x.',
                            effort_class='ready', unchanged_reason='', changes=[])
    Position.objects.create(plan=paused, order=2, venue=venues[1], state='queued', reason='Second: y.',
                            effort_class='edit', changes=[{'class': 'edit'}, {'class': 'section'}])
    stopped = Plan.objects.create(manuscript=m, built_on_version=v1, status='stopped')
    Position.objects.create(plan=stopped, order=1, venue=venues[2], state='closed', reason='First: z.',
                            effort_class='ready', unchanged_reason='Scope only.', changes=[])

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    Plan = new.get_model('review', 'SubmissionPlan')
    Step = new.get_model('review', 'SubmissionPlanStep')
    Version = new.get_model('review', 'ManuscriptVersion')

    assert Plan.objects.get(id=paused.id).status == 'paused'
    assert Plan.objects.get(id=stopped.id).status == 'abandoned'
    assert Plan.objects.get(id=paused.id).manuscript_version_id == v1.id
    declined = Step.objects.get(plan_id=paused.id, order=1)
    assert declined.state == 'declined' and declined.rationale == 'First: x.'
    queued = Step.objects.get(plan_id=paused.id, order=2)
    assert queued.compliance_distance == 4 and queued.band == 3 and queued.required_changes
    assert Step.objects.get(plan_id=stopped.id).outcome_note == 'Scope only.'
    assert Version.objects.get(id=v2.id).supersedes_id == v1.id

    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
