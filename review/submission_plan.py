"""The ordered submission plan (submission plan spec, 8 October, sections 4.2 to 8).

Policy carried into code:
  * the order rests on fit and compliance distance only, never on ratings, journal metrics or
    predicted acceptance; rationales are generated from the ordering key, not by an AI model;
  * three bands, never mixed: eligible with no changes, eligible with changes, needs changes;
    inside a band, the spec key: effort class (edits before a new section before a new study),
    then fit band (strong before moderate), then the amount of change, then topic similarity,
    then the venue name for exact ties; ineligible venues are listed separately;
  * one venue at a time (simultaneous submission is not allowed);
  * a decline never advances the plan: it pauses and asks whether the author revised;
  * a venue that declined is never proposed again for the same manuscript;
  * an author's reorder, skip or removal is respected when the plan is regenerated;
  * every step carries a stated rationale.

States (spec 6):
  plan  draft -> active -> (paused <-> active) -> completed | abandoned
  step  queued -> current -> submitted -> accepted | declined | withdrawn;  queued/current -> skipped
"""
import os

from django.db import transaction
from django.utils import timezone

from .gap_classes import (CLASS_KIND, CLASS_LABEL, CLASS_RANK, compliance_distance, effort_of, items_for, plain_item,
                          plain_words, preparation_items)
from .models import ManuscriptVersion, PlanEvent, SubmissionPlan, SubmissionPlanStep, Venue

METHOD_VERSION = 'plan-v2'
LIVE_STATUSES = set(SubmissionPlan.LIVE_STATUSES)
MOVABLE_STATES = {'queued', 'current'}
DONE_STATES = {'accepted', 'declined', 'skipped', 'withdrawn'}
BAND_TEXT = {1: 'Eligible, no changes', 2: 'Eligible, with changes', 3: 'Needs changes'}
KIND_LABEL = {'light': 'Light change', 'medium': 'Medium change', 'heavy': 'Heavy change', 'prepare': 'To prepare'}


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
    raw = os.getenv('PLAN_MAX_STEPS') or os.getenv('PLAN_MAX_POSITIONS') or '8'
    try:
        steps = max(1, min(int(raw), 20))
    except ValueError:
        steps = 8
    return {'max_steps': steps,
            'min_fit': _env_float('PLAN_MIN_FIT', '0.40'),       # cosine of local embeddings
            'strong_fit': _env_float('PLAN_STRONG_FIT', '0.60'),
            # Audit item 22: with no similarity score (embeddings off), keyword overlap is the floor.
            'min_keyword_fit': _env_float('PLAN_MIN_KEYWORD_FIT', '0.10')}


# ---------------------------------------------------------------------------
# Ordering (spec 5)
# ---------------------------------------------------------------------------

def assess(match, config):
    """Band, distance and fit for one match, or (None, reason) when the venue is not a fit.

    The match marks a breached desk rule 'ineligible' for the text as it stands. For the plan,
    a breach the author can fix by revising (a word cut, a missing section) is 'needs changes';
    only a gap revising cannot fix (article type not accepted, rules not configured) excludes it.
    """
    items = [plain_item(i) for i in items_for(match)]  # venue text never brings odds into the plan
    out = [i for i in items if i['class'] == 'out']
    if out:
        return None, out[0]['message']
    if match.eligibility == 'ineligible' and not items:
        return None, 'This venue\'s rules exclude this manuscript.'
    sim = match.topic_similarity
    if sim is not None and sim < config['min_fit']:
        return None, 'Its scope is too far from this manuscript\'s topic.'
    if sim is None and _keyword_fit(match) < config.get('min_keyword_fit', 0.10):
        # Audit item 22: no similarity score (embeddings off) is not a free pass for an off-topic venue.
        return None, 'Its scope shares too few topics with this manuscript.'
    if not items and match.eligibility == 'eligible':
        band = 1
    elif match.eligibility == 'eligible':
        band = 2
    else:
        band = 3
    cls, count, quantity = effort_of(items)
    return {
        'match': match, 'venue': match.venue, 'items': items, 'band': band, 'class': cls,
        'count': count, 'quantity': quantity,
        'distance': compliance_distance(items), 'similarity': sim,
        'fit_band': 'strong' if (sim is not None and sim >= config['strong_fit']) else 'moderate',
    }, None


def _keyword_fit(match):
    """Share of the manuscript's topic terms the venue uses (the matcher's keyword fallback)."""
    try:
        from .services.venue_shortlist import _keyword_similarity, _manuscript_terms
        return _keyword_similarity(_manuscript_terms(match.manuscript), match.venue, match.venue_config)
    except Exception:  # noqa: BLE001  (never drop a venue because the fallback itself failed)
        return 1.0


