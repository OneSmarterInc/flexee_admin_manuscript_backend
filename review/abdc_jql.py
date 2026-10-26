"""Pure, deterministic parsing and matching for an ABDC JQL catalogue edition.

Keep ABDC facts separate from OpenAlex verification. A match is a link, not an
endorsement or evidence that the journal is currently accepting submissions.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SOURCE = 'https://abdc.edu.au/abdc-journal-quality-list/'
REQUIRED = frozenset({'journal_title', 'publisher', 'issn', 'issn_online', 'year_inception',
                      'for_code', 'abdc_rating', 'abdc_edition', 'source_excel_row'})
RATINGS = frozenset({'A*', 'A', 'B', 'C'})
ISSN_PATTERN = re.compile(r'^(\d{4})-?(\d{3}[\dXx])$')


def clean_issn(value):
    clean = re.sub(r'[\u200b-\u200f\ufeff\s]', '', str(value or ''))
    match = ISSN_PATTERN.fullmatch(clean)
    if not match:
        return ''
    value = (match[1] + match[2]).upper()
    check = (11 - sum(int(d) * weight for d, weight in zip(value[:7], range(8, 1, -1))) % 11) % 11
    return f'{value[:4]}-{value[4:]}' if value[-1] == ('X' if check == 10 else str(check)) else ''


def normalize_title(text):
    text = unicodedata.normalize('NFKC', str(text or '')).casefold()
    return re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).strip()


@dataclass(frozen=True)
class ABDCRow:
    edition: int
    source_row: int
    title: str
    publisher: str
    issn: str
    issn_online: str
    year_inception: int | None
    for_code: str
    rating: str
    source_url: str

    @property
    def valid_issns(self):
        return tuple(dict.fromkeys(filter(None, (clean_issn(self.issn), clean_issn(self.issn_online)))))


def read_abdc_csv(file_path, *, edition=2025):
    """Reject malformed inputs *before* database changes; retain all source rows."""
    rows = []
    seen = set()
    with Path(file_path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED <= set(reader.fieldnames):
            raise ValueError('ABDC file must be the full metadata CSV, not an ISSN-only worklist. '
                             f'Missing: {sorted(REQUIRED - set(reader.fieldnames or []))}')
        for csv_line, item in enumerate(reader, 2):
            title = (item.get('journal_title') or '').strip()
            rating = (item.get('abdc_rating') or '').strip().upper()
            if not title or rating not in RATINGS:
                raise ValueError(f'CSV line {csv_line}: missing title or invalid ABDC rating {rating!r}')
            try:
                file_edition = int(str(item['abdc_edition']).strip())
                source_row = int(str(item['source_excel_row']).strip())
                year = (item.get('year_inception') or '').strip()
                year_inception = int(float(year)) if year else None
            except (TypeError, ValueError) as exc:
                raise ValueError(f'CSV line {csv_line}: invalid edition, row or inception year') from exc
            if file_edition != edition or source_row < 1 or (year_inception and not 1400 <= year_inception <= 2100):
                raise ValueError(f'CSV line {csv_line}: edition or year mismatch')
            if source_row in seen:
                raise ValueError(f'CSV line {csv_line}: duplicate source Excel row {source_row}')
            seen.add(source_row)
            rows.append(ABDCRow(
                edition=file_edition, source_row=source_row, title=title,
                publisher=(item.get('publisher') or '').strip(),
                issn=(item.get('issn') or '').strip(),
                issn_online=(item.get('issn_online') or '').strip(),
                year_inception=year_inception,
                for_code=str(item.get('for_code') or '').strip(), rating=rating,
                source_url=(item.get('source_url') or '').strip() or SOURCE,
            ))
    if not rows:
        raise ValueError('ABDC source file contains no journal rows')
    return rows


def build_index(indexed):
    """indexed: iterable of dicts with id, issn_l, issns, title, publisher."""
    by_issn, by_title = defaultdict(set), defaultdict(set)
    by_id = {}
    for item in indexed:
        row = dict(item)
        key = str(row['id'])
        row['id'] = key
        by_id[key] = row
        for raw in [row.get('issn_l'), *(row.get('issns') or [])]:
            issn = clean_issn(raw)
            if issn:
                by_issn[issn].add(key)
        by_title[normalize_title(row.get('title'))].add(key)
    return by_id, by_issn, by_title


def match_row(row, index):
    """Return (status, method, linked_id, suggestions).

    Never automatically join two records with different valid ISSNs by title.
    Ambiguous candidate sets remain pending human review.
    """
    by_id, by_issn, by_title = index
    matched = set().union(*(by_issn.get(issn, set()) for issn in row.valid_issns)) if row.valid_issns else set()
    if len(matched) == 1:
        return 'matched', 'issn', next(iter(matched)), []
    if len(matched) > 1:
        return 'pending_review', 'multiple_issn', None, sorted(matched)[:20]
    same_title = by_title.get(normalize_title(row.title), set())
    exact_publisher = [key for key in same_title if normalize_title(by_id[key].get('publisher')) == normalize_title(row.publisher)
                       and row.publisher]
    if len(exact_publisher) == 1:
        match = by_id[exact_publisher[0]]
        existing = [clean_issn(x) for x in [match.get('issn_l'), *(match.get('issns') or [])]]
        existing = {x for x in existing if x}
        # If both sides have ISSNs and none overlap, do not auto-link by title.
        if not (existing and row.valid_issns):
            return 'matched', 'title_publisher', exact_publisher[0], []
    if same_title:
        return 'pending_review', 'title_candidate', None, sorted(same_title)[:20]
    return 'unmatched', '', None, []
