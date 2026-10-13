"""Found while testing on 12 October: choosing a venue before the AI had read the manuscript queued a
venue assessment that could only fail, and the author saw "The background job could not be completed".

Now the request is refused at once with a clear reason, and a job that still fails for this reason
explains what to do."""
import pytest

from review.models import ReviewJob, VenueSubmission
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401


def _submission(author, semantic):
    m = make_manuscript(author)
    profile = dict(m.parsed_profile or {})
    if semantic:
        profile['semantic'] = {'topic': 'Applied AI in organizations'}
    else:
        profile.pop('semantic', None)
    m.parsed_profile = profile
    m.save(update_fields=['parsed_profile'])
    venue = make_venue('Field Notes Journal', 'fnj')
    return VenueSubmission.objects.create(manuscript=m, venue=venue, status='draft',
                                          venue_config=venue.agent_configs.get())


@pytest.mark.django_db
def test_choosing_a_venue_before_readiness_is_refused_with_a_reason():
    client, author = author_client()
    submission = _submission(author, semantic=False)
    response = client.post(f'/api/author/venue-submissions/{submission.id}/assessment/run/', data='{}',
                           content_type='application/json')
    assert response.status_code == 409 and response.json()['code'] == 'readiness_needed'
    assert 'Readiness' in response.json()['detail']
    assert not ReviewJob.objects.filter(job_type='venue_assessment').exists()


@pytest.mark.django_db
def test_after_readiness_the_assessment_is_queued():
    client, author = author_client()
    submission = _submission(author, semantic=True)
    response = client.post(f'/api/author/venue-submissions/{submission.id}/assessment/run/', data='{}',
                           content_type='application/json')
    assert response.status_code == 202
    assert ReviewJob.objects.filter(job_type='venue_assessment', reference_id=str(submission.id)).exists()


@pytest.mark.django_db
def test_a_job_that_failed_for_missing_readiness_says_what_to_do():
    client, author = author_client()
    submission = _submission(author, semantic=True)
    job = ReviewJob.objects.create(job_type='venue_assessment', reference_id=str(submission.id), status='failed',
                                   error_message='Run semantic readiness before the venue-specific assessment.')
    body = client.get(f'/api/author/jobs/{job.id}/').json()
    assert 'Finish the readiness check first' in body['error']