FIT_BAND_RANK = {'strong': 0, 'moderate': 1}
EFFORT_PHRASE = {'ready': 'no changes', 'edit': 'edits', 'section': 'a new section', 'study': 'a new study'}


def order_key(entry):
    """Band, then the spec key inside the band. Nothing else: no ratings, metrics or tiers.

    (effort class, fit band, how many changes of that class, their total amount, -similarity, name)
    """
    return (entry['band'], CLASS_RANK.get(entry['class'], CLASS_RANK['section']),
            FIT_BAND_RANK.get(entry['fit_band'], 1), entry.get('count', 0), entry.get('quantity', 0),
            -(entry['similarity'] if entry['similarity'] is not None else -1.0),
            entry['venue'].name.lower())


def _changes_phrase(entry):
    items = [i for i in entry['items'] if i['class'] in CLASS_KIND or i['class'] == 'section']
    if entry['band'] == 1 or not items:
        return 'no changes needed for this venue'
    weights = [CLASS_KIND.get(i['class'], 'medium') for i in items]
    heaviest = 'heavy' if 'heavy' in weights else 'medium' if 'medium' in weights else 'light'
    what = '; '.join(i['message'].rstrip('.') for i in items[:2])
    what = what[:1].lower() + what[1:]
    count = f'{len(items)} changes' if len(items) > 1 else 'one change'
    lead = 'the venue takes this kind of manuscript, with ' if entry['band'] == 2 else 'needs '
    return f'{lead}{count}, the largest {heaviest}: {what}'


def rationale_for(number, entry, previous):
    """Plain words from the ordering key. No odds, no ratings."""
    # No ordinal ("First: ...") in the text: the author can reorder, and the number is shown anyway.
    scope = 'its scope is a strong match' if entry['fit_band'] == 'strong' else 'its scope is a reasonable match'
    phrase = _changes_phrase(entry)
    text = f'{phrase[:1].upper()}{phrase[1:]}, and {scope}.'
    if previous is not None:
        if entry['band'] > previous['band']:
            text += f' Placed after the "{BAND_TEXT[previous["band"]].lower()}" venues.'
        elif CLASS_RANK.get(entry['class'], 2) > CLASS_RANK.get(previous['class'], 2):
            text += (f' Placed after the venue before it in our order because it needs '
                     f'{EFFORT_PHRASE.get(entry["class"], "more work")} rather than '
                     f'{EFFORT_PHRASE.get(previous["class"], "less")}.')
        elif FIT_BAND_RANK.get(entry['fit_band'], 1) > FIT_BAND_RANK.get(previous['fit_band'], 1):
            text += ' Same kind of change as the venue before it in our order; its scope is a less close match.'
        elif (entry.get('count', 0), entry.get('quantity', 0)) > (previous.get('count', 0), previous.get('quantity', 0)):
            text += ' Placed after the venue before it in our order because it needs more change of the same kind.'
        elif (entry['similarity'] or 0) < (previous['similarity'] or 0):
            text += ' Same amount of change as the venue before it in our order; its scope is slightly less close.'
        else:
            text += ' Same amount of change as the venue before it in our order.'
    return text


def declined_venue_ids(manuscript):
    """A venue that declined is never proposed again for the same manuscript (spec 6). That includes a
    Flexee editor's rejection the plan never saw, for example one sent while no plan was running (bug H2)."""
    from .models import VenueSubmission
    planned = SubmissionPlanStep.objects.filter(plan__manuscript=manuscript, state='declined')
    rejected = VenueSubmission.objects.filter(manuscript=manuscript, status='rejected')
    return set(planned.values_list('venue_id', flat=True)) | set(rejected.values_list('venue_id', flat=True))


def _author_exclusions(previous_plan):
    """Venues the author skipped or removed in the previous plan: {venue_id: reason}."""
    if previous_plan is None:
        return {}
    out = {str(s.venue_id): 'You skipped this venue in the previous plan.'
           for s in previous_plan.steps.filter(state='skipped')}
    for item in previous_plan.not_included or []:
        if item.get('removed_by_author'):
            out[str(item['venue_id'])] = 'You removed this venue from the plan.'
    return out


