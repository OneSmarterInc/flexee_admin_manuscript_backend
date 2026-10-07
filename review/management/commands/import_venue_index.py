from django.core.management.base import BaseCommand, CommandError

from review.services.venue_index import IndexConfig, coverage_counts, run_index, start_index_run


class Command(BaseCommand):
    help = ('Build or refresh the venue index spine from OpenAlex, then check records against Crossref and DOAJ '
            '(build plan step 2). No AI is used. Safe to re-run: records are updated, never duplicated.')

    def add_arguments(self, parser):
        parser.add_argument('--enrich-only', action='store_true',
                            help='Skip the OpenAlex refresh; only finish Crossref/DOAJ checks that are due.')

    def handle(self, *args, **options):
        mode = 'enrich' if options['enrich_only'] else 'full'
        run, created = start_index_run(mode=mode, trigger='command', requested_by='manage.py')
        if not created:
            raise CommandError(f'An index run is already {run.status} (started {run.created_at:%Y-%m-%d %H:%M}). '
                               'Wait for it to finish, or it is closed automatically after an hour.')
        config = IndexConfig()
        self.stdout.write(f'Importing field profile "{config.profile}" ({len(config.subfields)} subfields)…')
        run = run_index(run)
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
