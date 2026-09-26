from __future__ import annotations

import os
import re
import stat
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from django.http import FileResponse


MANUSCRIPT_EXTENSIONS = {'.docx', '.pdf', '.md', '.zip'}
ZIP_MANUSCRIPT_EXTENSIONS = {'.docx', '.pdf', '.md'}


class UploadSecurityError(ValueError):
    pass


def _env_int(names, default, *, minimum=1):
    for name in names:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == '':
            continue
        try:
            return max(minimum, int(raw))
        except (TypeError, ValueError):
            continue
    return max(minimum, int(default))


def sanitize_original_filename(value, *, default='upload', max_length=240):
    """Keep a display/download filename without retaining path or control data."""
    text = str(value or '').replace('\\', '/')
    name = text.rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]+', '_', name).strip().strip('.')
    if not name:
        name = default
    if len(name) > max_length:
        root, ext = os.path.splitext(name)
        keep = max(1, max_length - len(ext))
        name = root[:keep] + ext[:max_length]
    return name


def validate_manuscript_filename(value):
    filename = sanitize_original_filename(value, default='manuscript')
    extension = os.path.splitext(filename)[1].lower()
    if extension not in MANUSCRIPT_EXTENSIONS:
        raise UploadSecurityError(
            'manuscript must be a .docx, .pdf, .md, or .zip file'
        )
    return filename


def _safe_zip_member_name(name):
    raw = str(name or '')
    if '\x00' in raw:
        return False
    normalized = raw.replace('\\', '/')
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return False
    if any(part in {'..', ''} for part in path.parts):
        return False
    if re.match(r'^[A-Za-z]:', normalized):
        return False
    return True


def validate_manuscript_zip(content: bytes):
    """
    Validate a manuscript ZIP before it is persisted or processed.

    ZIP members are read in-memory by the application rather than extracted to
    disk, but traversal/symlink/special-file names are rejected anyway so future
    storage changes cannot turn an inert archive into a filesystem escape.
    """
    max_files = _env_int(
        ('MANUSCRIPT_ZIP_MAX_FILES', 'AUTHOR_ZIP_MAX_FILES', 'ZIP_MAX_FILES'),
        40,
    )
    max_uncompressed = _env_int(
        (
            'MANUSCRIPT_ZIP_MAX_UNCOMPRESSED_BYTES',
            'AUTHOR_ZIP_MAX_UNCOMPRESSED_BYTES',
            'ZIP_MAX_EXTRACTED_BYTES',
        ),
        50 * 1024 * 1024,
    )
    max_ratio = _env_int(('MANUSCRIPT_ZIP_MAX_COMPRESSION_RATIO',), 200)

    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UploadSecurityError('The uploaded file is not a valid ZIP archive') from exc

    supported = 0
    total_uncompressed = 0
    with archive:
        entries = archive.infolist()
        if len(entries) > max_files:
            raise UploadSecurityError(
                f'ZIP contains too many entries; limit is {max_files}'
            )

        for info in entries:
            if not _safe_zip_member_name(info.filename):
                raise UploadSecurityError('ZIP contains an unsafe member path')

            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode not in {stat.S_IFREG, stat.S_IFDIR}:
                raise UploadSecurityError(
                    'ZIP contains a symlink or unsupported special-file entry'
                )
            if info.flag_bits & 0x1:
                raise UploadSecurityError('Encrypted ZIP entries are not supported')
            if info.is_dir():
                continue

            total_uncompressed += max(0, int(info.file_size or 0))
            if total_uncompressed > max_uncompressed:
                raise UploadSecurityError(
                    'Extracted ZIP size exceeds the configured limit'
                )

            compressed = max(0, int(info.compress_size or 0))
            uncompressed = max(0, int(info.file_size or 0))
            if uncompressed >= 1024 * 1024:
                if compressed == 0 or (uncompressed / max(1, compressed)) > max_ratio:
                    raise UploadSecurityError(
                        'ZIP member compression ratio exceeds the configured safety limit'
                    )

            ext = os.path.splitext(info.filename)[1].lower()
            if ext in ZIP_MANUSCRIPT_EXTENSIONS:
                supported += 1

        if supported == 0:
            raise UploadSecurityError(
                'No .docx, .pdf, or .md manuscript file was found inside the ZIP archive'
            )

    return {
        'entries': len(entries),
        'supported_manuscript_files': supported,
        'uncompressed_bytes': total_uncompressed,
    }


def secure_download_response(file_object, *, filename, content_type='application/octet-stream'):
    """Return an authenticated file as an attachment with anti-caching headers."""
    safe_name = sanitize_original_filename(filename, default='download')
    response = FileResponse(
        file_object,
        content_type=content_type,
        as_attachment=True,
        filename=safe_name,
    )
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Frame-Options'] = 'DENY'
    response['Content-Security-Policy'] = "sandbox"
    return response