def rank(manuscript, version=None, *, previous_plan=None):
    """(entries in plan order, not_included) for the version's current matches."""
    from .author_api import _active_config  # local import: avoids a cycle
    config = settings()
    version = version or manuscript.current_version
    matches = (manuscript.venue_matches.filter(version_id=version.id if version else None, venue__excluded=False)
               .select_related('venue', 'venue__organization'))
    matchable = set(Venue.objects.matchable().values_list('id', flat=True))
    declined = declined_venue_ids(manuscript)
    by_author = _author_exclusions(previous_plan)
    entries, not_included = [], []
    for match in matches:
        if match.venue_id not in matchable:
            continue  # excluded, inactive or listed-tier venues are never in a plan
        name = match.venue.name
        if match.venue_id in declined:
            not_included.append({'venue_id': str(match.venue_id), 'venue': name,
                                 'reason': 'This venue already declined this manuscript.'})
            continue
        if str(match.venue_id) in by_author:
            not_included.append({'venue_id': str(match.venue_id), 'venue': name, 'reason': by_author[str(match.venue_id)],
                                 'removed_by_author': True})
            continue
        entry, why = assess(match, config)
        if entry is None:
            not_included.append({'venue_id': str(match.venue_id), 'venue': name, 'reason': why})
            continue
        entry['config'] = _active_config(match.venue)
        entries.append(entry)
    entries.sort(key=order_key)
    entries = _apply_author_order(entries, previous_plan)
    for extra in entries[config['max_steps']:]:
        not_included.append({'venue_id': str(extra['venue'].id), 'venue': extra['venue'].name,
                             'reason': f'The plan holds {config["max_steps"]} venues; this one came after them.'})
    not_included.sort(key=lambda item: item['venue'].lower())
    return entries[:config['max_steps']], not_included


def _apply_author_order(entries, previous_plan):
    """If the author reordered the previous plan, keep their order for the venues still in it
    and add new venues after them in our order. We do not re-impose our order (spec 6)."""
    if previous_plan is None or not previous_plan.steps.filter(author_moved=True).exists():
        return entries
    previous = {s.venue_id: (s.order, s.author_moved) for s in previous_plan.steps.all()}
    kept = sorted((e for e in entries if e['venue'].id in previous), key=lambda e: previous[e['venue'].id][0])
    for entry in kept:
        entry['author_moved'] = previous[entry['venue'].id][1]
        entry['author_order'] = True
    return kept + [e for e in entries if e['venue'].id not in previous]


# ---------------------------------------------------------------------------
# Generation (runs in the job queue)
# ---------------------------------------------------------------------------

def _log(plan, action, *, step=None, actor='author', from_state='', to_state='', note='', venue_name=''):
    PlanEvent.objects.create(plan=plan, step=step, actor=actor, action=action, from_state=from_state,
                             to_state=to_state, note=(note or '')[:2000],
                             venue_name=(venue_name or (step.venue.name if step else ''))[:300])


def ready_to_plan(manuscript):
    if not manuscript.current_version_id:
        raise PlanError('This manuscript has no version yet.')
    readiness = manuscript.current_readiness().filter(status='completed').first()
    if not readiness or not (readiness.summary or {}).get('ready_for_matching'):
        raise PlanError('Run the readiness check for the current version first.', 'readiness_needed')
    if not manuscript.current_matches().exists():
        raise PlanError('Find matching venues for the current version first.', 'matches_needed')


def live_plan(manuscript):
    return SubmissionPlan.objects.filter(manuscript=manuscript, status__in=LIVE_STATUSES).first()


def check_can_generate(manuscript):
    """Raise if a new plan cannot be generated now. Used by the API before queueing the job."""
    accepted_by = _accepted(manuscript.id)
    if accepted_by:
        raise PlanError(f'{accepted_by} accepted this manuscript, so there is nothing left to plan.',
                        'already_accepted')
    ready_to_plan(manuscript)
    live = live_plan(manuscript)
    if live is None:
        return
    if live.steps.filter(state='submitted').exists():
        raise PlanError('A venue in the current plan is still deciding; record its outcome first.', 'plan_live')
    if live.status == 'paused' and _unanswered(live) is not None:
        raise PlanError('A venue declined: first say whether you revised the manuscript.', 'answer_needed')


