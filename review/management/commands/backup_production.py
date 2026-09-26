import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from review.backup_utils import (
    BackupError,
    append_attempt_log,
    create_backup,
    database_vendor,
)
from review.models import AuditEvent


class Command(BaseCommand):
    help = 'Create a verified database + media production backup bundle.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--backup-root',
            default=(os.getenv('BACKUP_ROOT', '').strip() or str(settings.BASE_DIR / 'backups')),
            help='Directory where backup bundles are stored.',
        )
        parser.add_argument(
            '--retention-days',
            type=int,
            default=int(os.getenv('BACKUP_RETENTION_DAYS', '30')),
            help='Delete completed backup bundles older than this many days after a successful backup.',
        )
        parser.add_argument(
            '--pg-dump-bin',
            default=(os.getenv('PG_DUMP_BIN', '').strip() or 'pg_dump'),
        )
        parser.add_argument(
            '--pg-restore-bin',
            default=(os.getenv('PG_RESTORE_BIN', '').strip() or 'pg_restore'),
        )
        parser.add_argument(
            '--no-verify',
            action='store_true',
            help='Skip archive verification. Not recommended for production.',
        )
        parser.add_argument(
            '--allow-sqlite',
            action='store_true',
            help='Allow SQLite for local rehearsal only. Production backups should use PostgreSQL.',
        )

    def handle(self, *args, **options):
        backup_root = Path(options['backup_root']).expanduser()
        db_config = settings.DATABASES['default']
        vendor = database_vendor(db_config)
        if settings.PRODUCTION and vendor != 'postgresql':
            raise CommandError('Production backup requires PostgreSQL.')

        try:
            bundle, manifest = create_backup(
                db_config=db_config,
                media_root=Path(settings.MEDIA_ROOT),
                backup_root=backup_root,
                retention_days=options['retention_days'],
                pg_dump_bin=options['pg_dump_bin'],
                pg_restore_bin=options['pg_restore_bin'],
                verify=not options['no_verify'],
                allow_sqlite=options['allow_sqlite'],
                release_sha=os.getenv('RELEASE_SHA', ''),
            )
            append_attempt_log(
                backup_root,
                {
                    'status': 'success',
                    'bundle': bundle.name,
                    'database_vendor': manifest['database']['vendor'],
                    'verified': manifest.get('verified', False),
                    'database_bytes': manifest['database']['bytes'],
                    'media_bytes': manifest['media']['bytes'],
                    'media_file_count': manifest['media']['source_file_count'],
                },
            )
            try:
                AuditEvent.objects.create(
                    actor_role='system',
                    action='system.backup_completed',
                    resource_type='backup_bundle',
                    resource_id=bundle.name,
                    detail={
                        'database_vendor': manifest['database']['vendor'],
                        'verified': manifest.get('verified', False),
                        'database_bytes': manifest['database']['bytes'],
                        'media_bytes': manifest['media']['bytes'],
                        'media_file_count': manifest['media']['source_file_count'],
                    },
                )
            except Exception:
                pass

            self.stdout.write(self.style.SUCCESS(f'Backup completed: {bundle}'))
            self.stdout.write(
                self.style.SUCCESS(
                    f"Verified={manifest.get('verified', False)} "
                    f"DB={manifest['database']['bytes']} bytes "
                    f"Media={manifest['media']['source_file_count']} files"
                )
            )
        except Exception as exc:
            try:
                append_attempt_log(
                    backup_root,
                    {
                        'status': 'failed',
                        'database_vendor': vendor,
                        'error': str(exc)[:1200],
                    },
                )
            except Exception:
                pass
            try:
                AuditEvent.objects.create(
                    actor_role='system',
                    action='system.backup_failed',
                    resource_type='backup',
                    detail={'database_vendor': vendor, 'error': str(exc)[:1200]},
                )
            except Exception:
                pass

            if isinstance(exc, BackupError):
                raise CommandError(str(exc)) from exc
            raise
