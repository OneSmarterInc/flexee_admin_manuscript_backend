import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from review.backup_utils import BackupError, verify_bundle


class Command(BaseCommand):
    help = 'Verify a Flexee database + media backup bundle without restoring it.'

    def add_arguments(self, parser):
        parser.add_argument('--backup', required=True, help='Path to a completed backup bundle directory.')
        parser.add_argument(
            '--pg-restore-bin',
            default=(os.getenv('PG_RESTORE_BIN', '').strip() or 'pg_restore'),
        )

    def handle(self, *args, **options):
        bundle = Path(options['backup']).expanduser()
        try:
            manifest = verify_bundle(bundle, pg_restore_bin=options['pg_restore_bin'])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Backup verified: {bundle.resolve()} "
                f"({manifest['database']['vendor']}, "
                f"{manifest['media']['source_file_count']} media files)"
            )
        )