@transaction.atomic
def generate_plan(manuscript, *, generated_by=''):
    """A new draft plan for the current version. A live plan without a pending venue is replaced,
    and becomes this plan's previous plan (its declines, skips, removals and order carry over)."""
    check_can_generate(manuscript)
    previous = SubmissionPlan.objects.select_for_update().filter(manuscript=manuscript,
                                                                 status__in=LIVE_STATUSES).first()
    if previous is None:
        previous = SubmissionPlan.objects.filter(manuscript=manuscript).order_by('-created_at').first()
    elif previous:
        _abandon(previous, note='Replaced by a newly generated plan.', actor='system')
    entries, not_included = rank(manuscript, previous_plan=previous)
    if not entries:
        raise PlanError('No venue can be planned yet: ' + (not_included[0]['reason'] if not_included
                        else 'no matching venues.'), 'nothing_to_plan')
    plan = SubmissionPlan.objects.create(
        manuscript=manuscript, manuscript_version=manuscript.current_version, status='draft',
        method_version=METHOD_VERSION, generated_by=generated_by or f'{METHOD_VERSION} (no AI model)',
        previous_plan=previous, not_included=not_included)
    previous_entry = None
    for number, entry in enumerate(entries, start=1):
        rationale = rationale_for(number, entry, previous_entry)
        if entry.get('author_order'):
            rationale += ' You set this order in the previous plan; it is kept.'
        SubmissionPlanStep.objects.create(
            plan=plan, order=number, venue=entry['venue'], venue_config=entry['config'], venue_match=entry['match'],
            rationale=rationale,
            required_changes=entry['items'] + [plain_item(i) for i in preparation_items(entry['config'])],
            band=entry['band'], compliance_distance=entry['distance'], effort_class=entry['class'],
            fit_band=entry['fit_band'], author_moved=bool(entry.get('author_moved')))
        previous_entry = entry
    _log(plan, 'generated', actor='system',
         note=f'{len(entries)} venues for version {manuscript.current_version.number}.')
    return plan


def latest_plan(manuscript):
    return (SubmissionPlan.objects.filter(manuscript=manuscript).order_by('-created_at')
            .prefetch_related('steps__venue', 'events').first())


# ---------------------------------------------------------------------------
# State machine (spec 6)
# ---------------------------------------------------------------------------

def _lock(plan_id, step_id=None):
    plan = SubmissionPlan.objects.select_for_update().get(id=plan_id)
    step = plan.steps.select_for_update().get(id=step_id) if step_id else None
    return plan, step


def _require(step, *states):
    if step.state not in states:
        raise PlanError(f'This is not possible while the venue is "{step.get_state_display()}".', 'bad_state')


def _require_status(plan, *statuses):
    if plan.status not in statuses:
        raise PlanError(f'This is not possible while the plan is "{plan.get_status_display()}".', 'plan_closed')


def _move(step, to_state, action, *, actor='author', note='', fields=()):
    from_state = step.state
    step.state = to_state
    step.save(update_fields=['state', 'updated_at', *fields])
    _log(step.plan, action, step=step, actor=actor, from_state=from_state, to_state=to_state, note=note)


def _set_position(plan, step):
    plan.position = step.order if step else None
    plan.save(update_fields=['position', 'status', 'updated_at'])


def _advance(plan, *, actor='author'):
    """Make the first queued step current (only on an active plan with no step in progress)."""
    if plan.steps.filter(state__in={'current', 'submitted'}).exists():
        return None
    nxt = plan.steps.filter(state='queued').order_by('order').first()
    if nxt is None:
        plan.status = 'completed' if plan.steps.filter(state='accepted').exists() else plan.status
        _set_position(plan, None)
        return None
    _move(nxt, 'current', 'became_current', actor=actor)
    _set_position(plan, nxt)
    return nxt


LIVE_SUBMISSION_STATUSES = ('submitted', 'under_review', 'revision_requested')  # as in author_api


def _live_submission(manuscript_id, *, other_than_venue=None):
    """The Flexee packet a venue is still deciding on for this manuscript, if any."""
    from .models import VenueSubmission
    found = VenueSubmission.objects.filter(manuscript_id=manuscript_id, status__in=LIVE_SUBMISSION_STATUSES)
    if other_than_venue is not None:
        found = found.exclude(venue_id=other_than_venue)
    return found.select_related('venue').order_by('-submitted_at').first()


def _adopt_live_submission(plan, *, actor='venue'):
    """Bug H2: a packet the author sent before Start, while the plan was paused, or straight to a venue
    further down still belongs to the plan. That venue becomes the current step and is linked, so its
    editor's decision reaches the plan. Only on an active plan with nothing in progress."""
    if plan.status != 'active' or plan.steps.filter(state='submitted').exists():
        return None
    live = _live_submission(plan.manuscript_id)
    if live is None:
        return None
    step = plan.steps.select_for_update().filter(venue_id=live.venue_id, state__in={'queued', 'current'}).first()
    if step is None:
        return None
    current = plan.steps.filter(state='current').exclude(id=step.id).first()
    if current is not None:
        _move(current, 'queued', 'moved_back', actor=actor, note=f'The manuscript was sent to {step.venue.name} first.')
    if step.state == 'queued':
        _move(step, 'current', 'became_current', actor=actor)
    _set_position(plan, step)
    step.submitted_version_id = live.version_id or plan.manuscript.current_version_id
    step.venue_submission = live
    _move(step, 'submitted', 'submitted', actor=actor, fields=('submitted_version', 'venue_submission'))
    return step


