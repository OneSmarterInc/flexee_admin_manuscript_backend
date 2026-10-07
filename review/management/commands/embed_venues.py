"""Pre-compute topic vectors for matchable venues (build plan step 5).

Matching embeds any missing venue on the fly, so this is optional. Run it after publishing many
venues so the first author to match does not wait for them.

    python manage.py embed_venues
    python manage.py embed_venues --check   # test the embedding model with one sentence
"""
from django.core.management.base import BaseCommand, CommandError

from review.models import Venue, VenueEmbedding
from review.services import venue_shortlist as vs


class Command(BaseCommand):
    help = 'Embed the aims and scope of every matchable venue with the local embedding model.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help='Only check that the embedding model answers.')
        parser.add_argument('--minutes', type=int, default=30, help='Time limit (default 30).')

    def handle(self, *args, **options):
        model = vs.embed_model()
        try:
            vector = vs.ollama_embed(['Information systems research on enterprise software adoption.'], model=model)[0]
        except vs.EmbeddingUnavailable as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f'Embedding model {model} answers ({len(vector)} dimensions).')
        if options['check']:
            return
        venues = list(Venue.objects.matchable())
        vectors = vs.ensure_venue_vectors(venues, time_limit=options['minutes'] * 60,
                                          say=lambda m: self.stdout.write(f'  {m}'))
        stale = VenueEmbedding.objects.exclude(model=model).count()
        self.stdout.write(self.style.SUCCESS(
            f'{len(vectors)} of {len(venues)} matchable venues have a current topic vector.'
            + (f' {stale} older vectors from another model are replaced as venues are matched.' if stale else '')))
