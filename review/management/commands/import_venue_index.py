import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from review.models import VenueIndexRun
from review.services.venue_index import IndexConfig, coverage_counts, run_index, start_index_run


class Command(BaseCommand):
    help = ('Build or refresh the venue index spine from OpenAlex, then check records against Crossref and DOAJ '
            '(build plan step 2). No AI is used. Safe to re-run: records are updated, never duplicated, and '
            'Crossref/DOAJ checks already done in the last 30 days are skipped.')

    def add_arguments(self, parser):
        parser.add_argument('--enrich-only', action='store_true',
                            help='Skip the OpenAlex refresh; only finish Crossref/DOAJ checks that are due.')
        parser.add_argument('--minutes', type=int, default=0,
                            help='Stop after this many minutes (default VENUE_INDEX_TIME_LIMIT_MINUTES, 25). '
                                 'Unfinished checks continue next time.')

    def handle(self, *args, **options):
        mode = 'enrich' if options['enrich_only'] else 'full'
        run, created = start_index_run(mode=mode, trigger='command', requested_by='manage.py')
        if not created:
            raise CommandError(f'An index run is already {run.status} (started {run.created_at:%Y-%m-%d %H:%M}). '
                               'Wait for it to finish, or it is closed automatically after an hour.')
        config = IndexConfig()
        started = time.monotonic()
        self.stdout.write(f'Importing field profile "{config.profile}" ({len(config.subfields)} subfields). '
                          'Press Ctrl+C to stop; everything saved so far is kept.')
        if not config.polite:
            self.stdout.write(self.style.WARNING(
                'Tip: set VENUE_INDEX_CONTACT_EMAIL in .env. Crossref then allows 3 checks in parallel (about 3x faster).'))

        def progress(message):
            elapsed = int(time.monotonic() - started)
            self.stdout.write(f'  [{elapsed // 60:02d}:{elapsed % 60:02d}] {message}')
            self.stdout.flush()

        minutes = options['minutes']
        try:
            run = run_index(run, progress=progress, time_limit_seconds=minutes * 60 if minutes > 0 else None)
        except KeyboardInterrupt:
            VenueIndexRun.objects.filter(id=run.id).update(
                status='failed', completed_at=timezone.now(),
                summary='Stopped with Ctrl+C. Journals and checks saved before that were kept.')
            self.stdout.write(self.style.WARNING('\nStopped. Everything saved so far was kept; run the command again '
                                                 'to continue (checks already done are skipped).'))
            return

        for error in run.errors[-5:]:
            self.stdout.write(self.style.WARNING(f"  {error['source']}: {error['detail']}"))
        counts = coverage_counts(config.profile)
        style = self.style.SUCCESS if run.status == 'completed' else self.style.ERROR
        self.stdout.write(style(f'{run.status.title()}: {run.summary}'))
        self.stdout.write(f"Index now holds {counts['total']} journals: {counts['crossref']} registered with Crossref, "
                          f"{counts['doaj']} in DOAJ, {counts['linked']} linked to live venues, "
                          f"{counts['not_enriched']} not yet checked.")
        if run.status != 'completed':
            raise CommandError('The import did not complete; records saved before the problem were kept.')