def _accepted(manuscript_id):
    """Bug H5: a manuscript a venue accepted is finished; no new plan is made for it."""
    from .models import VenueSubmission
    step = (SubmissionPlanStep.objects.filter(plan__manuscript_id=manuscript_id, state='accepted')
            .select_related('venue').first())
    if step is not None:
        return step.venue.name
    item = (VenueSubmission.objects.filter(manuscript_id=manuscript_id, status='accepted')
            .select_related('venue').first())
    return item.venue.name if item is not None else ''


def _unanswered(plan):
    return (plan.steps.filter(state__in={'declined', 'withdrawn'}, revision_answer='')
            .order_by('-order').first())


def _require_current_version(plan):
    """Audit item 15: a plan is for one version. After a newer upload it cannot be started or used to
    send, because the venue would get a text the plan was not made for."""
    current = plan.manuscript.current_version
    if current is not None and plan.manuscript_version_id != current.id:
        raise PlanError(f'This plan was made for version {plan.manuscript_version.number}, and version '
                        f'{current.number} is now the current one. Generate a new plan for it.', 'stale_plan')


@transaction.atomic
def activate(plan_id):
    plan, _ = _lock(plan_id)
    _require_status(plan, 'draft')
    _require_current_version(plan)
    live = _live_submission(plan.manuscript_id)
    if live is not None and not plan.steps.filter(venue_id=live.venue_id, state__in={'queued', 'current'}).exists():
        raise PlanError(f'{live.venue.name} is still deciding on this manuscript. One venue at a time: wait for '
                        'its decision, or withdraw it, before starting this plan.', 'one_venue_at_a_time')
    plan.status = 'active'
    _log(plan, 'activated')
    _advance(plan)
    plan.save(update_fields=['status', 'updated_at'])
    _adopt_live_submission(plan)  # bug H2: a packet already sent to a planned venue is linked now
    return plan


@transaction.atomic
def reorder(plan_id, step_ids):
    """The author's own order for the steps not yet sent. Steps already sent or closed stay where they are."""
    plan, _ = _lock(plan_id)
    _require_status(plan, 'draft', 'active', 'paused')
    movable = list(plan.steps.select_for_update().filter(state__in=MOVABLE_STATES).order_by('order'))
    ids = [str(i) for i in step_ids or []]
    if sorted(ids) != sorted(str(s.id) for s in movable):
        raise PlanError('Send every step that has not been sent yet, each once, in the new order.', 'bad_order', 400)
    by_id = {str(s.id): s for s in movable}
    slots = [s.order for s in movable]
    old_index = {str(s.id): i for i, s in enumerate(movable)}
    # Two passes so the unique (plan, order) constraint never sees a duplicate.
    for step in movable:
        SubmissionPlanStep.objects.filter(id=step.id).update(order=step.order + 1000)
    for index, (step_id, slot) in enumerate(zip(ids, slots)):
        step = by_id[step_id]
        step.order = slot
        moved = old_index[step_id] != index
        step.author_moved = step.author_moved or moved
        step.save(update_fields=['order', 'author_moved', 'updated_at'])
    _log(plan, 'reordered', note=', '.join(by_id[i].venue.name for i in ids))
    if plan.status == 'active':
        current = plan.steps.filter(state='current').first()
        first = plan.steps.filter(state__in=MOVABLE_STATES).order_by('order').first()
        if current and first and current.id != first.id:
            _move(current, 'queued', 'moved_back')
            _move(first, 'current', 'became_current')
            _set_position(plan, first)
    return plan


@transaction.atomic
def skip(plan_id, step_id, *, note=''):
    plan, step = _lock(plan_id, step_id)
    _require_status(plan, 'draft', 'active', 'paused')
    _require(step, 'queued', 'current')
    if not (note or '').strip():
        raise PlanError('Say why you are skipping this venue.', 'needs_reason', 400)
    step.skip_reason = (note or '').strip()[:2000]
    _move(step, 'skipped', 'skipped', note=note, fields=('skip_reason',))
    if plan.status == 'active':
        _advance(plan)
    return step


