"""Install reviewed document profiles for the latest manuscript's matched venues, locally."""
import json
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from review.models import Manuscript, Venue, VenueAgentConfig

class Command(BaseCommand):
    help = 'Install separately verified, partial document checks for current local venue matches.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        if settings.DATABASES['default'].get('HOST') not in ('localhost', '127.0.0.1', '::1'):
            raise CommandError('Local database only.')
        profiles = json.loads((Path(__file__).resolve().parents[2] / 'services' / 'verified_document_profiles.json').read_text(encoding='utf-8'))
        manuscript = Manuscript.objects.order_by('-created_at').first()
        if not manuscript: raise CommandError('No manuscript.')
        venue_ids = list(manuscript.current_matches().values_list('venue_id', flat=True))
        with transaction.atomic():
            for venue in Venue.objects.select_for_update().filter(pk__in=venue_ids).order_by('name'):
                old = venue.agent_configs.filter(active=True).order_by('-version').first()
                if not old:
                    self.stdout.write('NO ACTIVE RULES: ' + venue.name); continue
                if (old.policies or {}).get('document_check_profile') == 'oscm-2026-03-01':
                    self.stdout.write('EXISTING OSCM PROFILE: ' + venue.name); continue
                profile = profiles.get(venue.name)
                if not profile:
                    self.stdout.write('EXISTING STRUCTURED CHECKS ONLY: ' + venue.name); continue
                if (old.policies or {}).get('verified_document_checks') == profile:
                    self.stdout.write('ALREADY ENABLED: ' + venue.name); continue
                if not options['apply']:
                    self.stdout.write('WOULD ENABLE: ' + venue.name); continue
                fields = {f.name:getattr(old,f.name) for f in VenueAgentConfig._meta.concrete_fields
                          if f.name not in {'id','venue','version','created_at','effective_at','active'}}
                fields['policies'] = {**(old.policies or {}), 'verified_document_checks':profile}
                version = (venue.agent_configs.order_by('-version').values_list('version',flat=True).first() or 0) + 1
                new = VenueAgentConfig.objects.create(venue=venue,version=version,active=True,**fields)
                venue.agent_configs.exclude(pk=new.pk).filter(active=True).update(active=False)
                self.stdout.write(f'ENABLED v{version}: {venue.name}')
