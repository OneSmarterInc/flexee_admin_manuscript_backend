"""Load submission rules a person read and checked, for journals the crawler cannot reach.

The index reads rules automatically and validates every one against a quote on the page it came
from. That works on open-access and society journals and fails on Elsevier, SAGE, Wiley, Taylor
and Francis and INFORMS, which refuse our crawler, and on pages the local model cannot parse. The
ABDC A* band is mostly those: of 2,648 imported ABDC journals, 2 carry rules.

This loads rules a person has read from the journal's own guidelines page and recorded, with the
URL they read. It is the manual path, and it is deliberately separate from the automated one: no
quote validation happens here, because the evidence is a person rather than a matched string, and
the URL they used is stored on the venue so anyone can check them.

Matches an existing index record by ISSN so the journal keeps its catalogue facts and its ABDC
rating. Never overwrites a venue that already exists, so re-running is safe.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from review.author_api import build_venue_config_fields, unique_venue_slug
from review.models import EditorUser, IndexedVenue, Organization, Venue, VenueAgentConfig

CONFIG_KEYS = ('aims_scope', 'article_types', 'accepted_methods', 'quality_threshold', 'reviewer_criteria',
               'policies', 'disclosures', 'reporting_standards', 'desk_rejection_rules',
               'structured_desk_rejection_rules', 'required_submission_items', 'config_notes')


class Command(BaseCommand):
    help = ('Load human-checked submission rules from a JSON file and publish each journal as a live '
            'venue. --dry-run reports what would happen and writes nothing.')

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='JSON file: a list of journal rule objects.')
        parser.add_argument('--actor', default='', help='Platform superuser email recorded as the approver.')
        parser.add_argument('--dry-run', action='store_true', help='Validate and report; write nothing.')

    def _actor(self, email):
        superusers = EditorUser.objects.filter(platform_superuser=True)
        if email:
            user = superusers.filter(email__iexact=email.strip()).first()
            if user is None:
                raise CommandError(f'{email} is not a platform superuser.')
            return user
        found = list(superusers[:2])
        if not found:
            raise CommandError('No platform superuser exists to attribute this to.')
        if len(found) > 1:
            raise CommandError('More than one platform superuser exists: pass --actor <email>.')
        return found[0]

    def _issn_index(self):
        """Every ISSN in the index, mapped to its record id.

        Built once in Python rather than queried per entry. A JSON "contains" lookup is not
        supported on SQLite, and the index is small enough that one pass is cheaper than one query
        per journal anyway.
        """
        index = {}
        for pk, issn_l, issns in IndexedVenue.objects.values_list('id', 'issn_l', 'issns'):
            for value in [issn_l, *(issns or [])]:
                key = str(value or '').strip()
                if key:
                    index.setdefault(key, pk)
        return index

    def _record_for(self, entry, issn_index):
        """The index record this entry describes, matched on ISSN then on exact title."""
        for value in (entry.get('issn'), entry.get('issn_online')):
            pk = issn_index.get(str(value or '').strip())
            if pk is not None:
                return IndexedVenue.objects.select_related('venue').filter(id=pk).first()
        title = str(entry.get('title') or '').strip()
        return IndexedVenue.objects.select_related('venue').filter(title__iexact=title).first() if title else None

    def handle(self, *args, **options):
        path = Path(options['file']).expanduser()
        if not path.is_file():
            raise CommandError(f'Cannot find file: {path}')
        try:
            entries = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise CommandError(f'Could not read {path}: {exc}')
        if not isinstance(entries, list) or not entries:
            raise CommandError('The file must contain a non-empty list of journal rule objects.')
        actor = self._actor(options['actor'])
        issn_index = self._issn_index()

        # Validate every entry before writing any of them, so a bad file changes nothing.
        prepared, skipped = [], []
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise CommandError(f'Entry {index} is not an object.')
            title = str(entry.get('title') or '').strip()
            url = str(entry.get('guidelines_url') or '').strip()
            if not title:
                raise CommandError(f'Entry {index} has no title.')
            if not url.startswith(('http://', 'https://')):
                raise CommandError(f'{title}: guidelines_url must be the page the rules were read from.')
            try:
                fields = build_venue_config_fields({k: entry.get(k) for k in CONFIG_KEYS})
            except (ValueError, TypeError) as exc:
                raise CommandError(f'{title}: {exc}')
            if not (fields['article_types'] or fields['required_submission_items']
                    or fields['structured_desk_rejection_rules'] or fields['aims_scope']):
                raise CommandError(f'{title}: no rules in this entry, so there is nothing to load.')
            record = self._record_for(entry, issn_index)
            if record is None:
                skipped.append((title, 'no index record matches its ISSN or title'))
                continue
            if record.venue_id:
                skipped.append((title, f'already live as "{record.venue.name}"'))
                continue
            prepared.append((entry, record, fields, url))

        for title, reason in skipped:
            self.stdout.write(f'  skipped   {title[:52]} — {reason}')
        if options['dry_run']:
            for entry, record, fields, url in prepared:
                self.stdout.write(f'  would load  {record.title[:52]} — {len(fields["article_types"])} article types, '
                                  f'{len(fields["required_submission_items"])} required items')
            self.stdout.write(f'\n{len(prepared)} would load, {len(skipped)} skipped. Nothing was written.')
            return

        loaded = 0
        for entry, record, fields, url in prepared:
            with transaction.atomic():
                organization, _created = Organization.objects.get_or_create(
                    name=(str(entry.get('publisher') or '').strip() or record.publisher or record.title)[:200])
                venue = Venue.objects.create(
                    organization=organization,
                    name=record.title[:300],
                    slug=unique_venue_slug(record.title),
                    venue_type='journal',
                    active=True,
                    trust_tier=Venue.TIER_VERIFIED_INDEX,
                    last_verified_at=timezone.now(),
                    source_urls=[url],
                )
                VenueAgentConfig.objects.create(venue=venue, version=1, active=True, **fields)
                record.venue = venue
                record.rules_status = 'ready'
                record.rules_read_at = timezone.now()
                record.rules_error = ''
                record.save(update_fields=['venue', 'rules_status', 'rules_read_at', 'rules_error', 'updated_at'])
            loaded += 1
            self.stdout.write(f'  loaded    {venue.name[:52]}')

        self.stdout.write(f'\nLoaded {loaded} journals, skipped {len(skipped)}. Approved by {actor.email}.')
        self.stdout.write('Each is live for authors with the guidelines URL it was read from on the venue.')
