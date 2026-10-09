"""The ordered submission plan (8 October instructions, 2.5; draft spec sections 4.2 to 6).

Policy carried into code:
  * the order rests on fit and compliance distance only, never on ratings, journal metrics or
    predicted acceptance; reasons are generated from the ordering key, not by an AI model;
  * one live position at a time (simultaneous submission is not allowed);
  * a decline never advances the plan: it stops in 'awaiting_author' until the author answers;
  * every position carries a stated reason.
"""
import os

from django.db import transaction
from django.utils import timezone

from .gap_classes import CLASS_LABEL, CLASS_RANK, effort_of, items_for
from .models import ManuscriptVersion, PlanEvent, PlanPosition, SubmissionPlan, Venue

METHOD_VERSION = 'plan-v1'
LIVE_STATES = {'preparing', 'submitted', 'under_review', 'revise_resubmit', 'awaiting_author'}
DONE_STATES = {'accepted', 'closed', 'skipped', 'withdrawn'}
ORDINALS = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth', 'Seventh', 'Eighth', 'Ninth', 'Tenth']


class PlanError(Exception):
    """A request the plan cannot accept; the message is shown to the author."""

    def __init__(self, message, code='plan_error', status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def settings():
    try:
        positions = max(1, min(int(os.getenv('PLAN_MAX_POSITIONS', '5')), 10))
    except ValueError:
        positions = 5
    return {'max_positions': positions,
            'min_fit': _env_float('PLAN_MIN_FIT', '0.40'),       # cosine of local embeddings
            'strong_fit': _env_float('PLAN_STRONG_FIT', '0.60')}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def assess(match, config):
    """Effort and fit for one match, or (None, reason) when the venue cannot be in the plan."""
    items = items_for(match)
    cls, count, quantity = effort_of(items)
    venue = match.venue
    if cls == 'out':
        reason = next(i['message'] for i in items if i['class'] == 'out')
        return None, reason
    if match.eligibility == 'ineligible' and not items:
        return None, 'This venue\'s rules exclude this manuscript.'
    sim = match.topic_similarity
    if sim is not None and sim < config['min_fit']:
        return None, 'Its scope is too far from this manuscript\'s topic.'
    band = 'strong' if (sim is not None and sim >= config['strong_fit']) else 'moderate'
    return {
        'match': match, 'venue': venue, 'items': items, 'class': cls, 'count': count, 'quantity': quantity,
        'band': band, 'similarity': sim,
    }, None


def order_key(entry):
    return (CLASS_RANK[entry['class']], 0 if entry['band'] == 'strong' else 1, entry['count'], entry['quantity'],
            -(entry['similarity'] if entry['similarity'] is not None else -1.0),
            entry['match'].shortlist_rank if entry['match'].shortlist_rank is not None else 10 ** 6,
            entry['venue'].name.lower())


def _changes_phrase(entry):
    hardest = [i for i in entry['items'] if i['class'] == entry['class']]
    if entry['class'] == 'ready':
        return 'no changes needed for this venue'
    kind = {'edit': 'edit', 'section': 'new section', 'study': 'new study'}[entry['class']]
    what = '; '.join(i['message'].rstrip('.') for i in hardest[:2])
    what = what[:1].lower() + what[1:]
    many = f'{len(hardest)} {kind}s' if len(hardest) > 1 else f'one {kind}'
    return f'needs {many}: {what}'


def reason_for(position_number, entry, previous):
    """Plain words from the ordering key. No odds, no ratings."""
    ordinal = ORDINALS[position_number - 1] if position_number <= len(ORDINALS) else f'Position {position_number}'
    scope = 'its scope is a strong match' if entry['band'] == 'strong' else 'its scope is a reasonable match'
    text = f'{ordinal}: {_changes_phrase(entry)}, and {scope}.'
    if previous is not None:
        if CLASS_RANK[entry['class']] > CLASS_RANK[previous['class']]:
            text += (f' Placed after venues needing only {CLASS_LABEL[previous["class"]].lower()}'
                     if previous['class'] != 'ready' else ' Placed after venues that need no changes') + '.'
        elif entry['band'] != previous['band']:
            text += ' Same amount of change as the one before, but a less close scope.'
        else:
            text += ' Same amount of change as the one before; its scope is slightly less close.'
    return text


def rank(manuscript, version=None):
    """(entries in plan order, not_included) for the version's current matches."""
    from .author_api import _active_config  # local import: avoids a cycle
    config = settings()
    version_id = (version or manuscript.current_version).id if (version or manuscript.current_version) else None
    matches = (manuscript.venue_matches.filter(version_id=version_id, venue__excluded=False)
               .select_related('venue', 'venue__organization'))
    matchable = set(Venue.objects.matchable().values_list('id', flat=True))
    entries, not_included = [], []
    for match in matches:
        if match.venue_id not in matchable:
            continue
        entry, why = assess(match, config)
        if entry is None:
            not_included.append({'venue_id': str(match.venue_id), 'venue': match.venue.name, 'reason': why})
            continue
        entry['config'] = _active_config(match.venue)
        entries.append(entry)
    entries.sort(key=order_key)
    for extra in entries[config['max_positions']:]:
        not_included.append({'venue_id': str(extra['venue'].id), 'venue': extra['venue'].name,
                             'reason': f'The plan holds {config["max_positions"]} venues; this one came after them.'})
    not_included.sort(key=lambda item: item['venue'].lower())
    return entries[:config['max_positions']], not_included


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _log(plan, action, *, position=None, actor='author', from_state='', to_state='', note=''):
    PlanEvent.objects.create(plan=plan, position=position, actor=actor, action=action, from_state=from_state,
                             to_state=to_state, note=(note or '')[:2000])


def ready_to_plan(manuscript):
    if not manuscript.current_version_id:
        raise PlanError('This manuscript has no version yet.')
    readiness = manuscript.current_readiness().filter(status='completed').first()
    if not readiness or not (readiness.summary or {}).get('ready_for_matching'):
        raise PlanError('Run the readiness check for the current version first.', 'readiness_needed')
    if not manuscript.current_matches().exists():
        raise PlanError('Find matching venues for the current version first.', 'matches_needed')


@transaction.atomic
def build_plan(manuscript, *, replace=False):
    ready_to_plan(manuscript)
    active = SubmissionPlan.objects.select_for_update().filter(manuscript=manuscript, status='active').first()
    if active:
        if not replace:
            raise PlanError('This manuscript already has an active plan.', 'plan_exists')
        if active.positions.filter(state__in=LIVE_STATES - {'preparing'}).exists():
            raise PlanError('A venue in the current plan is still in progress; finish or withdraw it first.',
                            'plan_live')
        active.status = 'stopped'
        active.save(update_fields=['status', 'updated_at'])
        _log(active, 'replaced', note='A new plan was built.')
    entries, not_included = rank(manuscript)
    if not entries:
        raise PlanError('No venue can be planned yet: ' + (not_included[0]['reason'] if not_included
                        else 'no matching venues.'), 'nothing_to_plan')
    plan = SubmissionPlan.objects.create(manuscript=manuscript, built_on_version=manuscript.current_version,
                                         method_version=METHOD_VERSION, not_included=not_included)
    previous = None
    for number, entry in enumerate(entries, start=1):
        PlanPosition.objects.create(
            plan=plan, order=number, venue=entry['venue'], venue_config=entry['config'], match=entry['match'],
            reason=reason_for(number, entry, previous), effort_class=entry['class'], changes=entry['items'],
            fit_band=entry['band'])
        previous = entry
    _log(plan, 'built', note=f'{len(entries)} venues for version {manuscript.current_version.number}.')
    return plan


def active_plan(manuscript):
    return (SubmissionPlan.objects.filter(manuscript=manuscript).order_by('-created_at')
            .prefetch_related('positions__venue', 'events').first())


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _lock(plan_id, position_id=None):
    plan = SubmissionPlan.objects.select_for_update().get(id=plan_id)
    position = plan.positions.select_for_update().get(id=position_id) if position_id else None
    return plan, position


def _require(position, *states):
    if position.state not in states:
        raise PlanError(f'This step is not possible while the venue is "{position.get_state_display()}".',
                        'bad_state')


def _require_active(plan):
    if plan.status != 'active':
        raise PlanError('This plan is no longer active.', 'plan_closed')


def _move(position, to_state, action, *, actor='author', note='', fields=()):
    from_state = position.state
    position.state = to_state
    position.save(update_fields=['state', 'updated_at', *fields])
    _log(position.plan, action, position=position, actor=actor, from_state=from_state, to_state=to_state, note=note)


@transaction.atomic
def start(plan_id, position_id):
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'queued')
    if plan.positions.filter(state__in=LIVE_STATES).exists():
        raise PlanError('Another venue in this plan is still in progress. One venue at a time.', 'one_at_a_time')
    first_queued = plan.positions.filter(state='queued').order_by('order').first()
    if first_queued.id != position.id:
        raise PlanError(f'Start with "{first_queued.venue.name}" first, or skip it with a reason.', 'out_of_order')
    _move(position, 'preparing', 'started')
    return position


