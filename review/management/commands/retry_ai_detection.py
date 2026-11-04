from django.core.management.base import BaseCommand, CommandError
from review.models import Manuscript
from review.services.ai_detection import enqueue_detection
import os

class Command(BaseCommand):
    help = 'Queue AI detection for one local manuscript, retrying failed/skipped checks.'
    def add_arguments(self, parser):
        parser.add_argument('manuscript_id')
    def handle(self, *args, **options):
        if os.getenv('AI_DETECTOR_ENABLED','false').lower() != 'true':
            raise CommandError('Enable AI_DETECTOR_ENABLED first.')
        try:
            manuscript=Manuscript.objects.get(pk=options['manuscript_id'])
        except (Manuscript.DoesNotExist, ValueError):
            raise CommandError('Manuscript not found.')
        enqueue_detection(manuscript,retry=True)
        self.stdout.write('Queue request processed. Existing active/completed checks are preserved.')
