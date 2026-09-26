import json
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from review.backup_utils import (
    BackupError,
    database_config_from_url,
    database_name,
    database_vendor,
    read_manifest,
    restore_postgres,
    smoke_test_postgres,
    stage_media_restore,
    swap_media_restore,
    utc_now,
    verify_bundle,
    verify_sqlite,
)
from review.models import AuditEvent


class Command(BaseCommand):
    help = (
        'Restore a verified Flexee backup into an explicitly named target database. '
        'The target must be confirmed to prevent accidental production overwrite.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--backup', required=True, help='Path to the completed backup bundle.')
        parser.add_argument(
            '--target-database-url',
            default=os.getenv('RESTORE_TARGET_DATABASE_URL', ''),
            help='Explicit PostgreSQL target. Never defaults to DATABASE_URL.',
        )
        parser.add_argument(
            '--target-sqlite-path',
            default='',
            help='Explicit SQLite target path for local restore rehearsal only.',
        )
        parser.add_argument(
            '--confirm-target-database',
            required=True,
            help='Must exactly match the PostgreSQL database name or SQLite target filename.',
        )
        parser.add_argument(
            '--allow-current-database',
            action='store_true',
            help='Required when restoring over the database used by this Django process.',
        )
        parser.add_argument(
            '--restore-media',
            action='store_true',
            help='Also restore the media archive.',
        )
        parser.add_argument(
            '--media-root',
            default=str(settings.MEDIA_ROOT),
            help='Media target used only with --restore-media.',
        )
        parser.add_argument(
            '--confirm-media-replace',
            default='',
            help='Must be REPLACE_MEDIA when --restore-media is used.',
        )
        parser.add_argument(
            '--pg-restore-bin',
            default=os.getenv('PG_RESTORE_BIN', 'pg_restore'),
        )

    def handle(self, *args, **options):
        bundle = Path(options['backup']).expanduser().resolve()
        try:
            manifest = verify_bundle(bundle, pg_restore_bin=options['pg_restore_bin'])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        backup_vendor = manifest['database']['vendor']
        staged_media = None
        previous_media = None

        if options['restore_media']:
            if options['confirm_media_replace'] != 'REPLACE_MEDIA':
                raise CommandError(
                    '--restore-media requires --confirm-media-replace REPLACE_MEDIA'
                )
            try:
                staged_media = stage_media_restore(
                    bundle / manifest['media']['file'],
                    Path(options['media_root']).expanduser(),
                )
            except BackupError as exc:
                raise CommandError(str(exc)) from exc

        try:
            if backup_vendor == 'postgresql':
                target_url = str(options['target_database_url'] or '').strip()
                if not target_url:
                    raise CommandError(
                        'PostgreSQL restore requires --target-database-url '
                        'or RESTORE_TARGET_DATABASE_URL.'
                    )
                target_config = database_config_from_url(target_url)
                if database_vendor(target_config) != 'postgresql':
                    raise CommandError('Target database URL must be PostgreSQL.')
                target_name = database_name(target_config)
                if options['confirm_target_database'] != target_name:
                    raise CommandError(
                        f'Confirmation mismatch. Expected --confirm-target-database {target_name}'
                    )

                current_config = settings.DATABASES['default']
                same_current = (
                    database_vendor(current_config) == 'postgresql'
                    and database_name(current_config) == target_name
                    and str(current_config.get('HOST') or '') == str(target_config.get('HOST') or '')
                    and str(current_config.get('PORT') or '') == str(target_config.get('PORT') or '')
                )
                if same_current and not options['allow_current_database']:
                    raise CommandError(
                        'Target matches the currently configured Django database. '
                        'Add --allow-current-database only during an intentional disaster-recovery restore.'
                    )

                restore_postgres(
                    bundle / manifest['database']['file'],
                    target_config,
                    pg_restore_bin=options['pg_restore_bin'],
                )
                smoke_test_postgres(target_config)
                target_description = f'postgresql:{target_name}'

            elif backup_vendor == 'sqlite':
                target_text = str(options['target_sqlite_path'] or '').strip()
                if not target_text:
                    raise CommandError(
                        'SQLite restore rehearsal requires --target-sqlite-path.'
                    )
                target = Path(target_text).expanduser().resolve()
                if options['confirm_target_database'] != target.name:
                    raise CommandError(
                        f'Confirmation mismatch. Expected --confirm-target-database {target.name}'
                    )

                current_name = Path(str(settings.DATABASES['default'].get('NAME') or '')).resolve()
                if target == current_name and not options['allow_current_database']:
                    raise CommandError(
                        'Target matches the SQLite database used by this Django process. '
                        'Restore to a separate test file, or add --allow-current-database only intentionally.'
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle / manifest['database']['file']
                temp_target = target.parent / f'.{target.name}.restore-partial'
                if temp_target.exists():
                    temp_target.unlink()
                shutil.copy2(source, temp_target)
                verify_sqlite(temp_target)

                previous_db = None
                if target.exists():
                    stamp = utc_now().strftime('%Y%m%dT%H%M%SZ')
                    previous_db = target.parent / f'{target.name}.pre-restore-{stamp}'
                    os.replace(target, previous_db)
                try:
                    os.replace(temp_target, target)
                except Exception:
                    if previous_db and previous_db.exists() and not target.exists():
                        os.replace(previous_db, target)
                    raise
                verify_sqlite(target)
                target_description = f'sqlite:{target}'
            else:
                raise CommandError(f'Unsupported backup database vendor: {backup_vendor}')

            if staged_media is not None:
                previous_media = swap_media_restore(
                    staged_media,
                    Path(options['media_root']).expanduser(),
                )
                staged_media = None

            detail = {
                'backup_bundle': bundle.name,
                'database_target': target_description,
                'media_restored': bool(options['restore_media']),
                'previous_media_path': str(previous_media) if previous_media else '',
                'verified_before_restore': True,
                'restored_at': utc_now().isoformat(),
            }
            try:
                AuditEvent.objects.create(
                    actor_role='system',
                    action='system.restore_completed',
                    resource_type='backup_bundle',
                    resource_id=bundle.name,
                    detail=detail,
                )
            except Exception:
                pass

            report_path = bundle / f"restore-report-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
            try:
                report_path.write_text(
                    json.dumps(detail, indent=2, sort_keys=True),
                    encoding='utf-8',
                )
            except OSError:
                report_path = None

            self.stdout.write(
                self.style.SUCCESS(
                    f'Restore completed and smoke-tested: {target_description}'
                )
            )
            if previous_media:
                self.stdout.write(
                    f'Previous media preserved at: {previous_media}'
                )
            if report_path:
                self.stdout.write(f'Restore report: {report_path}')

        except Exception as exc:
            if staged_media is not None:
                shutil.rmtree(staged_media, ignore_errors=True)
            try:
                AuditEvent.objects.create(
                    actor_role='system',
                    action='system.restore_failed',
                    resource_type='backup_bundle',
                    resource_id=bundle.name,
                    detail={'error': str(exc)[:1200]},
                )
            except Exception:
                pass
            if isinstance(exc, CommandError):
                raise
            if isinstance(exc, BackupError):
                raise CommandError(str(exc)) from exc
            raise
