from django.core.management.base import BaseCommand
from django.utils import timezone
from review.models import RevokedSession


class Command(BaseCommand):
    help = 'Remove expired session revocations (expired tokens remain invalid).'

    def handle(self, *args, **options):
        count, _ = RevokedSession.objects.filter(expires_at__lt=timezone.now()).delete()
        self.stdout.write(f'Removed {count} expired session revocations.')
