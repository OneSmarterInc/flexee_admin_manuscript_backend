from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dj_database_url


BACKUP_FORMAT_VERSION = 1
BACKUP_PREFIX = 'flexee-backup-'


class BackupError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def append_attempt_log(backup_root: Path, payload: dict):
    backup_root.mkdir(parents=True, exist_ok=True)
    path = backup_root / 'backup_attempts.jsonl'
    row = {'recorded_at': utc_now().isoformat(), **payload}
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n')


def database_config_from_url(url: str) -> dict:
    if not str(url or '').strip():
        raise BackupError('A database URL is required.')
    config = dj_database_url.parse(url, conn_max_age=0)
    if not config.get('ENGINE'):
        raise BackupError('Could not parse the target database URL.')
    return config


def database_name(config: dict) -> str:
    return str(config.get('NAME') or '').strip()


def database_vendor(config: dict) -> str:
    engine = str(config.get('ENGINE') or '').lower()
    if 'postgresql' in engine or 'postgres' in engine:
        return 'postgresql'
    if 'sqlite' in engine:
        return 'sqlite'
    return engine.rsplit('.', 1)[-1] or 'unknown'


def _postgres_env(config: dict) -> dict:
    env = os.environ.copy()
    mapping = {
        'PGHOST': config.get('HOST'),
        'PGPORT': config.get('PORT'),
        'PGUSER': config.get('USER'),
        'PGPASSWORD': config.get('PASSWORD'),
        'PGDATABASE': config.get('NAME'),
    }
    for key, value in mapping.items():
        if value not in (None, ''):
            env[key] = str(value)
    options = config.get('OPTIONS') or {}
    sslmode = options.get('sslmode')
    if sslmode:
        env['PGSSLMODE'] = str(sslmode)
    return env


def run_command(args, *, env=None, timeout=3600):
    try:
        return subprocess.run(
            [str(item) for item in args],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BackupError(f'Required executable was not found: {args[0]}') from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f'Command timed out: {args[0]}') from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or '').strip()
        raise BackupError(f'{args[0]} failed: {stderr[-1200:]}') from exc


def dump_postgres(config: dict, output_path: Path, *, pg_dump_bin='pg_dump'):
    args = [
        pg_dump_bin,
        '--format=custom',
        '--no-owner',
        '--no-privileges',
        '--file',
        str(output_path),
    ]
    run_command(args, env=_postgres_env(config))


def verify_postgres_dump(dump_path: Path, *, pg_restore_bin='pg_restore'):
    result = run_command([pg_restore_bin, '--list', str(dump_path)])
    if not result.stdout.strip():
        raise BackupError('pg_restore --list returned no archive contents.')


