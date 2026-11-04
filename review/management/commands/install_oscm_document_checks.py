"""Install the source-verified OSCM check profile locally, preserving configuration history."""
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from review.models import Venue, VenueAgentConfig
from review.services.oscm_checks import SOURCE, PROFILE

class Command(BaseCommand):
    help = 'Enable the verified OSCM initial-submission document checks on the local database.'

    def handle(self, *args, **options):
        if settings.DATABASES['default'].get('HOST') not in ('localhost','127.0.0.1','::1'):
            raise CommandError('This command is limited to the local database.')
        with transaction.atomic():
            venues = Venue.objects.select_for_update().filter(name='Operations and Supply Chain Management: An International Journal')
            if venues.count() != 1:
                raise CommandError('Expected exactly one OSCM venue; no changes made.')
            venue = venues.get()
            old = venue.agent_configs.filter(active=True).order_by('-version').first()
            if not old: raise CommandError('No active OSCM configuration.')
            if (old.policies or {}).get('document_check_profile') == PROFILE:
                self.stdout.write(f'Already enabled in configuration v{old.version}.')
                return
            fields = {f.name:getattr(old,f.name) for f in VenueAgentConfig._meta.concrete_fields
                      if f.name not in {'id','venue','version','created_at','effective_at','active'}}
            fields['policies'] = {**(old.policies or {}), 'document_check_profile':PROFILE,
                'document_check_source':SOURCE, 'document_check_verified_at':timezone.now().isoformat(),
                'manuscript_preparation':{'font':'Times New Roman, 12 point','line_spacing':'1.5','columns':'Single column at initial submission','margins':'2.54 cm on all sides','anonymization':'Remove identifying text and author file properties; supply a separate title page.'},
                'length_limits':{'manuscript':'8,000 words including abstract, tables, figures, references and appendices','abstract':'250 words maximum, one paragraph','keywords':'3–6'},
                'document_check_coverage':'Automated DOCX checks only; editorial quality, reference accuracy, image text and complete anonymization still require review.'}
            next_version = (venue.agent_configs.order_by('-version').values_list('version',flat=True).first() or 0)+1
            new = VenueAgentConfig.objects.create(venue=venue,version=next_version,active=True,**fields)
            venue.agent_configs.exclude(pk=new.pk).filter(active=True).update(active=False)
            venue.source_urls = list(dict.fromkeys([*(venue.source_urls or []),SOURCE]))
            venue.save(update_fields=['source_urls'])
            self.stdout.write(self.style.SUCCESS(f'OSCM document checks enabled in v{new.version}; prior versions preserved.'))