@transaction.atomic
def remove(plan_id, step_id, *, note=''):
    """Take a venue out of the plan. It is listed as removed and not proposed again on regeneration."""
    plan, step = _lock(plan_id, step_id)
    _require_status(plan, 'draft', 'active', 'paused')
    _require(step, 'queued', 'current')
    name, venue_id = step.venue.name, str(step.venue_id)
    _log(plan, 'removed', note=note, from_state=step.state, venue_name=name)
    step.delete()
    plan.not_included = [i for i in (plan.not_included or []) if i.get('venue_id') != venue_id] + [
        {'venue_id': venue_id, 'venue': name, 'reason': 'You removed this venue from the plan.',
         'removed_by_author': True}]
    plan.save(update_fields=['not_included', 'updated_at'])
    if plan.status == 'active':
        _advance(plan)
    return plan


def reviews_on_flexee(venue):
    """A venue whose own editors work on Flexee: the author sends the packet here and the editor decides here."""
    return venue.trust_tier == Venue.TIER_CLAIMED


@transaction.atomic
def mark_submitted(plan_id, step_id, *, venue_submission=None, actor='author'):
    plan, step = _lock(plan_id, step_id)
    _require_status(plan, 'active')
    _require(step, 'current')
    if venue_submission is None and reviews_on_flexee(step.venue):
        # "I have sent it" would skip the editor: the step must come from a real Flexee packet.
        raise PlanError(f'{step.venue.name} is on Flexee: send the packet from its venue page, and this plan '
                        'updates by itself.', 'send_packet', 409)
    if venue_submission is None:
        # Bug H3: one venue at a time also holds against a Flexee packet sent outside the plan. The
        # manuscript lock is the one a packet submit takes, so the two cannot pass at the same moment.
        from .models import Manuscript
        Manuscript.objects.select_for_update().filter(id=plan.manuscript_id).first()
        live = _live_submission(plan.manuscript_id, other_than_venue=step.venue_id)
        if live is not None:
            raise PlanError(f'{live.venue.name} is still deciding on this manuscript. One venue at a time: wait '
                            'for its decision, or withdraw it, first.', 'one_venue_at_a_time', 409)
        venue_submission = _live_submission(plan.manuscript_id)  # a packet already sent to this same venue
    if venue_submission is None:
        _require_current_version(plan)  # a Flexee submission records the version it actually carries
    step.submitted_version_id = (venue_submission.version_id if venue_submission and venue_submission.version_id
                                 else plan.manuscript.current_version_id)
    step.venue_submission = venue_submission
    _move(step, 'submitted', 'submitted', actor=actor, fields=('submitted_version', 'venue_submission'))
    return step


@transaction.atomic
def report_outcome(plan_id, step_id, outcome, *, actor='author', note=''):
    """accepted | declined | withdrawn. A decline or withdrawal pauses the plan for the author's answer."""
    plan, step = _lock(plan_id, step_id)
    _require_status(plan, 'active')
    if outcome not in {'accepted', 'declined', 'withdrawn'}:
        raise PlanError('outcome must be accepted, declined or withdrawn.', 'bad_outcome', 400)
    _require(step, 'submitted')
    if step.venue_submission_id and actor != 'venue':
        # Audit item 14: a venue on Flexee records its own decision; the author cannot overrule the editor.
        raise PlanError(f'{step.venue.name} is on Flexee: its editor records the decision here. To withdraw, '
                        'withdraw the submission itself.', 'venue_decides', 409)
    step.outcome_note = (note or '').strip()[:4000]
    step.outcome_reported_by, step.outcome_at = actor, timezone.now()
    fields = ('outcome_note', 'outcome_reported_by', 'outcome_at')
    _move(step, outcome, outcome, actor=actor, note=note, fields=fields)
    if outcome == 'accepted':
        plan.status = 'completed'
        _set_position(plan, None)
        _log(plan, 'completed', actor=actor, note=f'Accepted by {step.venue.name}.')
    else:
        # Never advance: the next venue waits until the author says whether they revised.
        plan.status = 'paused'
        plan.save(update_fields=['status', 'updated_at'])
    return step


def _newer_version(plan, step, version_number):
    version = ManuscriptVersion.objects.filter(manuscript=plan.manuscript, number=version_number).first()
    if version is None:
        raise PlanError('That version does not exist.', 'bad_version', 400)
    baseline = step.submitted_version.number if step.submitted_version_id else plan.manuscript_version.number
    if version.number <= baseline:
        raise PlanError(f'Upload a revised version first: the venue saw version {baseline}.', 'needs_revision')
    return version


