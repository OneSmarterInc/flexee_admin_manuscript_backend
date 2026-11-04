import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from review.models import JournalRuleCheck, ReadinessAssessment
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue

@pytest.fixture(autouse=True)
def local_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def setup_check():
    client, author = author_client()
    manuscript = make_manuscript(author)
    manuscript.refresh_from_db()
    venue = make_venue('Example Journal', 'example-check')
    config = venue.agent_configs.get()
    config.structured_desk_rejection_rules = [
        {'field':'word_count','operator':'>','value':4,'message':'Maximum four words'},
        {'field':'required_sections','operator':'missing_any','value':['References'],'message':'References heading required'},
        {'field':'unsupported','operator':'==','value':1,'message':'Manual rule'},
    ]
    config.save()
    return client, manuscript, venue, config

@pytest.mark.django_db
def test_revision_rechecks_file_and_preserves_history():
    client, m, v, c = setup_check()
    url = f'/api/author/manuscripts/{m.id}/journal-checks/{v.id}/'
    before = ReadinessAssessment.objects.count()
    response = client.post(url)
    assert response.status_code == 200
    first = response.json()['report']
    assert first['counts']['failed'] == 2
    assert first['counts']['not_checked'] >= 2
    assert client.post(url).json()['report'] == first
    assert JournalRuleCheck.objects.count() == 1
    upload = client.post(f'/api/author/manuscripts/{m.id}/versions/', {
        'manuscript': SimpleUploadedFile('revision.md', b'# References\nOne.\n', content_type='text/markdown'),
        'change_note': 'Shortened text and added references heading',
    })
    assert upload.status_code == 201, upload.content
    assert client.get(url).json()['report'] is None
    second = client.post(url).json()['report']
    assert second['manuscript_version'] == 2
    assert second['previous_version'] == 1
    assert sum(x['change'] == 'resolved' for x in second['checks']) == 2
    assert second['counts']['failed'] == 0
    assert second['counts']['not_checked'] >= 2
    assert JournalRuleCheck.objects.count() == 2
    assert JournalRuleCheck.objects.order_by('id').first().report == first
    assert ReadinessAssessment.objects.count() == before

@pytest.mark.django_db
def test_unreadable_file_never_resolves_text_rules(monkeypatch):
    client, m, v, c = setup_check()
    monkeypatch.setattr('review.author_api.load_manuscript_text', lambda m: '')
    body = client.post(f'/api/author/manuscripts/{m.id}/journal-checks/{v.id}/').json()['report']
    assert all(x['status'] == 'not_checked' for x in body['checks'] if x['id'].startswith('rule-'))
    assert not any(x['change'] == 'resolved' for x in body['checks'])

@pytest.mark.django_db
def test_other_author_and_hidden_venue_cannot_be_checked():
    client, m, v, c = setup_check()
    other, _ = author_client('other-check@example.com')
    url = f'/api/author/manuscripts/{m.id}/journal-checks/{v.id}/'
    assert other.post(url).status_code in (401,403,404)
    v.excluded = True
    v.save()
    assert client.post(url).status_code == 404
    assert JournalRuleCheck.objects.count() == 0

@pytest.mark.django_db
def test_new_config_does_not_claim_old_failures_resolved():
    client, m, v, c = setup_check()
    url = f'/api/author/manuscripts/{m.id}/journal-checks/{v.id}/'
    client.post(url)
    c.active = False
    c.save()
    c.pk = None
    c.version = 2
    c.active = True
    c.save()
    body = client.post(url).json()['report']
    assert body['config_version'] == 2
    assert body['previous_version'] is None
    assert not any(x['change'] == 'resolved' for x in body['checks'])

@pytest.mark.django_db
def test_unchanged_revision_does_not_clear_failures():
    client, m, v, c = setup_check()
    url = f'/api/author/manuscripts/{m.id}/journal-checks/{v.id}/'
    first = client.post(url).json()['report']
    response = client.post(f'/api/author/manuscripts/{m.id}/versions/', {
        'manuscript':SimpleUploadedFile('same.md', b'# AI quality inspection\n\nSome text.\n'),
        'change_note':'Same document'})
    assert response.status_code == 409
    assert client.get(url).json()['report'] == first