@transaction.atomic
def mark_submitted(plan_id, position_id, *, venue_submission=None, actor='author'):
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'preparing')
    manuscript = plan.manuscript
    position.submitted_version_id = (venue_submission.version_id if venue_submission and venue_submission.version_id
                                     else manuscript.current_version_id)
    position.venue_submission = venue_submission
    _move(position, 'submitted', 'submitted', actor=actor, fields=('submitted_version', 'venue_submission'))
    return position


@transaction.atomic
def mark_under_review(plan_id, position_id, *, actor='author'):
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'submitted')
    _move(position, 'under_review', 'under_review', actor=actor)
    return position


@transaction.atomic
def report_outcome(plan_id, position_id, outcome, *, actor='author', note=''):
    """accepted | revise_resubmit | declined | withdrawn. A decline stops the plan for the author's answer."""
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    if outcome not in {'accepted', 'revise_resubmit', 'declined', 'withdrawn'}:
        raise PlanError('outcome must be accepted, revise_resubmit, declined or withdrawn.', 'bad_outcome', 400)
    _require(position, 'submitted', 'under_review', 'revise_resubmit')
    position.outcome, position.outcome_reported_by, position.outcome_at = outcome, actor, timezone.now()
    fields = ('outcome', 'outcome_reported_by', 'outcome_at')
    if outcome == 'accepted':
        _move(position, 'accepted', 'accepted', actor=actor, note=note, fields=fields)
        plan.status = 'completed'
        plan.save(update_fields=['status', 'updated_at'])
        _log(plan, 'completed', actor=actor, note=f'Accepted by {position.venue.name}.')
    elif outcome == 'revise_resubmit':
        _move(position, 'revise_resubmit', 'revise_resubmit', actor=actor, note=note, fields=fields)
    elif outcome == 'declined':
        # Never advance: the next venue waits until the author says whether they revised.
        _move(position, 'awaiting_author', 'declined', actor=actor, note=note, fields=fields)
    else:
        _move(position, 'withdrawn', 'withdrawn', actor=actor, note=note, fields=fields)
    return position


