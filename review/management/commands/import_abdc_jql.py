"""Import an official ABDC edition without altering published journal trust tiers."""
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from review.abdc_jql import build_index, match_row, read_abdc_csv
from review.models import ABDCJournal, IndexedVenue


class Command(BaseCommand):
    help = 'Import the ABDC full metadata CSV into a separate, provenance-preserving rating catalogue.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='Path to abdc_2025_flexee_full_metadata.csv (NOT ISSN worklist)')
        parser.add_argument('--edition', type=int, default=2025)
        parser.add_argument('--dry-run', action='store_true', help='Validate and calculate matches without writing anything')

    def handle(self, *args, **options):
        file_path = Path(options['file']).expanduser()
        if not file_path.is_file():
            raise CommandError(f'Cannot find file: {file_path}')
        try:
            rows = read_abdc_csv(file_path, edition=options['edition'])
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        indexed = IndexedVenue.objects.values('id', 'issn_l', 'issns', 'title', 'publisher')
        index = build_index(indexed)
        existing = {r.source_row: r for r in ABDCJournal.objects.filter(edition=options['edition'])}
        stats = Counter()
        pending = []
        to_save = []
        for source in rows:
            status, method, link_id, candidates = match_row(source, index)
            old = existing.get(source.source_row)
            # Manual adjudications are sticky on reimport; never clobber them.
            if old and old.match_method == 'manual' and old.indexed_venue_id:
                status, method, link_id, candidates = 'matched', 'manual', str(old.indexed_venue_id), []
            stats[status] += 1
            if status != 'matched':
                pending.append(source.title)
            if options['dry_run']:
                continue
            defaults = dict(
                title=source.title, publisher=source.publisher, issn=source.issn,
                issn_online=source.issn_online, year_inception=source.year_inception,
                for_code=source.for_code, rating=source.rating, source_url=source.source_url,
                source_file=file_path.name, indexed_venue_id=link_id,
                match_status=status, match_method=method, candidates=candidates,
            )
            to_save.append((source, defaults))
        if not options['dry_run']:
            with transaction.atomic():
                for source, defaults in to_save:
                    ABDCJournal.objects.update_or_create(
                        edition=source.edition, source_row=source.source_row, defaults=defaults,
                    )
        self.stdout.write(('DRY RUN: ' if options['dry_run'] else 'IMPORTED: ') +
                          f'{len(rows)} source rows; matched {stats["matched"]}; '
                          f'pending review {stats["pending_review"]}; unmatched {stats["unmatched"]}.')
        self.stdout.write(f'No IndexedVenue, Venue, verification tier or publication setting was changed.')
        if pending:
            self.stdout.write(f'Needs follow-up: {len(pending)}; example: {", ".join(pending[:5])}')
