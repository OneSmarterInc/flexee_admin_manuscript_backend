"""Parser, conservative linking, idempotence and admin-only access."""
import csv

import pytest
from django.urls import resolve

from review.abdc_jql import build_index, clean_issn, match_row, read_abdc_csv
from review.models import ABDCJournal, IndexedVenue


def _source_csv(tmp_path):
    path = tmp_path / 'abdc.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['journal_title','publisher','issn','issn_online','year_inception',
            'for_code','abdc_rating','abdc_edition','source_url','source_excel_row'])
        writer.writeheader()
        writer.writerow({'journal_title':'Academy of Management Journal','publisher':'Academy of Management',
                         'issn':'0001-4273','issn_online':'1948-0989','year_inception':'1958',
                         'for_code':'3507','abdc_rating':'A*','abdc_edition':'2025','source_excel_row':'15'})
        writer.writerow({'journal_title':'Missing identifier','publisher':'Not found','issn':'',
                         'issn_online':'','abdc_rating':'C','abdc_edition':'2025','source_excel_row':'200'})
    return path


def test_parser_retains_missing_issns_and_rating(tmp_path):
    rows = read_abdc_csv(_source_csv(tmp_path))
    assert len(rows) == 2
    assert rows[0].rating == 'A*'
    assert rows[0].valid_issns == ('0001-4273', '1948-0989')
    assert not rows[1].valid_issns


def test_rejects_bad_issn():
    assert clean_issn('0001-4273') == '0001-4273'
    assert clean_issn('0001-4274') == ''
    assert clean_issn('') == ''


def test_unique_issn_matches_and_missing_unmatched(tmp_path):
    rows = read_abdc_csv(_source_csv(tmp_path))
    ix = build_index([{'id': 19, 'issn_l': '0001-4273','issns':['1948-0989'],
                       'title':'Academy of Management Journal','publisher':'Academy of Management'}])
    assert match_row(rows[0], ix) == ('matched', 'issn', '19', [])
    assert match_row(rows[1], ix) == ('unmatched', '', None, [])


def test_conflicting_issns_are_not_automatically_linked(tmp_path):
    rows = read_abdc_csv(_source_csv(tmp_path))
    ix = build_index([{'id':'2','issn_l':'1234-5679','issns':[],
                      'title':'Academy of Management Journal','publisher':'Academy of Management'}])
    assert match_row(rows[0], ix)[0] == 'pending_review'


def test_ambiguous_matches_require_review(tmp_path):
    rows = read_abdc_csv(_source_csv(tmp_path))
    ix = build_index([{'id':'a','issn_l':'0001-4273','issns':[], 'title':'A','publisher':'X'},
                      {'id':'b','issn_l':'1948-0989','issns':[], 'title':'B','publisher':'Y'}])
    assert match_row(rows[0], ix)[0] == 'pending_review'


@pytest.mark.django_db
def test_abdc_source_is_separate_from_indexed_venue():
    row = ABDCJournal.objects.create(edition=2025, source_row=11, title='Source only', rating='C',
                                     source_url='https://abdc.edu.au/abdc-journal-quality-list/')
    assert ABDCJournal.objects.count() == 1
    assert IndexedVenue.objects.count() == 0
    assert row.indexed_venue is None


def test_admin_route_exists():
    assert resolve('/api/admin/abdc-journals/').url_name == 'abdc-journals'

@pytest.mark.django_db
def test_command_dry_run_then_idempotent_import(tmp_path):
    from django.core.management import call_command
    from io import StringIO

    path = _source_csv(tmp_path)
    call_command('import_abdc_jql', file=str(path), edition=2025, dry_run=True, stdout=StringIO())
    assert ABDCJournal.objects.count() == 0
    call_command('import_abdc_jql', file=str(path), edition=2025, stdout=StringIO())
    assert ABDCJournal.objects.count() == 2
    assert ABDCJournal.objects.get(source_row=15).rating == 'A*'
    call_command('import_abdc_jql', file=str(path), edition=2025, stdout=StringIO())
    assert ABDCJournal.objects.count() == 2
    assert ABDCJournal.objects.get(source_row=200).match_status == 'unmatched'


@pytest.mark.django_db
def test_abdc_api_does_not_expose_anonymous_records(client):
    response = client.get('/api/admin/abdc-journals/')
    assert response.status_code == 401
