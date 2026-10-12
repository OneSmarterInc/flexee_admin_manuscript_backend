"""Align the submission plan with the 8 October specification (sections 4.1 to 6).

PlanPosition becomes SubmissionPlanStep with the spec's field names and states; plans gain a
draft/active/paused/completed/abandoned status, a current position and generated_by; versions
gain `supersedes`. Existing rows are mapped, nothing is dropped that a user could see.
"""
import django.db.models.deletion
from django.db import migrations, models

STEP_STATE_MAP = {
    'preparing': 'current',
    'under_review': 'submitted',
    'revise_resubmit': 'submitted',
    'awaiting_author': 'declined',
    'closed': 'declined',
}
CLASS_DISTANCE = {'edit': 1, 'section': 3, 'study': 9}


def forwards(apps, schema_editor):
    Plan = apps.get_model('review', 'SubmissionPlan')
    Step = apps.get_model('review', 'SubmissionPlanStep')
    Version = apps.get_model('review', 'ManuscriptVersion')
    for version in Version.objects.filter(number__gt=1).only('id', 'manuscript_id', 'number'):
        previous = Version.objects.filter(manuscript_id=version.manuscript_id, number=version.number - 1).first()
        if previous:
            Version.objects.filter(id=version.id).update(supersedes=previous)
    for step in Step.objects.all():
        fields = {}
        if step.state in STEP_STATE_MAP:
            fields['state'] = STEP_STATE_MAP[step.state]
        notes = [n for n in (step.outcome_note, step.unchanged_reason) if n]
        if notes:
            fields['outcome_note'] = '\n'.join(notes)
        changes = step.required_changes or []
        fields['compliance_distance'] = sum(CLASS_DISTANCE.get((c or {}).get('class'), 3) for c in changes
                                            if isinstance(c, dict))
        fields['band'] = 1 if not changes else 3
        Step.objects.filter(id=step.id).update(**fields)
    for plan in Plan.objects.all():
        status = plan.status
        steps = Step.objects.filter(plan_id=plan.id)
        if status == 'stopped':
            status = 'abandoned'
        elif status == 'active' and steps.filter(state='declined', revision_answer='').exists():
            status = 'paused'
        current = steps.filter(state__in=['current', 'submitted']).order_by('order').first()
        if status == 'active' and current is None:
            first = steps.filter(state='queued').order_by('order').first()
            if first:
                Step.objects.filter(id=first.id).update(state='current')
                current = first
        Plan.objects.filter(id=plan.id).update(status=status, position=current.order if current else None,
                                               generated_by=plan.method_version)


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0039_submission_plan'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='submissionplan', name='review_one_active_plan_per_manuscript'),
        migrations.RemoveConstraint(model_name='planposition', name='review_unique_plan_position_order'),
        migrations.RenameModel(old_name='PlanPosition', new_name='SubmissionPlanStep'),
        migrations.RenameField(model_name='submissionplan', old_name='built_on_version', new_name='manuscript_version'),
        migrations.RenameField(model_name='submissionplanstep', old_name='match', new_name='venue_match'),
        migrations.RenameField(model_name='submissionplanstep', old_name='reason', new_name='rationale'),
        migrations.RenameField(model_name='submissionplanstep', old_name='changes', new_name='required_changes'),
        migrations.RenameField(model_name='planevent', old_name='position', new_name='step'),
        migrations.AddField(model_name='submissionplan', name='position',
                            field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='submissionplan', name='generated_by',
                            field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='submissionplan', name='previous_plan',
                            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name='next_plans', to='review.submissionplan')),
        migrations.AddField(model_name='submissionplanstep', name='band',
                            field=models.PositiveSmallIntegerField(
                                choices=[(1, 'Eligible, no changes'), (2, 'Eligible, with changes'),
                                         (3, 'Needs changes')], default=3)),
        migrations.AddField(model_name='submissionplanstep', name='compliance_distance',
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='submissionplanstep', name='author_moved',
                            field=models.BooleanField(default=False)),
        migrations.AddField(model_name='submissionplanstep', name='outcome_note',
                            field=models.TextField(blank=True)),
        migrations.AddField(model_name='planevent', name='venue_name',
                            field=models.CharField(blank=True, max_length=300)),
        migrations.AddField(model_name='manuscriptversion', name='supersedes',
                            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name='superseded_by', to='review.manuscriptversion')),
        migrations.AlterField(model_name='planevent', name='step',
                              field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                      related_name='events', to='review.submissionplanstep')),
        migrations.AlterField(model_name='submissionplan', name='method_version',
                              field=models.CharField(default='plan-v2', max_length=40)),
        migrations.AlterField(model_name='submissionplan', name='status',
                              field=models.CharField(choices=[('draft', 'Draft'), ('active', 'Active'),
                                                              ('paused', 'Paused: waiting for the author'),
                                                              ('completed', 'Completed'), ('abandoned', 'Abandoned')],
                                                     db_index=True, default='draft', max_length=20)),
        migrations.AlterField(model_name='submissionplanstep', name='plan',
                              field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps',
                                                      to='review.submissionplan')),
        migrations.AlterField(model_name='submissionplanstep', name='state',
                              field=models.CharField(choices=[('queued', 'Queued'), ('current', 'Current'),
                                                              ('submitted', 'Submitted'), ('accepted', 'Accepted'),
                                                              ('declined', 'Declined'), ('skipped', 'Skipped'),
                                                              ('withdrawn', 'Withdrawn')],
                                                     db_index=True, default='queued', max_length=20)),
        migrations.AlterField(model_name='submissionplanstep', name='venue',
                              field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='plan_steps',
                                                      to='review.venue')),
        migrations.AlterField(model_name='submissionplanstep', name='venue_config',
                              field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                      related_name='plan_steps', to='review.venueagentconfig')),
        migrations.AlterField(model_name='submissionplanstep', name='venue_match',
                              field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                      related_name='plan_steps', to='review.venuematch')),
        migrations.AlterField(model_name='submissionplanstep', name='venue_submission',
                              field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                      related_name='plan_steps', to='review.venuesubmission')),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='submissionplanstep', name='outcome'),
        migrations.RemoveField(model_name='submissionplanstep', name='unchanged_reason'),
        migrations.AddConstraint(
            model_name='submissionplan',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['draft', 'active', 'paused'])),
                                               fields=('manuscript',), name='review_one_live_plan_per_manuscript')),
        migrations.AddConstraint(
            model_name='submissionplanstep',
            constraint=models.UniqueConstraint(fields=('plan', 'order'), name='review_unique_plan_step_order')),
    ]
