"""Bounded extraction and inference in a disposable process. No network calls."""
import json
import sys
from pathlib import Path
try:
    from . import detector
except ImportError:
    import detector


def extract(path):
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('File exceeds detector size limit')
    limit = detector.MAX_CHARS
    if path.suffix.lower() in ('.txt', '.md'):
        raw = path.read_bytes()
        encoding = 'utf-32' if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')) else ('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        return raw.decode(encoding), []
    if path.suffix.lower() == '.docx':
        import zipfile
        from docx import Document
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > 200*1024*1024:
                raise ValueError('Expanded document exceeds limit')
        doc = Document(path)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.extend(c.text for c in row.cells)
        return '\n'.join(parts), ['Text boxes, footnotes, tracked changes and some nested content may not be extracted.']
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF is password protected')
        parts, notes, size = [], [], 0
        for i, page in enumerate(reader.pages):
            if i >= 300 or size > limit:
                notes.append('Some pages were not extracted because the extraction limit was reached.')
                break
            text = page.extract_text() or ''
            if not text.strip():
                notes.append('Some pages had no readable text; OCR is not included.')
            parts.append(text)
            size += len(text)
        return '\n'.join(parts), list(dict.fromkeys(notes))
    return None, ['This file type is not supported by AI-writing detection.']


if __name__ == '__main__':
    detector.MODEL_DIR = Path(sys.argv[2])
    text, notes = extract(Path(sys.argv[1]))
    result = detector.analyze(text) if text is not None else {'status':'not_checked', 'reason':notes[0]}
    # Do not persist manuscript excerpts or language-model prompts in detector reports.
    for passage in result.get('passages', []):
        passage.pop('text_excerpt', None)
    result['extraction_notes'] = notes
    if notes and result.get('status') == 'estimated':
        result['coverage'] = 'partial extraction / ' + result.get('coverage', 'unknown')
    print(json.dumps(result, ensure_ascii=True))