def backup_sqlite(source_path: Path, output_path: Path):
    if not source_path.exists():
        raise BackupError(f'SQLite database does not exist: {source_path}')
    source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
    target = sqlite3.connect(output_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def verify_sqlite(path: Path):
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = connection.execute('PRAGMA integrity_check').fetchone()
    finally:
        connection.close()
    if not row or str(row[0]).lower() != 'ok':
        raise BackupError(f'SQLite integrity check failed for {path}')


def archive_media(media_root: Path, output_path: Path):
    media_root = media_root.resolve()
    file_count = 0
    total_bytes = 0
    with tarfile.open(output_path, 'w:gz') as archive:
        if media_root.exists():
            for path in sorted(media_root.rglob('*')):
                if not path.is_file():
                    continue
                relative = path.relative_to(media_root)
                archive.add(path, arcname=str(relative), recursive=False)
                file_count += 1
                total_bytes += path.stat().st_size
    return file_count, total_bytes


def _safe_tar_members(archive: tarfile.TarFile):
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or '..' in path.parts:
            raise BackupError(f'Unsafe media archive member: {member.name}')
        if member.issym() or member.islnk():
            raise BackupError(f'Symbolic/hard links are not allowed in media backups: {member.name}')
    return members


def verify_media_archive(path: Path):
    try:
        with tarfile.open(path, 'r:gz') as archive:
            members = _safe_tar_members(archive)
            for member in members:
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise BackupError(f'Cannot read archived media member: {member.name}')
                    while extracted.read(1024 * 1024):
                        pass
    except tarfile.TarError as exc:
        raise BackupError(f'Invalid media archive: {exc}') from exc


def extract_media_archive(path: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, 'r:gz') as archive:
        members = _safe_tar_members(archive)
        for member in members:
            archive.extract(member, path=destination)


def write_manifest(bundle_dir: Path, manifest: dict):
    path = bundle_dir / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    return path


def read_manifest(bundle_dir: Path) -> dict:
    path = bundle_dir / 'manifest.json'
    if not path.is_file():
        raise BackupError(f'Backup manifest not found: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f'Backup manifest is invalid: {exc}') from exc
    if data.get('format_version') != BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"Unsupported backup format version: {data.get('format_version')!r}"
        )
    return data


def verify_bundle(bundle_dir: Path, *, pg_restore_bin='pg_restore') -> dict:
    bundle_dir = bundle_dir.resolve()
    manifest = read_manifest(bundle_dir)
    for section in ('database', 'media'):
        entry = manifest.get(section) or {}
        filename = str(entry.get('file') or '').strip()
        expected_sha = str(entry.get('sha256') or '').strip()
        if not filename or not expected_sha:
            raise BackupError(f'Manifest {section} section is incomplete.')
        path = bundle_dir / filename
        if not path.is_file():
            raise BackupError(f'Backup file is missing: {path}')
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise BackupError(f'Checksum mismatch for {filename}.')

    db_entry = manifest['database']
    db_path = bundle_dir / db_entry['file']
    if db_entry.get('vendor') == 'postgresql':
        verify_postgres_dump(db_path, pg_restore_bin=pg_restore_bin)
    elif db_entry.get('vendor') == 'sqlite':
        verify_sqlite(db_path)
    else:
        raise BackupError(f"Unsupported database vendor: {db_entry.get('vendor')}")

    verify_media_archive(bundle_dir / manifest['media']['file'])
    return manifest


def purge_old_backups(backup_root: Path, *, retention_days: int, keep: Path | None = None):
    if retention_days < 1:
        raise BackupError('Backup retention days must be at least 1.')
    cutoff = utc_now() - timedelta(days=retention_days)
    removed = []
    backup_root = backup_root.resolve()
    keep = keep.resolve() if keep else None
    for path in backup_root.iterdir() if backup_root.exists() else []:
        if not path.is_dir() or not path.name.startswith(BACKUP_PREFIX):
            continue
        if keep and path.resolve() == keep:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified >= cutoff:
            continue
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def create_backup(
    *,
    db_config: dict,
    media_root: Path,
    backup_root: Path,
    retention_days: int = 30,
    pg_dump_bin='pg_dump',
    pg_restore_bin='pg_restore',
    verify=True,
    allow_sqlite=False,
    release_sha='',
) -> tuple[Path, dict]:
    backup_root = backup_root.resolve()
    media_root = media_root.resolve()
    try:
        backup_root.relative_to(media_root)
        raise BackupError('BACKUP_ROOT must not be inside MEDIA_ROOT.')
    except ValueError:
        pass
    backup_root.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    suffix = uuid.uuid4().hex[:8]
    final_name = f'{BACKUP_PREFIX}{now.strftime("%Y%m%dT%H%M%SZ")}-{suffix}'
    temp_dir = backup_root / f'.{final_name}.partial'
    final_dir = backup_root / final_name
    if temp_dir.exists() or final_dir.exists():
        raise BackupError('Backup destination collision. Retry the operation.')
    temp_dir.mkdir(parents=True)

    vendor = database_vendor(db_config)
    try:
        if vendor == 'postgresql':
            db_file = temp_dir / 'database.dump'
            dump_postgres(db_config, db_file, pg_dump_bin=pg_dump_bin)
        elif vendor == 'sqlite' and allow_sqlite:
            db_file = temp_dir / 'database.sqlite3'
            backup_sqlite(Path(db_config['NAME']), db_file)
        elif vendor == 'sqlite':
            raise BackupError(
                'Production backups require PostgreSQL. '
                'Use --allow-sqlite only for local backup workflow testing.'
            )
        else:
            raise BackupError(f'Unsupported database vendor: {vendor}')

        media_file = temp_dir / 'media.tar.gz'
        file_count, source_bytes = archive_media(media_root, media_file)

        manifest = {
            'format_version': BACKUP_FORMAT_VERSION,
            'application': 'flexee_admin_manuscript_backend',
            'created_at': now.isoformat(),
            'release_sha': str(release_sha or '').strip(),
            'database': {
                'vendor': vendor,
                'name': database_name(db_config),
                'file': db_file.name,
                'bytes': db_file.stat().st_size,
                'sha256': sha256_file(db_file),
            },
            'media': {
                'file': media_file.name,
                'bytes': media_file.stat().st_size,
                'sha256': sha256_file(media_file),
                'source_file_count': file_count,
                'source_bytes': source_bytes,
            },
            'retention_days': retention_days,
            'verified': False,
        }
        write_manifest(temp_dir, manifest)

        if verify:
            verify_bundle(temp_dir, pg_restore_bin=pg_restore_bin)
            manifest['verified'] = True
            manifest['verified_at'] = utc_now().isoformat()
            write_manifest(temp_dir, manifest)

        os.replace(temp_dir, final_dir)
        removed = purge_old_backups(
            backup_root,
            retention_days=retention_days,
            keep=final_dir,
        )
        manifest['expired_backups_removed'] = removed
        write_manifest(final_dir, manifest)
        return final_dir, manifest
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def restore_postgres(
    dump_path: Path,
    target_config: dict,
    *,
    pg_restore_bin='pg_restore',
):
    if database_vendor(target_config) != 'postgresql':
        raise BackupError('PostgreSQL backup can only be restored to PostgreSQL.')
    target_name = database_name(target_config)
    if not target_name:
        raise BackupError('Target PostgreSQL database name is empty.')
    args = [
        pg_restore_bin,
        '--clean',
        '--if-exists',
        '--no-owner',
        '--no-privileges',
        '--exit-on-error',
        '--dbname',
        target_name,
        str(dump_path),
    ]
    run_command(args, env=_postgres_env(target_config), timeout=7200)


def smoke_test_postgres(config: dict):
    if database_vendor(config) != 'postgresql':
        raise BackupError('PostgreSQL smoke test requires a PostgreSQL target.')
    try:
        import psycopg2
        connection = psycopg2.connect(
            dbname=config.get('NAME'),
            user=config.get('USER') or None,
            password=config.get('PASSWORD') or None,
            host=config.get('HOST') or None,
            port=config.get('PORT') or None,
            connect_timeout=10,
            **({'sslmode': (config.get('OPTIONS') or {}).get('sslmode')}
               if (config.get('OPTIONS') or {}).get('sslmode') else {}),
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                if cursor.fetchone()[0] != 1:
                    raise BackupError('PostgreSQL restore smoke test SELECT 1 failed.')
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'django_migrations'"
                )
                if cursor.fetchone()[0] != 1:
                    raise BackupError('Restored database is missing django_migrations.')
        finally:
            connection.close()
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f'Restored PostgreSQL smoke test failed: {exc}') from exc


def stage_media_restore(archive_path: Path, target_root: Path) -> Path:
    verify_media_archive(archive_path)
    parent = target_root.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.flexee-media-restore-', dir=parent))
    try:
        extract_media_archive(archive_path, stage)
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def swap_media_restore(staged_root: Path, target_root: Path) -> Path | None:
    target_root = target_root.resolve()
    staged_root = staged_root.resolve()
    parent = target_root.parent
    previous = None
    if target_root.exists():
        stamp = utc_now().strftime('%Y%m%dT%H%M%SZ')
        previous = parent / f'{target_root.name}.pre-restore-{stamp}'
        if previous.exists():
            raise BackupError(f'Pre-restore media path already exists: {previous}')
        os.replace(target_root, previous)
    try:
        os.replace(staged_root, target_root)
    except Exception:
        if previous and previous.exists() and not target_root.exists():
            os.replace(previous, target_root)
        raise
    return previous