def _newer_version(plan, position, version_number):
    version = ManuscriptVersion.objects.filter(manuscript=plan.manuscript, number=version_number).first()
    if version is None:
        raise PlanError('That version does not exist.', 'bad_version', 400)
    baseline = position.submitted_version.number if position.submitted_version_id else plan.built_on_version.number
    if version.number <= baseline:
        raise PlanError(f'Upload a revised version first: the venue saw version {baseline}.', 'needs_revision')
    return version


@transaction.atomic
def resubmit(plan_id, position_id, *, version_number):
    """After 'revise and resubmit': the same venue gets a newer version."""
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'revise_resubmit')
    version = _newer_version(plan, position, version_number)
    position.submitted_version = version
    _move(position, 'submitted', 'resubmitted', note=f'Version {version.number}.', fields=('submitted_version',))
    return position


@transaction.atomic
def answer_decline(plan_id, position_id, *, answer, version_number=None, reason='', confirm=False):
    """The author's answer after a decline: revised (with a newer version) or unchanged (with a reason)."""
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'awaiting_author')
    if answer == 'revised':
        if version_number is None:
            raise PlanError('Say which revised version the next venue should get.', 'needs_revision', 400)
        version = _newer_version(plan, position, version_number)
        recheck = _recheck_remaining(plan, version)
        position.revision_answer, position.revision_version = 'revised', version
        _move(position, 'closed', 'answered_revised', note=f'Revised as version {version.number}. {recheck}',
              fields=('revision_answer', 'revision_version'))
    elif answer == 'unchanged':
        if len(reason.strip()) < 20:
            raise PlanError('Explain why the next venue should get the same text (at least 20 characters).',
                            'needs_reason', 400)
        if not confirm:
            raise PlanError('Confirm that the next venue gets the manuscript the previous venue declined, unchanged.',
                            'needs_confirmation', 400)
        position.revision_answer, position.unchanged_reason = 'unchanged', reason.strip()[:2000]
        _move(position, 'closed', 'answered_unchanged', note=reason, fields=('revision_answer', 'unchanged_reason'))
    else:
        raise PlanError('answer must be revised or unchanged.', 'bad_answer', 400)
    return position


