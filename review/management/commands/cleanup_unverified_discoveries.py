from django.core.management.base import BaseCommand

from review.models import DiscoveredVenue
from review.services.venue_discovery import BLOCKED_NOTE


class Command(BaseCommand):
    help = ('Remove staged discovered venues that were never verified on their official site. '
            'Only records still in "New" and not added to Venue Agents are touched. Dry run unless --yes.')

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='Actually delete (default is a dry run).')
        parser.add_argument('--all-unclear', action='store_true',
                            help='Also remove every New venue whose status is still "unclear".')

    def handle(self, *args, **options):
        targets = DiscoveredVenue.objects.filter(discovery_status='new', added_venue__isnull=True)
        if options['all_unclear']:
            targets = targets.filter(acceptance_status='unclear')
        else:
            targets = targets.filter(last_error=BLOCKED_NOTE)
        names = list(targets.order_by('name').values_list('name', flat=True))
        for name in names[:50]:
            self.stdout.write(f'  - {name}')
        if len(names) > 50:
            self.stdout.write(f'  ... and {len(names) - 50} more')
        if not options['yes']:
            self.stdout.write(self.style.WARNING(f'Dry run: {len(names)} venue(s) would be removed. Add --yes to remove them.'))
            return
        deleted, _ = targets.delete()
        self.stdout.write(self.style.SUCCESS(f'Removed {deleted} unverified discovered venue(s).'))