@transaction.atomic
def answer_revision(plan_id, *, answer, version_number=None, note=''):
    """After a decline: 'unchanged' makes the next step current; 'revised' records the new version
    and the caller queues a regenerated plan against it (spec 6)."""
    plan, _ = _lock(plan_id)
    _require_status(plan, 'paused')
    step = _unanswered(plan)
    if step is None:
        raise PlanError('There is no declined venue waiting for an answer.', 'bad_state')
    if answer == 'unchanged':
        if not (note or '').strip():
            raise PlanError('Say why the next venue should get the manuscript unchanged.', 'needs_reason', 400)
        step.revision_answer = 'unchanged'
        step.save(update_fields=['revision_answer', 'updated_at'])
        _log(plan, 'answered_unchanged', step=step, note=note)
        plan.status = 'active'
        plan.save(update_fields=['status', 'updated_at'])
        _advance(plan)
        return plan, None
    if answer != 'revised':
        raise PlanError('answer must be revised or unchanged.', 'bad_answer', 400)
    if version_number is None:
        raise PlanError('Say which revised version the next venue should get.', 'needs_revision', 400)
    version = _newer_version(plan, step, version_number)
    if plan.manuscript.current_version_id != version.id:
        raise PlanError(f'Version {version.number} is not the current version.', 'bad_version', 400)
    manuscript = plan.manuscript
    readiness = manuscript.current_readiness().filter(status='completed').first()
    if not readiness or not (readiness.summary or {}).get('ready_for_matching') or \
            not manuscript.venue_matches.filter(version=version).exists():
        raise PlanError(f'Run the readiness check and venue matching for version {version.number} first, so the '
                        'plan can be regenerated against it.', 'matches_needed')
    step.revision_answer, step.revision_version = 'revised', version
    step.save(update_fields=['revision_answer', 'revision_version', 'updated_at'])
    _log(plan, 'answered_revised', step=step, note=f'Revised as version {version.number}. {note}'.strip())
    return plan, version


def _abandon(plan, *, note='', actor='author'):
    plan.status = 'abandoned'
    plan.position = None
    plan.save(update_fields=['status', 'position', 'updated_at'])
    # An ended plan has no venue in progress: the step that was "current" goes back to the queue.
    plan.steps.filter(state='current').update(state='queued', updated_at=timezone.now())
    _log(plan, 'abandoned', actor=actor, note=note)


@transaction.atomic
def abandon(plan_id, *, note=''):
    plan, _ = _lock(plan_id)
    _require_status(plan, 'draft', 'active', 'paused')
    deciding = plan.steps.filter(state='submitted').select_related('venue').first()
    if deciding is not None:
        # Audit item 10: ending the plan now would allow a second live submission, and the venue's
        # decision would arrive at a plan that no longer exists.
        raise PlanError(f'{deciding.venue.name} is still deciding. Record its decision, or say you withdrew '
                        'the manuscript, before ending the plan.', 'venue_deciding', 409)
    _abandon(plan, note=note)
    return plan


def reopen_revision_answer(manuscript, reason=''):
    """Audit item 17: the author said "revised" and the new plan could not be generated. The answer is
    taken back, so the paused plan asks again instead of waiting for ever. Returns the step or None."""
    with transaction.atomic():
        plan = (SubmissionPlan.objects.select_for_update()
                .filter(manuscript=manuscript, status='paused').order_by('-created_at').first())
        if plan is None:
            return None
        step = (plan.steps.filter(state__in={'declined', 'withdrawn'}, revision_answer='revised')
                .order_by('-order').first())
        if step is None:
            return None
        step.revision_answer, step.revision_version = '', None
        step.save(update_fields=['revision_answer', 'revision_version', 'updated_at'])
        _log(plan, 'revision_reopened', step=step, actor='system',
             note=('The new plan could not be generated: ' + reason).strip()[:2000])
        return step


def sync_from_submission(submission):
    """Keep a plan in step with a Flexee venue's own workflow (submitted, decision).
    Never raises: plan problems must not break the editorial flow."""
    try:
        status = submission.status
        if status == 'submitted':
            plan = (SubmissionPlan.objects.filter(status='active', manuscript_id=submission.manuscript_id).first())
            if plan is not None:
                with transaction.atomic():
                    plan, _ = _lock(plan.id)
                    return _adopt_live_submission(plan)  # current, queued (bug H2) or nothing to do
            return None
        step = (SubmissionPlanStep.objects.filter(plan__status='active', plan__manuscript_id=submission.manuscript_id,
                                                  venue_id=submission.venue_id, state__in={'current', 'submitted'})
                .select_related('plan').first())
        if step is None:
            return None
        # A transfer moves the manuscript away from this venue: for the plan that is a withdrawal (bug H1).
        outcome = {'accepted': 'accepted', 'rejected': 'declined', 'withdrawn': 'withdrawn',
                   'transferred': 'withdrawn'}.get(status)
        if outcome and step.state == 'submitted':
            return report_outcome(step.plan_id, step.id, outcome, actor='venue')
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception('Plan sync failed for submission %s', submission.id)
    return None


