import json

from django.core.management.base import BaseCommand

from review.services.index_report import index_report


def pct(value):
    return f'{value * 100:.1f}%'


class Command(BaseCommand):
    help = ('The three Venue Index numbers for the report to Vikram: escalation rate (vs 20%), share of '
            'screening overturned by a person, and share of rule reads failing validation. Read-only.')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Look back this many days (default 30).')
        parser.add_argument('--json', action='store_true', help='Print the full report as JSON.')

    def handle(self, *args, **options):
        days = max(1, options['days'])
        report = index_report(days=days)
        if options['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        e, s, v = report['escalation'], report['screening'], report['validation']
        w = self.stdout.write
        w(f'Venue Index report, last {days} days')
        w('')
        verdict = 'ABOVE the threshold' if e['over_threshold'] else 'under the threshold'
        w(f"1. Escalation rate: {pct(e['rate'])} of {e['reads']} rule reads ({verdict} of {pct(e['threshold'])})")
        r = e['resolved_at']
        w(f"   Settled by: local {r['local']} · local retry {r['local_retry']} · cloud {r['cloud']} · "
          f"not settled {r['unresolved']}")
        for f in e['fields']:
            mark = '  <- move to cloud' if f['move_to_cloud'] else ''
            w(f"   {f['label']}: missing on first try {f['count']} ({pct(f['rate'])}){mark}")
        w(f"   Cloud step: {'on' if e['cloud_enabled'] else 'off'} · {e['cloud_calls']} calls · "
          f"${e['cloud_cost_usd']:.2f}")
        w('')
        w(f"2. Screening overturned by a person: {pct(s['share'])} of {s['decided']} records decided")
        w(f"   Flagged but kept: {s['flagged_but_kept']} · Clear but excluded: {s['clear_but_excluded']}")
        w('')
        w(f"3. Rule-read attempts failing validation: {pct(v['failed_validation_share'])} "
          f"({v['failed_validation']} of {v['attempts']} attempts; {v['errors']} more were errors)")
        for stage, row in sorted(v['by_stage'].items()):
            w(f"   {stage}: {row['inadequate']} of {row['attempts']} ({pct(row['failed_validation_share'])})")
        w(f"   Reads with no adequate result: {v['reads_unresolved']} of {v['reads']} "
          f"({pct(v['reads_unresolved_share'])})")
        if not e['reads'] and not s['decided']:
            w('')
            w('No rule reads or screening decisions in this window yet. Run '
              '`python manage.py import_venue_index --rules-only` and decide some flagged records first.')