def _recheck_remaining(plan, version):
    """Re-measure the queued venues against the revised version and re-order them, with reasons."""
    queued = list(plan.positions.filter(state='queued').order_by('order'))
    if not queued:
        return 'No venues left in the plan.'
    if not plan.manuscript.venue_matches.filter(version=version).exists():
        raise PlanError(f'Run the readiness check and venue matching for version {version.number} first, so the '
                        'remaining venues can be re-checked against it.', 'matches_needed')
    entries, _ = rank(plan.manuscript, version)
    by_venue = {entry['venue'].id: entry for entry in entries}
    kept, dropped = [], []
    for position in queued:
        entry = by_venue.get(position.venue_id)
        (kept if entry else dropped).append((position, entry))
    for position, _entry in dropped:
        position.skip_reason = f'After revision (version {version.number}) this venue no longer fits the plan.'
        _move(position, 'skipped', 'dropped_after_revision', actor='system', note=position.skip_reason,
              fields=('skip_reason',))
    kept.sort(key=lambda pair: order_key(pair[1]))
    slots = sorted(p.order for p, _ in kept)
    # Two passes so the unique (plan, order) constraint never sees a duplicate.
    for position, _ in kept:
        PlanPosition.objects.filter(id=position.id).update(order=position.order + 1000)
    previous = None
    for (position, entry), slot in zip(kept, slots):
        position.order = slot
        position.match = entry['match']
        position.changes, position.effort_class, position.fit_band = entry['items'], entry['class'], entry['band']
        position.reason = reason_for(slot, entry, previous) + f' (re-checked against version {version.number})'
        position.save(update_fields=['order', 'match', 'changes', 'effort_class', 'fit_band', 'reason', 'updated_at'])
        previous = entry
    _log(plan, 'rechecked', actor='system', note=f'{len(kept)} venues re-checked against version {version.number}.')
    return f'{len(kept)} remaining venues re-checked against it.'


@transaction.atomic
def skip(plan_id, position_id, *, reason):
    plan, position = _lock(plan_id, position_id)
    _require_active(plan)
    _require(position, 'queued')
    if len(reason.strip()) < 5:
        raise PlanError('Give a short reason for skipping this venue.', 'needs_reason', 400)
    position.skip_reason = reason.strip()[:2000]
    _move(position, 'skipped', 'skipped', note=reason, fields=('skip_reason',))
    return position


