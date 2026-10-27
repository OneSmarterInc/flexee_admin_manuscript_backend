"""Loading human-checked rules for journals the crawler cannot reach (27 October).

Elsevier, SAGE, Wiley, Taylor and Francis and INFORMS refuse our crawler, and the local model
returns nothing from the pages a search does deliver. Of 2,648 imported ABDC journals, 2 carry
rules. This is the path for the rest: a person reads the journal's own guidelines page and records
what it says, with the URL they read.
"""
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from review.models import EditorUser, IndexedVenue, Venue
from tests.test_index_rules import journal


def entry(**overrides):
    base = {
        'title': 'Journal of Applied AI in Organizations',
        'issn': '0048-7333',
        'publisher': 'Meridian Academic Publishing',
        'guidelines_url': 'https://society.example/author-guidelines',
        'article_types': ['Research article', 'Research note'],
        'required_submission_items': [{'label': 'Cover letter', 'type': 'textarea'}],
        'structured_desk_rejection_rules': [
            {'field': 'word_count', 'operator': '>', 'value': 8000,
             'message': 'Manuscripts are limited to 8,000 words.'}],
        'aims_scope': 'Empirical and conceptual work on AI in organizations.',
    }
    base.update(overrides)
    return base


def write(tmp_path, entries):
    path = tmp_path / 'rules.json'
    path.write_text(json.dumps(entries), encoding='utf-8')
    return str(path)


@pytest.fixture
def root():
    return EditorUser.objects.create(email='root@example.com', password_hash='x', platform_superuser=True)


@pytest.mark.django_db
def test_a_journal_is_published_with_the_rules_and_the_url_they_came_from(tmp_path, root):
    record = journal()
    call_command('import_venue_rules', '--file', write(tmp_path, [entry()]))
    record.refresh_from_db()
    venue = record.venue
    assert venue is not None and venue.trust_tier == Venue.TIER_VERIFIED_INDEX
    assert venue.name == record.title
    assert venue.source_urls == ['https://society.example/author-guidelines']
    config = venue.agent_configs.get()
    assert config.active and config.article_types == ['Research article', 'Research note']
    assert config.required_submission_items[0]['label'] == 'Cover letter'
    assert config.structured_desk_rejection_rules[0]['value'] == 8000
    assert record.rules_status == 'ready'


@pytest.mark.django_db
def test_dry_run_writes_nothing(tmp_path, root):
    journal()
    call_command('import_venue_rules', '--file', write(tmp_path, [entry()]), '--dry-run')
    assert Venue.objects.count() == 0


@pytest.mark.django_db
def test_a_journal_that_is_already_live_is_left_alone(tmp_path, root):
    record = journal()
    call_command('import_venue_rules', '--file', write(tmp_path, [entry()]))
    record.refresh_from_db()
    first = record.venue_id
    call_command('import_venue_rules', '--file', write(tmp_path, [entry()]))
    record.refresh_from_db()
    assert record.venue_id == first and Venue.objects.count() == 1


@pytest.mark.django_db
def test_an_entry_with_no_matching_index_record_is_skipped(tmp_path, root):
    journal()
    call_command('import_venue_rules',
                 '--file', write(tmp_path, [entry(issn='9999-9999', title='Nowhere Journal')]))
    assert Venue.objects.count() == 0


@pytest.mark.django_db
def test_a_bad_entry_stops_the_whole_file_before_anything_is_written(tmp_path, root):
    journal()
    entries = [entry(), entry(title='Second Journal', guidelines_url='not-a-url')]
    with pytest.raises(CommandError, match='guidelines_url'):
        call_command('import_venue_rules', '--file', write(tmp_path, entries))
    assert Venue.objects.count() == 0


@pytest.mark.django_db
def test_an_entry_with_no_rules_is_refused(tmp_path, root):
    journal()
    bare = {'title': 'Empty Journal', 'issn': '0048-7333',
            'guidelines_url': 'https://society.example/authors'}
    with pytest.raises(CommandError, match='no rules in this entry'):
        call_command('import_venue_rules', '--file', write(tmp_path, [bare]))
    assert Venue.objects.count() == 0


@pytest.mark.django_db
def test_an_invalid_rule_names_the_journal_it_came_from(tmp_path, root):
    journal()
    bad = entry(structured_desk_rejection_rules=[{'field': 'page_count', 'operator': '>', 'value': 40,
                                                  'message': 'Too long.'}])
    with pytest.raises(CommandError, match='Journal of Applied AI'):
        call_command('import_venue_rules', '--file', write(tmp_path, [bad]))


@pytest.mark.django_db
def test_it_refuses_to_run_without_a_platform_superuser(tmp_path):
    journal()
    with pytest.raises(CommandError, match='No platform superuser'):
        call_command('import_venue_rules', '--file', write(tmp_path, [entry()]))
