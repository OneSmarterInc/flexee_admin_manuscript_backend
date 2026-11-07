"""Time-, memory-, page- and output-bounded extraction with private temporary files."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _limit(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, default))))
    except ValueError:
        return default


def extract_document(content, filename):
    suffix = Path(str(filename).lower()).suffix
    if suffix not in {'.pdf', '.docx', '.md'}:
        raise ValueError('Manuscript must be a .docx, .pdf, or .md file')
    if len(content) > 100 * 1024 * 1024:
        raise ValueError('Document exceeds the extraction size limit')
    maximum = _limit('DOCUMENT_MAX_TEXT_CHARS', 8000000, 1000, 16000000)
    if suffix == '.md':
        text = content.decode('utf-8', errors='replace')
        if len(text) > maximum:
            raise ValueError('Document exceeds the extracted-text limit')
        return text
    from ..storage_security import validate_upload
    validate_upload(content, filename)
    with tempfile.TemporaryDirectory(prefix='flexee-document-') as folder:
        source, destination = Path(folder) / ('input' + suffix), Path(folder) / 'text.txt'
        source.write_bytes(content)
        try:
            result = subprocess.run([
                sys.executable, '-I', str(Path(__file__).with_name('document_worker.py')),
                str(source), str(destination),
                str(_limit('DOCUMENT_MEMORY_MB', 1536, 256, 4096)),
                str(_limit('DOCUMENT_MAX_PAGES', 1000, 1, 5000)), str(maximum),
            ], timeout=_limit('DOCUMENT_PARSE_TIMEOUT_SECONDS', 60, 1, 120),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Document extraction took too long. Please simplify or export the document again.') from exc
        if result.returncode or not destination.exists() or destination.stat().st_size > maximum * 4:
            raise ValueError('The document could not be read within safe processing limits. Please check or export it again.')
        return destination.read_text(encoding='utf-8')
