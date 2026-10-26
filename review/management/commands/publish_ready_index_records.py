"""Publish every venue index record whose rules are read and approved-ready.

Approving one journal at a time is fine for a handful. The ABDC import put 1,300 journals in
the rules queue, and a run that reads forty at a time produces more ready records than anyone
will click through. This applies exactly the gates the Publish button applies, one record at a
time, and reports every skip with its reason.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from review.discovery_api import AddToVenueError
from review.index_api import publish_blocker, publish_index_record
from review.models import EditorUser, IndexedVenue


class _CommandActor:
    """Stands in for an HttpRequest so the shared publish path can record audit events.

    The audit log snapshots who acted, so a bulk publish is attributed to a real platform
    superuser rather than appearing to come from nobody.
    """

    def __init__(self, editor_user):
        self.editor_user = editor_user
        self.META = {'REMOTE_ADDR': 'management-command'}


class Command(BaseCommand):
    help = ('Publish every index record whose rules are ready, using the same checks as the '
            'Publish button. --dry-run lists what would be published and changes nothing.')

    def add_arguments(self, parser):
        parser.add_argument('--actor', default='',
                            help='Email of the platform superuser to record in the audit log. '
                                 'Defaults to the only platform superuser when there is exactly one.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Publish at most this many records (default: all that are ready).')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be published and why anything is skipped. Writes nothing.')

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

    def handle(self, *args, **options):
        actor = self._actor(options['actor'])
        request = _CommandActor(actor)
        candidates = (IndexedVenue.objects.select_related('discovered', 'venue')
                      .filter(rules_status='ready', venue__isnull=True)
                      .filter(Q(excluded=False))
                      .order_by('title'))
        total = candidates.count()
        if not total:
            self.stdout.write('No index record is waiting to be published.')
            return

        published, skipped, failed = 0, 0, 0
        limit = max(0, options['limit'])
        for item in candidates:
            if limit and published >= limit:
                break
            blocker = publish_blocker(item)
            if blocker is not None:
                skipped += 1
                self.stdout.write(f'  skipped  {item.title[:60]} — {blocker[0]}')
                continue
            if options['dry_run']:
                published += 1
                self.stdout.write(f'  would publish  {item.title[:60]}')
                continue
            try:
                item = publish_index_record(request, item)
            except AddToVenueError as exc:
                failed += 1
                self.stdout.write(f'  failed   {item.title[:60]} — {exc.detail}')
                continue
            published += 1
            self.stdout.write(f'  published  {item.venue.name[:60]}')

        verb = 'Would publish' if options['dry_run'] else 'Published'
        self.stdout.write(f'{verb} {published} of {total} ready records; {skipped} skipped, {failed} failed.')
        if options['dry_run']:
            self.stdout.write('Nothing was changed. Run again without --dry-run to publish.')
        elif published:
            self.stdout.write('Each published journal is now live for authors, labelled '
                              '"Checked from official pages".')