@transaction.atomic
def stop(plan_id, *, note=''):
    plan, _ = _lock(plan_id)
    _require_active(plan)
    plan.status = 'stopped'
    plan.save(update_fields=['status', 'updated_at'])
    _log(plan, 'stopped', note=note)
    return plan


def sync_from_submission(submission):
    """Keep a plan in step with a Flexee venue's own workflow (submitted, review started, decision).
    Never raises: plan problems must not break the editorial flow."""
    try:
        position = (PlanPosition.objects.filter(plan__status='active', plan__manuscript_id=submission.manuscript_id,
                                                venue_id=submission.venue_id)
                    .exclude(state__in=DONE_STATES | {'queued'}).select_related('plan').first())
        if position is None:
            return None
        status = submission.status
        if status == 'submitted' and position.state == 'preparing':
            return mark_submitted(position.plan_id, position.id, venue_submission=submission, actor='venue')
        if status == 'under_review' and position.state == 'submitted':
            return mark_under_review(position.plan_id, position.id, actor='venue')
        outcome = {'accepted': 'accepted', 'rejected': 'declined', 'revision_requested': 'revise_resubmit',
                   'withdrawn': 'withdrawn'}.get(status)
        if outcome and position.state in {'submitted', 'under_review', 'revise_resubmit'}:
            return report_outcome(position.plan_id, position.id, outcome, actor='venue')
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception('Plan sync failed for submission %s', submission.id)
    return None


# ---------------------------------------------------------------------------
# Payloads (no probability, score or colour code, by policy)
# ---------------------------------------------------------------------------

def position_payload(position):
    from .author_api import _venue_trust_payload
    venue = position.venue
    return {
        'id': str(position.id),
        'order': position.order,
        'state': position.state,
        'state_label': position.get_state_display(),
        'venue': {'id': str(venue.id), 'name': venue.name, 'slug': venue.slug, 'trust': _venue_trust_payload(venue)},
        'reason': position.reason,
        'effort': CLASS_LABEL.get(position.effort_class, position.effort_class),
        'effort_class': position.effort_class,
        'changes': [{'message': c.get('message'), 'kind': CLASS_LABEL.get(c.get('class'), c.get('class')),
                     'amount': c.get('quantity'), 'unit': c.get('unit'), 'source': c.get('source_locator')}
                    for c in position.changes or []],
        'scope': 'Strong scope match' if position.fit_band == 'strong' else 'Reasonable scope match',
        'venue_submission_id': str(position.venue_submission_id) if position.venue_submission_id else None,
        'submitted_version': position.submitted_version.number if position.submitted_version_id else None,
        'outcome': position.outcome or None,
        'outcome_reported_by': position.outcome_reported_by or None,
        'revision_answer': position.revision_answer or None,
        'revision_version': position.revision_version.number if position.revision_version_id else None,
        'unchanged_reason': position.unchanged_reason or None,
        'skip_reason': position.skip_reason or None,
    }


def plan_payload(plan):
    positions = list(plan.positions.select_related('venue', 'venue__organization', 'submitted_version',
                                                    'revision_version').order_by('order'))
    current = next((p for p in positions if p.state in LIVE_STATES), None) or \
        next((p for p in positions if p.state == 'queued'), None)
    return {
        'id': str(plan.id),
        'status': plan.status,
        'built_on_version': plan.built_on_version.number,
        'method_version': plan.method_version,
        'created_at': plan.created_at.isoformat(),
        'current_position_id': str(current.id) if current and plan.status == 'active' else None,
        'waiting_for_author': bool(current and current.state == 'awaiting_author'),
        'positions': [position_payload(p) for p in positions],
        'not_included': plan.not_included,
        'events': [{'action': e.action, 'actor': e.actor, 'from': e.from_state, 'to': e.to_state, 'note': e.note,
                    'at': e.created_at.isoformat()} for e in plan.events.order_by('created_at')],
        'how_ordered': 'Venues are ordered by how much the manuscript must change for each one (no changes, '
                       'then edits, then a new section, then a new study), and then by how close their scope is. '
                       'Nothing else decides the order.',
    }
