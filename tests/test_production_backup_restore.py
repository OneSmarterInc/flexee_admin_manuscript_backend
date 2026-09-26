import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from review.backup_utils import (
    BACKUP_PREFIX,
    BackupError,
    create_backup,
    dump_postgres,
    purge_old_backups,
    restore_postgres,
    verify_bundle,
    verify_media_archive,
)


def make_sqlite(path: Path):
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO sample(value) VALUES (?)', ('backup-value',))
        connection.commit()
    finally:
        connection.close()


def sqlite_config(path: Path):
    return {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(path),
    }


def test_sqlite_backup_bundle_contains_database_media_and_manifest(tmp_path):
    source_db = tmp_path / 'source.sqlite3'
    make_sqlite(source_db)
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'paper.md').write_text('# Manuscript', encoding='utf-8')
    nested = media / 'venue_requirement_files'
    nested.mkdir()
    (nested / 'cover.pdf').write_bytes(b'%PDF test')
    root = tmp_path / 'backups'

    bundle, manifest = create_backup(
        db_config=sqlite_config(source_db),
        media_root=media,
        backup_root=root,
        retention_days=30,
        verify=True,
        allow_sqlite=True,
    )

    assert bundle.name.startswith(BACKUP_PREFIX)
    assert manifest['verified'] is True
    assert manifest['database']['vendor'] == 'sqlite'
    assert manifest['media']['source_file_count'] == 2
    assert (bundle / 'database.sqlite3').is_file()
    assert (bundle / 'media.tar.gz').is_file()
    assert (bundle / 'manifest.json').is_file()

    verified = verify_bundle(bundle)
    assert verified['database']['sha256'] == manifest['database']['sha256']

    restored = sqlite3.connect(bundle / 'database.sqlite3')
    try:
        assert restored.execute('SELECT value FROM sample').fetchone()[0] == 'backup-value'
    finally:
        restored.close()


def test_verify_bundle_detects_tampered_media(tmp_path):
    source_db = tmp_path / 'source.sqlite3'
    make_sqlite(source_db)
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'file.txt').write_text('original', encoding='utf-8')

    bundle, _ = create_backup(
        db_config=sqlite_config(source_db),
        media_root=media,
        backup_root=tmp_path / 'backups',
        verify=True,
        allow_sqlite=True,
    )
    with (bundle / 'media.tar.gz').open('ab') as handle:
        handle.write(b'tampered')

    with pytest.raises(BackupError, match='Checksum mismatch'):
        verify_bundle(bundle)


def test_media_verification_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        payload = b'evil'
        member = tarfile.TarInfo('../outside.txt')
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(BackupError, match='Unsafe media archive member'):
        verify_media_archive(archive_path)


def test_postgres_password_is_not_put_on_command_line(tmp_path):
    config = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'flexee_prod',
        'HOST': 'db.internal',
        'PORT': '5432',
        'USER': 'flexee',
        'PASSWORD': 'super-secret-password',
        'OPTIONS': {'sslmode': 'require'},
    }
    captured = {}

    def fake_run(args, *, env=None, timeout=3600):
        captured['args'] = list(args)
        captured['env'] = dict(env or {})

        class Result:
            stdout = 'archive'
            stderr = ''

        Path(args[args.index('--file') + 1]).write_bytes(b'dump')
        return Result()

    with patch('review.backup_utils.run_command', fake_run):
        dump_postgres(config, tmp_path / 'database.dump')

    joined = ' '.join(str(item) for item in captured['args'])
    assert 'super-secret-password' not in joined
    assert captured['env']['PGPASSWORD'] == 'super-secret-password'
    assert captured['env']['PGSSLMODE'] == 'require'


def test_postgres_restore_is_guarded_and_password_stays_in_environment(tmp_path):
    config = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'flexee_restore_test',
        'HOST': '127.0.0.1',
        'PORT': '5432',
        'USER': 'flexee',
        'PASSWORD': 'restore-secret',
        'OPTIONS': {},
    }
    dump = tmp_path / 'database.dump'
    dump.write_bytes(b'fake')
    captured = {}

    def fake_run(args, *, env=None, timeout=3600):
        captured['args'] = list(args)
        captured['env'] = dict(env or {})

        class Result:
            stdout = ''
            stderr = ''

        return Result()

    with patch('review.backup_utils.run_command', fake_run):
        restore_postgres(dump, config)

    assert '--clean' in captured['args']
    assert '--if-exists' in captured['args']
    assert '--exit-on-error' in captured['args']
    assert captured['args'][captured['args'].index('--dbname') + 1] == 'flexee_restore_test'
    assert 'restore-secret' not in ' '.join(captured['args'])
    assert captured['env']['PGPASSWORD'] == 'restore-secret'


def test_old_backup_retention_removes_only_expired_bundle_directories(tmp_path, monkeypatch):
    root = tmp_path / 'backups'
    root.mkdir()
    old = root / f'{BACKUP_PREFIX}20200101T000000Z-old'
    old.mkdir()
    unrelated = root / 'keep-me'
    unrelated.mkdir()

    old_time = 1_600_000_000
    os.utime(old, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    removed = purge_old_backups(root, retention_days=1)

    assert old.name in removed
    assert not old.exists()
    assert unrelated.exists()


def test_restore_command_performs_real_sqlite_restore_and_media_swap(tmp_path):
    source_db = tmp_path / 'source.sqlite3'
    make_sqlite(source_db)
    source_media = tmp_path / 'source-media'
    source_media.mkdir()
    (source_media / 'manuscript.md').write_text('restored manuscript', encoding='utf-8')

    bundle, _ = create_backup(
        db_config=sqlite_config(source_db),
        media_root=source_media,
        backup_root=tmp_path / 'backups',
        verify=True,
        allow_sqlite=True,
    )

    target_db = tmp_path / 'restore-test.sqlite3'
    target_media = tmp_path / 'restore-media'
    target_media.mkdir()
    (target_media / 'old.txt').write_text('old content', encoding='utf-8')

    call_command(
        'restore_production_backup',
        backup=str(bundle),
        target_sqlite_path=str(target_db),
        confirm_target_database=target_db.name,
        restore_media=True,
        media_root=str(target_media),
        confirm_media_replace='REPLACE_MEDIA',
    )

    connection = sqlite3.connect(target_db)
    try:
        assert connection.execute('SELECT value FROM sample').fetchone()[0] == 'backup-value'
    finally:
        connection.close()

    assert (target_media / 'manuscript.md').read_text(encoding='utf-8') == 'restored manuscript'
    assert not (target_media / 'old.txt').exists()
    preserved = list(tmp_path.glob('restore-media.pre-restore-*'))
    assert len(preserved) == 1
    assert (preserved[0] / 'old.txt').read_text(encoding='utf-8') == 'old content'


def test_backup_root_inside_media_root_is_rejected(tmp_path):
    source_db = tmp_path / 'source.sqlite3'
    make_sqlite(source_db)
    media = tmp_path / 'media'
    media.mkdir()

    with pytest.raises(BackupError, match='must not be inside MEDIA_ROOT'):
        create_backup(
            db_config=sqlite_config(source_db),
            media_root=media,
            backup_root=media / 'backups',
            verify=True,
            allow_sqlite=True,
        )
