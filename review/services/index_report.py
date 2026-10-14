"""The three numbers the Venue Index is judged by (instructions of 8 October, section 4).

1. Escalation rate: of the rule reads, how many the first local attempt could not settle, overall and
   per field, against the 20 percent threshold above which a field should move to the cloud.
2. Screening overturned: of the records a person decided, how many decisions went against what the
   automated screening showed (flagged but kept, or clear but excluded).
3. Validation failures: of all rule-reading attempts (local, retry, cloud), how many the page
   validator rejected, and how many reads ended with no adequate result at all.

Read-only: it counts what is stored and changes nothing.
"""
from datetime import timedelta

from django.utils import timezone

from .rules_escalation import MOVE_TO_CLOUD_RATE, escalation_stats


def _share(part, whole):
    return round(part / whole, 3) if whole else 0.0


def _negative_flags(snapshot):
    return [f for f in (snapshot or []) if isinstance(f, dict) and f.get('kind', 'negative') == 'negative']


def screening_overturned(days=30, now=None):
    """First keep/exclude decision per record in the window, compared with the screening it saw."""
    from ..models import IndexReviewDecision
    now = now or timezone.now()
    since = now - timedelta(days=days)
    first = {}
    for d in (IndexReviewDecision.objects.filter(decided_at__gte=since, decision__in=('keep', 'exclude'))
              .order_by('decided_at').values('record_id', 'decision', 'flags_snapshot')):
        first.setdefault(d['record_id'], d)
    flagged_kept = clear_excluded = 0
    for d in first.values():
        flagged = bool(_negative_flags(d['flags_snapshot']))
        if flagged and d['decision'] == 'keep':
            flagged_kept += 1
        elif not flagged and d['decision'] == 'exclude':
            clear_excluded += 1
    decided = len(first)
    return {'decided': decided, 'flagged_but_kept': flagged_kept, 'clear_but_excluded': clear_excluded,
            'overturned': flagged_kept + clear_excluded, 'share': _share(flagged_kept + clear_excluded, decided)}


def validation_failures(days=30, now=None):
    from ..models import RulesAttempt
    now = now or timezone.now()
    since = now - timedelta(days=days)
    rows = list(RulesAttempt.objects.filter(created_at__gte=since, job='rule_extraction')
                .values('chain', 'stage', 'outcome'))
    by_stage = {}
    for row in rows:
        stage = by_stage.setdefault(row['stage'], {'attempts': 0, 'inadequate': 0, 'error': 0})
        stage['attempts'] += 1
        if row['outcome'] in ('inadequate', 'error'):
            stage[row['outcome']] += 1
    inadequate = sum(s['inadequate'] for s in by_stage.values())
    errors = sum(s['error'] for s in by_stage.values())
    chains = {}
    for row in rows:
        chains.setdefault(row['chain'], set()).add(row['outcome'])
    unresolved = sum(1 for outcomes in chains.values() if 'adequate' not in outcomes)
    for stage in by_stage.values():
        stage['failed_validation_share'] = _share(stage['inadequate'], stage['attempts'])
    return {'attempts': len(rows), 'failed_validation': inadequate, 'errors': errors,
            'failed_validation_share': _share(inadequate, len(rows)), 'by_stage': by_stage,
            'reads': len(chains), 'reads_unresolved': unresolved,
            'reads_unresolved_share': _share(unresolved, len(chains))}


def index_report(days=30, now=None):
    escalation = escalation_stats(days=days, now=now)
    return {
        'days': days,
        'escalation': {
            'reads': escalation['reads'],
            'rate': escalation['escalation_rate'],
            'threshold': MOVE_TO_CLOUD_RATE,
            'over_threshold': escalation['escalation_rate'] > MOVE_TO_CLOUD_RATE,
            'resolved_at': escalation['resolved'],
            'fields': escalation['fields'],
            'cloud_enabled': escalation['cloud_enabled'],
            'cloud_calls': escalation['cloud_calls'],
            'cloud_cost_usd': escalation['cloud_cost_usd'],
        },
        'screening': screening_overturned(days=days, now=now),
        'validation': validation_failures(days=days, now=now),
    }
