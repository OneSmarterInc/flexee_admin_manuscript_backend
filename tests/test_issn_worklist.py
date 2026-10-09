"""8 October instructions, 2.3: import_venue_index --issn-file (a combined worklist drives a run)."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from review.models import IndexedVenue, VenueIndexRun
from review.services import venue_index as vi
from tests.test_venue_index import MEDICINE, MIS, OPS, FakeHttp, env, run_full, source  # noqa: F401 (env: autouse settings)


def issn(seven):
    """A checksum-valid ISSN from seven digits."""
    digits = f'{seven:07d}'
    total = sum(int(d) * w for d, w in zip(digits, range(8, 1, -1)))
    check = (11 - total % 11) % 11
    return f"{digits[:4]}-{digits[4:]}{'X' if check == 10 else check}"


class WorklistHttp(FakeHttp):
    """OpenAlex answers filter=issn:a|b|... from a list of sources."""

    def __init__(self, sources, **kwargs):
        super().__init__([], **kwargs)
        self.sources = sources

    def get_json(self, source_name, url, params=None):
        params = params or {}
        if source_name in self.fail:
            raise vi.IndexSourceError(f'{source_name} returned HTTP 503')
        if source_name == 'openalex' and str(params.get('filter', '')).startswith('issn:'):
            self.calls.append((source_name, url, dict(params)))
            wanted = set(params['filter'][len('issn:'):].split('|'))
            hits = [s for s in self.sources if wanted & ({s['issn_l']} | set(s['issn']))]
            return 200, {'meta': {'count': len(hits)}, 'results': hits}
        return super().get_json(source_name, url, params)


def run_worklist(http, entries):
    run, created = vi.start_index_run(mode='worklist', trigger='command')
    assert created
    run.worklist = {'entries': len(entries)}
    run.save()
    return vi.run_index(run, http=http, worklist=entries)


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def test_text_file_one_issn_per_line(tmp_path):
    a, b = issn(4873330 // 10 * 10 + 1), issn(1234567)
    path = tmp_path / 'list.txt'
    path.write_text(f'{a}\n\n{b.replace("-", "")}\n1234-5678\n{a}\nno issn here\n', encoding='utf-8')
    entries, stats = vi.read_issn_worklist(path)
    assert entries == [(a,), (b,)]
    assert stats == {'rows': 5, 'invalid': 1, 'no_issn': 1, 'duplicates': 1, 'entries': 2, 'truncated': False}


def test_csv_reads_only_issn_columns_and_never_the_rating(tmp_path):
    p1, e1, p2, decoy = issn(1111111), issn(2222222), issn(3333333), issn(4444444)
    path = tmp_path / 'combined.csv'
    path.write_text(
        'Journal Title,Publisher,ISSN,ISSN Online,Rating,Notes\n'
        f'Journal One,Pub,{p1},{e1},A*,see {decoy}\n'
        f'Journal Two,Pub,{p2},,B,\n'
        f'Journal One (online row),Pub,{e1},,A*,\n', encoding='utf-8')
    entries, stats = vi.read_issn_worklist(path)
    assert entries == [(p1, e1), (p2,)]  # print + online = one journal; the online-only row merged into it
    assert decoy not in {i for entry in entries for i in entry}  # a non-ISSN column is never read
    assert stats['duplicates'] == 1


def test_csv_without_header_and_semicolons(tmp_path):
    a = issn(5555555)
    path = tmp_path / 'list.csv'
    path.write_text(f'Some Journal;{a}\n', encoding='utf-8')
    assert vi.read_issn_worklist(path)[0] == [(a,)]


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_worklist_run_adds_exactly_the_listed_journals():
    book = source('S9', 'A Book Series', issn_l=issn(7777777), type_='book series')
    http = WorklistHttp([MIS, MEDICINE, book])
    missing = issn(8888888)
    run = run_worklist(http, [('1873-7625',), (MEDICINE['issn_l'],), (missing,), (book['issn_l'],)])

    assert run.status == 'completed', run.summary
    titles = set(IndexedVenue.objects.values_list('title', flat=True))
    # The list decides coverage: the cardiology journal is kept even though it is outside the subject filter.
    assert titles == {MIS['display_name'], MEDICINE['display_name']}
    assert set(IndexedVenue.objects.values_list('in_worklist', flat=True)) == {True}
    assert run.worklist['not_found'] == 1 and run.worklist['not_found_sample'] == [missing]
    assert run.worklist['not_journal'] == 1
    assert run.catalogue_method == 'issn_worklist' and run.catalogue_complete is False
    assert run.not_found_issns == [missing]
    assert 'not found in OpenAlex' in run.summary
    # Crossref/DOAJ checks and screening still run after the worklist.
    assert any(call[0] == 'crossref' for call in http.calls)
    assert IndexedVenue.objects.exclude(screening_status='not_screened').count() == 2


@pytest.mark.django_db
def test_lookups_are_batched():
    sources = [source(f'S{100 + n}', f'Journal {n}', issn_l=issn(1000000 + n)) for n in range(120)]
    http = WorklistHttp(sources)
    run = run_worklist(http, [(s['issn_l'],) for s in sources])
    lookups = [c for c in http.calls if c[0] == 'openalex']
    assert len(lookups) == 3  # 50 + 50 + 20
    assert all(len(c[2]['filter'][5:].split('|')) <= vi.WORKLIST_BATCH for c in lookups)
    assert run.created_count == 120


@pytest.mark.django_db
def test_rerun_refreshes_instead_of_duplicating():
    run_worklist(WorklistHttp([MIS]), [(MIS['issn_l'],)])
    second = run_worklist(WorklistHttp([MIS]), [(MIS['issn_l'],)])
    assert IndexedVenue.objects.count() == 1
    assert (second.created_count, second.updated_count) == (0, 1)


@pytest.mark.django_db
def test_monthly_subject_refresh_never_removes_or_hides_worklist_journals():
    run_worklist(WorklistHttp([MEDICINE, OPS]), [(MEDICINE['issn_l'],), (OPS['issn_l'],)])
    run_full(FakeHttp([[MIS]]))  # MIS is not added by the worklist; OPS/MEDICINE are not seen
    run_full(FakeHttp([[MIS, MEDICINE]]))  # MEDICINE seen again and judged out of scope by the subject rules
    records = {r.title: r for r in IndexedVenue.objects.all()}
    assert MEDICINE['display_name'] in records and OPS['display_name'] in records
    assert records[MEDICINE['display_name']].missing_since is None
    assert records[OPS['display_name']].missing_since is None
    assert records[MIS['display_name']].in_worklist is False


@pytest.mark.django_db
def test_openalex_failure_keeps_what_was_saved():
    http = WorklistHttp([MIS], fail={'openalex'})
    run = run_worklist(http, [(MIS['issn_l'],)])
    assert run.worklist['stopped_early'] is True
    assert any(e['source'] == 'openalex' for e in run.errors)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_command_imports_the_file_and_saves_the_not_found_list(tmp_path, monkeypatch, capsys):
    missing = issn(9999991)
    path = tmp_path / 'worklist.txt'
    path.write_text(f"{MIS['issn_l']}\n{missing}\n", encoding='utf-8')
    monkeypatch.setattr(vi, 'IndexHttp', lambda config: WorklistHttp([MIS]))
    call_command('import_venue_index', '--issn-file', str(path), '--no-pages')
    out = capsys.readouterr().out
    assert 'Worklist worklist.txt: 2 rows -> 2 journals' in out
    assert IndexedVenue.objects.get().in_worklist is True
    run = VenueIndexRun.objects.get()
    assert run.mode == 'worklist' and run.worklist['file'] == 'worklist.txt'
    assert (tmp_path / 'worklist.not-found.txt').read_text().split() == [missing]


@pytest.mark.django_db
def test_command_refuses_bad_input(tmp_path):
    empty = tmp_path / 'empty.txt'
    empty.write_text('no issns here\n', encoding='utf-8')
    with pytest.raises(CommandError, match='No valid ISSNs'):
        call_command('import_venue_index', '--issn-file', str(empty))
    with pytest.raises(CommandError, match='not found'):
        call_command('import_venue_index', '--issn-file', str(tmp_path / 'nope.txt'))
    with pytest.raises(CommandError, match='cannot be combined'):
        call_command('import_venue_index', '--issn-file', str(empty), '--rules-only')
    assert not VenueIndexRun.objects.exists()