# ---------------------------------------------------------------------------
# Payloads (no probability, score or colour code, by policy)
# ---------------------------------------------------------------------------

def _open_calls(step):
    try:
        from .services.freshness import visible_calls
        calls, _hidden = visible_calls(step.venue, step.venue_config)
        return [{'title': c['title'], 'deadline': c['deadline'], 'url': c['url']} for c in calls]
    except Exception:  # noqa: BLE001  (a freshness problem must not hide the plan)
        return []


def _change_payload(change):
    cls = change.get('class')
    kind = 'prepare' if cls == 'prepare' else CLASS_KIND.get(cls, 'medium')
    # Checked again on the way out, so plans saved before this guard existed are clean too.
    message = plain_words(change.get('message'), cls or 'section', change.get('quantity'), change.get('unit') or '')
    return {'message': message, 'kind': kind, 'kind_label': KIND_LABEL[kind],
            'amount': change.get('quantity'), 'unit': change.get('unit'), 'source': change.get('source_locator')}


RATIONALE_FALLBACK = ('Placed by how much this venue needs the manuscript to change, and then by how close '
                      'its scope is.')


def _plain_rationale(text):
    return text if plain_words(text) == text else RATIONALE_FALLBACK


def step_payload(step):
    from .author_api import _venue_trust_payload
    venue = step.venue
    return {
        'id': str(step.id),
        'order': step.order,
        'state': step.state,
        'state_label': step.get_state_display(),
        'venue': {'id': str(venue.id), 'name': venue.name, 'slug': venue.slug, 'trust': _venue_trust_payload(venue),
                  'on_flexee': reviews_on_flexee(venue)},
        'open_calls': _open_calls(step),
        'rationale': _plain_rationale(step.rationale),
        'band': step.band,
        'band_label': BAND_TEXT.get(step.band, ''),
        'effort': CLASS_LABEL.get(step.effort_class, step.effort_class),
        'required_changes': [_change_payload(c) for c in step.required_changes or []],
        'scope': 'Strong scope match' if step.fit_band == 'strong' else 'Reasonable scope match',
        'author_moved': step.author_moved,
        'venue_submission_id': str(step.venue_submission_id) if step.venue_submission_id else None,
        'submitted_version': step.submitted_version.number if step.submitted_version_id else None,
        'outcome_note': step.outcome_note or None,
        'outcome_reported_by': step.outcome_reported_by or None,
        'revision_answer': step.revision_answer or None,
        'revision_version': step.revision_version.number if step.revision_version_id else None,
        'skip_reason': step.skip_reason or None,
    }


def plan_payload(plan):
    cached = getattr(plan, '_prefetched_objects_cache', {})
    if 'steps' in cached:  # audit item 22: the admin list prefetches, so 50 plans are not 50 x N queries
        steps = sorted(cached['steps'], key=lambda s: s.order)
    else:
        steps = list(plan.steps.select_related('venue', 'venue__organization', 'venue_config', 'submitted_version',
                                               'revision_version').order_by('order'))
    current = next((s for s in steps if s.state in {'current', 'submitted'}), None)
    waiting = _unanswered(plan) if plan.status == 'paused' else None
    return {
        'id': str(plan.id),
        'status': plan.status,
        'status_label': plan.get_status_display(),
        'manuscript_version': plan.manuscript_version.number,
        'position': plan.position,
        'generated_by': plan.generated_by,
        'previous_plan_id': str(plan.previous_plan_id) if plan.previous_plan_id else None,
        'created_at': plan.created_at.isoformat(),
        'current_step_id': str(current.id) if current and plan.status == 'active' else None,
        'waiting_for_author': waiting is not None,
        'waiting_step_id': str(waiting.id) if waiting else None,
        'steps': [step_payload(s) for s in steps],
        'not_included': [dict(n, reason=plain_words(n.get('reason'), 'out')) if isinstance(n, dict) else n
                         for n in plan.not_included or []],
        'events': [{'action': e.action, 'actor': e.actor, 'from': e.from_state, 'to': e.to_state, 'note': e.note,
                    'venue': e.venue_name, 'at': e.created_at.isoformat()}
                   for e in (sorted(cached['events'], key=lambda e: e.created_at) if 'events' in cached
                             else plan.events.order_by('created_at'))],
        'how_ordered': 'Venues are grouped: first those that take the manuscript with no changes, then those that '
                       'take it with changes, then those that need changes. Inside each group, the venue needing '
                       'the least change comes first, and then the one whose scope is closest. Nothing else '
                       'decides the order, and you can change it.',
    }
