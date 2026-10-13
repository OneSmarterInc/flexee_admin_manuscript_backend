"""12 October audit, submission plan (items 9, 10 and 11), plus an ended plan showing no current venue.

9  A version sent to a venue outside Flexee is never overwritten, also after its plan ends.
10 A plan cannot be ended while a venue is deciding (no second live submission, no lost decision).
11 Odds and rating language from venue rule text never reaches the plan.
"""
import pytest

from review.gap_classes import BANNED_WORDS, plain_item, plain_words
from review.models import SubmissionPlan, SubmissionPlanStep, VenueMatch
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_submission_plan import activate, generate, post, setup_plan, step

EDIT = {'title': 'x', 'author': 'Priya', 'manuscript_type': 'practitioner_article', 'disclosure': 'None',
        'attestation': 'true'}


def _sent_outside_flexee(client, author):
    m, _vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    assert step(client, plan, first, 'submitted').status_code == 200
    return m, plan, first


# ---------------------------------------------------------------------------
# Item 9
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_version_sent_outside_flexee_cannot_be_overwritten_after_the_plan_ends():
    client, author = author_client()
    m, plan, first = _sent_outside_flexee(client, author)
    assert step(client, plan, first, 'outcome', outcome='withdrawn', note='Pulled it.').status_code == 200
    assert post(client, f'/api/author/plans/{plan["id"]}/abandon/', note='Done').status_code == 200
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'abandoned'
    edit = client.post(f'/api/author/manuscripts/{m.id}/update/', EDIT)
    assert edit.status_code == 409 and edit.json()['code'] == 'manuscript_locked'
    assert client.get(f'/api/author/manuscripts/{m.id}/').json()['manuscript']['editable'] is False


@pytest.mark.django_db
def test_a_version_never_sent_stays_editable_after_the_plan_ends():
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    plan = generate(client, m)
    assert post(client, f'/api/author/plans/{plan["id"]}/abandon/').status_code == 200
    assert client.get(f'/api/author/manuscripts/{m.id}/').json()['manuscript']['editable'] is True


# ---------------------------------------------------------------------------
# Item 10 (and the ended plan showing a "current" venue)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_plan_cannot_end_while_a_venue_is_deciding():
    client, author = author_client()
    _m, plan, first = _sent_outside_flexee(client, author)
    response = post(client, f'/api/author/plans/{plan["id"]}/abandon/', note='Changed my mind')
    assert response.status_code == 409 and response.json()['code'] == 'venue_deciding'
    assert first['venue']['name'] in response.json()['detail']
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'active'
    # Once the decision is recorded, the plan can end.
    assert step(client, plan, first, 'outcome', outcome='accepted').status_code == 200
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'completed'


@pytest.mark.django_db
def test_an_ended_plan_has_no_current_venue():
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    assert SubmissionPlanStep.objects.filter(plan_id=plan['id'], state='current').exists()
    body = post(client, f'/api/author/plans/{plan["id"]}/abandon/').json()['plan']
    assert body['status'] == 'abandoned' and body['current_step_id'] is None
    assert all(s['state'] != 'current' for s in body['steps'])


# ---------------------------------------------------------------------------
# Item 11
# ---------------------------------------------------------------------------

def test_plain_words_replaces_odds_and_rating_language():
    assert plain_words('Cut about 500 words (limit 8,000).', 'edit') == 'Cut about 500 words (limit 8,000).'
    assert not BANNED_WORDS.search(plain_words('Likely desk rejected (90% odds).', 'edit'))
    assert plain_words('Over the limit: likely desk rejected.', 'edit', 500, 'words') == \
        'Cut about 500 words to meet a venue rule.'
    assert plain_item({'message': 'Top tier venues expect this.', 'class': 'section'})['message'] == \
        'Add or restructure a section to meet a venue rule.'


@pytest.mark.django_db
def test_venue_rule_text_with_odds_never_reaches_the_plan():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    config = vs['edit'].agent_configs.get()
    config.structured_desk_rejection_rules = [
        {'field': 'word_count', 'operator': '>', 'value': 500,
         'message': 'Over 500 words: likely desk rejected (90% odds).'}]
    config.required_submission_items = [{'label': 'Statement on why this is a top tier paper', 'required': True}]
    config.save()
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    VenueMatch.objects.filter(manuscript=m, venue=vs['edit']).update(topic_similarity=0.65)
    plan = generate(client, m)
    payload = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    texts = [s['rationale'] for s in payload['steps']]
    texts += [c['message'] or '' for s in payload['steps'] for c in s['required_changes']]
    texts += [n.get('reason') or '' for n in payload['not_included']]
    assert names_with(payload, 'Concise Letters'), plan
    for text in texts:
        assert not BANNED_WORDS.search(text), text
    # Saved clean too, not only shown clean.
    for saved in SubmissionPlanStep.objects.filter(plan_id=plan['id']):
        assert not BANNED_WORDS.search(saved.rationale)
        assert not any(BANNED_WORDS.search(c.get('message') or '') for c in saved.required_changes)


@pytest.mark.django_db
def test_a_plan_saved_before_the_guard_is_shown_clean():
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    plan = generate(client, m)
    saved = SubmissionPlanStep.objects.filter(plan_id=plan['id']).first()
    saved.rationale = 'Placed first: 90% chance of acceptance.'
    saved.required_changes = [{'message': 'Likely rejected without it.', 'class': 'edit'}]
    saved.save()
    payload = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    shown = next(s for s in payload['steps'] if s['id'] == str(saved.id))
    assert not BANNED_WORDS.search(shown['rationale'])
    assert not BANNED_WORDS.search(shown['required_changes'][0]['message'])


def names_with(payload, name):
    return [s for s in payload['steps'] if s['venue']['name'] == name]
